"""
webhook-log-api
===============
Query the TextIt webhook HTTP log from BigQuery.

WHY THIS EXISTS
An IT error email tells you a webhook failed. Finding the actual fire — the
request body, the response, the status — currently means binary-searching page
numbers on textit.com/httplog/webhooks/ (25 rows/page, ~1,600 pages, newest
first) until you land near the right timestamp.

Worse: TextIt retains only ~3.85 days of that log. If the email arrived Friday
and you look on Tuesday, the row is GONE from TextIt. It is not gone from
BigQuery. So this is not a nicer window onto TextIt — past ~4 days it is the
ONLY window.

DESIGN
One endpoint, several composable filters. Every filter is a STABLE IDENTIFIER
(uuid, url, status, time) — never a flow NAME, which is mutable, decorated
("LIVE: ", dates, initials) and non-unique. Keying a lookup on a display label
bakes a defect into the interface.

Filters AND together. Defaults to failures only.

    GET /failures?flow_uuid=<uuid>&days=7
    GET /failures?contact=<contact-uuid>
    GET /failures?url=get-responses_v2&status=500
    GET /failures?flow_uuid=<uuid>&status_class=timeout&days=30

Auth: `token` header, same as sheet-service / zip-lookup.
"""
from flask import Flask, request, jsonify
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from google.cloud import bigquery

__API_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")

PROJECT = "early-alert-responses"
T_LOG = f"`{PROJECT}.RESPONSES.webhook_log`"
T_DETAIL = f"`{PROJECT}.RESPONSES.webhook_log_detail`"

# webhook_log_detail carries partition_expiration_days=30. Past that the list row
# survives (status, time, url) but the BODY is gone. Callers are told explicitly
# rather than being handed a silent NULL.
DETAIL_RETENTION_DAYS = 30

MAX_LIMIT = 500
DEFAULT_LIMIT = 50

logging.basicConfig(level="INFO")
app = Flask(__name__)

bq = bigquery.Client(project=PROJECT)

UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def _check_token():
    t = request.headers.get("token")
    if __API_TOKEN and (not t or t != __API_TOKEN):
        return jsonify({"status": "fail", "error": "Invalid token"}), 401
    return None


def _parse_since(args):
    """Resolve a start timestamp from ?since=<iso> or ?days=N or ?hours=N.

    Defaults to 7 days. The list table is partitioned on DATE(fired_at), so a
    bounded window is also what keeps these queries cheap.
    """
    since = args.get("since")
    if since:
        try:
            s = since.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt, None
        except ValueError:
            return None, f"Could not parse since='{since}' (use ISO 8601, e.g. 2026-07-10T14:00:00Z)"

    for key, unit in (("days", "days"), ("hours", "hours")):
        if key in args:
            try:
                n = int(args[key])
            except ValueError:
                return None, f"{key} must be an integer"
            if n < 0:
                return None, f"{key} must be >= 0"
            return datetime.now(timezone.utc) - timedelta(**{unit: n}), None

    return datetime.now(timezone.utc) - timedelta(days=7), None


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "webhook-log-api"}), 200


@app.route("/failures", methods=["GET"])
def failures():
    """Query webhook fires. Filters AND together; all are optional.

    flow_uuid       TextIt flow UUID (stable identity — NOT the flow name)
    contact         contact UUID; searched inside the REQUEST BODY
    httplog_id      exact row (only useful once you already have one)
    url             substring of the target URL, e.g. 'get-responses_v2'
    status          exact HTTP status, e.g. 500
    status_class    non2xx (default) | 4xx | 5xx | timeout | all
    since / days / hours   time window (default: 7 days)
    include_success       'true' to include 2xx as well
    limit           default 50, max 500
    """
    err = _check_token()
    if err:
        return err

    try:
        args = request.args

        since, perr = _parse_since(args)
        if perr:
            return jsonify({"status": "fail", "error": perr}), 400

        until = None
        if args.get("until"):
            try:
                u = args["until"].replace("Z", "+00:00")
                until = datetime.fromisoformat(u)
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
            except ValueError:
                return jsonify({"status": "fail", "error": "Could not parse until (use ISO 8601)"}), 400

        try:
            limit = min(int(args.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
        except ValueError:
            return jsonify({"status": "fail", "error": "limit must be an integer"}), 400

        where = ["l.fired_at >= @since"]
        params = [bigquery.ScalarQueryParameter("since", "TIMESTAMP", since)]

        if until:
            where.append("l.fired_at <= @until")
            params.append(bigquery.ScalarQueryParameter("until", "TIMESTAMP", until))

        # --- flow_uuid: the stable flow identity ---
        flow_uuid = args.get("flow_uuid")
        if flow_uuid:
            if not UUID_RE.match(flow_uuid):
                return jsonify({
                    "status": "fail",
                    "error": f"flow_uuid does not look like a UUID: '{flow_uuid}'. "
                             "This API keys on UUIDs, not flow names — names are "
                             "mutable, decorated and non-unique.",
                }), 400
            where.append("l.flow_uuid = @flow_uuid")
            params.append(bigquery.ScalarQueryParameter("flow_uuid", "STRING", flow_uuid))

        # --- contact: lives in the request BODY, so this needs the detail join ---
        contact = args.get("contact")
        if contact:
            if not UUID_RE.match(contact):
                return jsonify({
                    "status": "fail",
                    "error": f"contact does not look like a UUID: '{contact}'",
                }), 400
            where.append("d.request_body LIKE @contact_like")
            params.append(bigquery.ScalarQueryParameter("contact_like", "STRING", f"%{contact}%"))

        if args.get("httplog_id"):
            try:
                hid = int(args["httplog_id"])
            except ValueError:
                return jsonify({"status": "fail", "error": "httplog_id must be an integer"}), 400
            where.append("l.httplog_id = @httplog_id")
            params.append(bigquery.ScalarQueryParameter("httplog_id", "INT64", hid))

        if args.get("url"):
            where.append("l.webhook_url LIKE @url_like")
            params.append(bigquery.ScalarQueryParameter("url_like", "STRING", f"%{args['url']}%"))

        if args.get("status"):
            try:
                sc = int(args["status"])
            except ValueError:
                return jsonify({"status": "fail", "error": "status must be an integer"}), 400
            where.append("l.status_code = @status")
            params.append(bigquery.ScalarQueryParameter("status", "INT64", sc))

        # --- failure filtering ---
        include_success = args.get("include_success", "").lower() in ("1", "true", "yes")
        status_class = args.get("status_class", "").lower()

        if status_class == "timeout":
            # NULL status_code = no HTTP response at all. TextIt's UI renders '--';
            # the detail page shows the literal string 'Connection Error'.
            where.append("l.status_code IS NULL")
        elif status_class == "4xx":
            where.append("l.status_code BETWEEN 400 AND 499")
        elif status_class == "5xx":
            where.append("l.status_code BETWEEN 500 AND 599")
        elif status_class == "all" or include_success:
            pass  # no failure filter
        else:
            where.append("l.is_failure")  # default: failures only

        sql = f"""
        SELECT
          l.httplog_id,
          l.fired_at,
          l.status_code,
          l.is_failure,
          l.elapsed_ms,
          l.webhook_url,
          l.flow_name,
          l.flow_uuid,
          d.request_method,
          d.request_path,
          d.request_host,
          d.request_headers,
          d.request_body,
          d.response_status_line,
          d.response_headers,
          d.response_body
        FROM {T_LOG} l
        LEFT JOIN {T_DETAIL} d USING (httplog_id)
        WHERE {' AND '.join(where)}
        ORDER BY l.fired_at DESC
        LIMIT {limit}
        """

        job = bq.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
        rows = list(job.result())

        detail_cutoff = datetime.now(timezone.utc) - timedelta(days=DETAIL_RETENTION_DAYS)

        results = []
        for r in rows:
            expired = r.request_body is None and r.fired_at < detail_cutoff
            item = {
                "httplog_id": r.httplog_id,
                "fired_at": r.fired_at.isoformat(),
                "status_code": r.status_code,          # null == timeout
                "is_failure": r.is_failure,
                "elapsed_ms": r.elapsed_ms,
                "webhook_url": r.webhook_url,
                "flow_name": r.flow_name,              # for display ONLY
                "flow_uuid": r.flow_uuid,
                "textit_flow_url": (
                    f"https://textit.com/flow/editor/{r.flow_uuid}/" if r.flow_uuid else None
                ),
            }
            if expired:
                # Be explicit. A silent null would read as "no body was sent".
                item["detail_expired"] = True
                item["detail_note"] = (
                    f"Request/response bodies are retained {DETAIL_RETENTION_DAYS} days. "
                    "This fire is older; only list metadata remains."
                )
            else:
                item["request"] = {
                    "method": r.request_method,
                    "path": r.request_path,
                    "host": r.request_host,
                    "headers": r.request_headers,
                    "body": r.request_body,
                }
                item["response"] = {
                    "status_line": r.response_status_line,   # 'Connection Error' on timeout
                    "headers": r.response_headers,
                    "body": r.response_body,
                }
            results.append(item)

        return jsonify({
            "status": "success",
            "count": len(results),
            "limit": limit,
            "window_start": since.isoformat(),
            "truncated": len(results) == limit,
            "results": results,
        }), 200

    except Exception as ex:
        logging.exception("query failed")
        return jsonify({"status": "fail", "error": str(ex)}), 500


@app.route("/summary", methods=["GET"])
def summary():
    """Failure counts grouped by flow, for a time window. 'What is breaking?'

    Deliberately keyed and returned by flow_uuid; flow_name is display only.
    """
    err = _check_token()
    if err:
        return err

    try:
        since, perr = _parse_since(request.args)
        if perr:
            return jsonify({"status": "fail", "error": perr}), 400

        sql = f"""
        SELECT
          flow_uuid,
          ANY_VALUE(flow_name) AS flow_name,
          COUNT(*) AS total,
          COUNTIF(is_failure) AS failures,
          COUNTIF(status_code IS NULL) AS timeouts,
          ROUND(SAFE_DIVIDE(COUNTIF(is_failure), COUNT(*)) * 100, 2) AS failure_pct
        FROM {T_LOG}
        WHERE fired_at >= @since
        GROUP BY flow_uuid
        HAVING failures > 0
        ORDER BY failures DESC
        LIMIT 100
        """
        job = bq.query(sql, job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("since", "TIMESTAMP", since)]
        ))

        return jsonify({
            "status": "success",
            "window_start": since.isoformat(),
            "results": [{
                "flow_uuid": r.flow_uuid,
                "flow_name": r.flow_name,
                "textit_flow_url": (
                    f"https://textit.com/flow/editor/{r.flow_uuid}/" if r.flow_uuid else None
                ),
                "total": r.total,
                "failures": r.failures,
                "timeouts": r.timeouts,
                "failure_pct": r.failure_pct,
            } for r in job.result()],
        }), 200

    except Exception as ex:
        logging.exception("summary failed")
        return jsonify({"status": "fail", "error": str(ex)}), 500
