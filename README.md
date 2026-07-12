# webhook-log-api

Query the TextIt webhook HTTP log from BigQuery.

**Last updated: 2026-07-12**

## Why

An IT error email tells us a webhook failed. Finding the actual fire — the request
body, the response, the status — used to mean binary-searching page numbers on
`textit.com/httplog/webhooks/` (25 rows/page, ~1,600 pages, newest-first) until we
landed near the right timestamp.

TextIt also retains only **~3.85 days** of that log. If the email arrived Friday and
we look on Tuesday, the row is gone from TextIt. It is not gone from BigQuery.

So past ~4 days this is not a nicer window onto TextIt — it is the only window.

Data source: BigQuery tables `RESPONSES.webhook_log` (list rows, indefinite
retention) and `RESPONSES.webhook_log_detail` (request/response bodies, 30-day
partition expiry) in the `early-alert-responses` project. Both are populated by a
daily scheduled ingest that scrapes the TextIt console (no API exists for this log).

This service is read-only. It never writes to BigQuery.

## Auth — IAM, not a shared token

```bash
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
     "$BASE/failures?flow_uuid=<uuid>&days=7"
```

This service is deployed `--no-allow-unauthenticated`. That is a **deliberate
departure** from `zip-lookup` / `add-to-db` / `sheet-service`, and the deciding
question is the **caller**:

| caller | auth | why |
|---|---|---|
| TextIt (`zip-lookup`, `add-to-db`, `sheet-service`) | `--allow-unauthenticated` + shared secret | TextIt **cannot mint Google OIDC tokens**. This is a constraint, not a choice. |
| Cloud Scheduler (`contacts-sync`, `vamc-sync`, `nightly-pipeline`, `backup-textit-flows`) | `--no-allow-unauthenticated` + IAM | Scheduler speaks OIDC. |
| **A human (this service)** | `--no-allow-unauthenticated` + IAM | We already hold Google identities in this project. |

The TextIt constraint does not apply here, so there is no reason to accept a shared
secret — and the stakes are higher than for the other services: **this endpoint
returns request bodies containing subscriber PII** (contact uuid, zip, state, gender,
ethnicity, free-text replies). A bearer token on a public URL guarding that is
strictly worse than IAM.

With IAM: no shared secret to leak or rotate, and access is **revocable per-person**.

### Granting access

```bash
gcloud run services add-iam-policy-binding webhook-log-api \
  --region=us-east1 \
  --member="user:logan@circlesofsupport.net" \
  --role="roles/run.invoker"
```

Repeat per person. Revoke with `remove-iam-policy-binding` — access is per-account,
so removing someone does not require rotating a secret and redistributing it.

## `GET /failures`

One endpoint. All filters optional; they **AND** together. Defaults to failures
only, last 7 days, 50 rows.

| param | meaning |
|---|---|
| `flow_uuid` | TextIt flow UUID. **Stable identity — not the flow name.** |
| `contact` | Contact UUID. Searched inside the **request body**. |
| `url` | Substring of the target URL, e.g. `get-responses_v2` |
| `status` | Exact HTTP status, e.g. `500` |
| `status_class` | `non2xx` (default) · `4xx` · `5xx` · `timeout` · `all` |
| `httplog_id` | Exact row. Only useful once you already have one. |
| `days` / `hours` / `since` / `until` | Time window. `since`/`until` are ISO 8601. |
| `include_success` | `true` to include 2xx |
| `limit` | Default 50, max 500 |

### Why UUIDs and not flow names

Flow **names** are mutable, decorated (`LIVE: `, dates, initials) and not unique.
`LIVE: Get Organization Info (2026-06-24 LP)` is a display label, not an identity.
Keying a lookup on it bakes a defect into the interface, so this API rejects it:

```
GET /failures?flow_uuid=LIVE:+Get+Organization+Info
400  flow_uuid does not look like a UUID. This API keys on UUIDs, not flow names —
     names are mutable, decorated and non-unique.
```

`flow_name` **is** returned, for display. It is never a key.

### Examples

```bash
# Everything that failed in a flow this week
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$BASE/failures?flow_uuid=c639b895-...&days=7"

# The failure for one subscriber (contact UUID comes straight from the IT email)
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$BASE/failures?contact=e8c1aefd-4f89-4744-84a3-37aa67f26956"

# Every timeout hitting get-responses_v2 in the last 3 days
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$BASE/failures?url=get-responses_v2&status_class=timeout&days=3"

# 500s only, on one flow, last 30 days
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$BASE/failures?flow_uuid=<uuid>&status=500&days=30"
```

### Response

```json
{
  "status": "success",
  "count": 1,
  "truncated": false,
  "window_start": "2026-07-05T19:00:00+00:00",
  "results": [{
    "httplog_id": 121099417,
    "fired_at": "2026-07-12T02:14:03.221+00:00",
    "status_code": null,
    "is_failure": true,
    "elapsed_ms": 15000,
    "webhook_url": "https://us-east4-early-alert-responses.cloudfunctions.net/...",
    "flow_name": "LIVE: Get Organization Info (2026-06-24 LP)",
    "flow_uuid": "c639b895-...",
    "textit_flow_url": "https://textit.com/flow/editor/c639b895-.../",
    "request":  { "method": "POST", "path": "/...", "headers": "...", "body": "..." },
    "response": { "status_line": "Connection Error", "headers": "", "body": "" }
  }]
}
```

Notes:
- `status_code: null` means **timeout** — no HTTP response at all. The detail page
  shows the literal string `Connection Error`, which is what `status_line` carries.
- **Credentials are masked** (`[REDACTED]`) in headers and bodies. Masking happens at
  ingest and is irreversible.
- **Bodies are otherwise raw and contain PII** — contact UUID, zip, state, gender,
  ethnicity, free-text subscriber replies. Treat responses accordingly.

### Body retention: 30 days

`webhook_log_detail` has `partition_expiration_days = 30`. Past that, the list row
survives (status, time, URL) but the bodies are gone. The API says so explicitly
rather than returning a silent null:

```json
{ "httplog_id": 120..., "detail_expired": true,
  "detail_note": "Request/response bodies are retained 30 days. This fire is older; only list metadata remains." }
```

## `GET /summary`

Failure counts grouped by flow, for a window. "What is breaking?"

```bash
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$BASE/summary?days=7"
```

Returns `flow_uuid`, `flow_name`, `total`, `failures`, `timeouts`, `failure_pct`,
ordered by failures descending. Keyed on `flow_uuid`; `flow_name` is display only.

## `GET /health`

Unauthenticated liveness check.

## Deploy

Cloud Run, `us-east1`, same pattern as `zip-lookup`. Cloud Build trigger on push to
`main` (`cloudbuild.yaml`). Image goes to `webhook-repo` in Artifact Registry.

**Required at deploy time:**
- **No env vars.** There is no shared secret.
- Deployed `--no-allow-unauthenticated`; grant `roles/run.invoker` per user (above).
- The runtime service account needs **BigQuery Data Viewer + Job User** on
  `early-alert-responses`. The default compute SA
  (`853176470965-compute@developer.gserviceaccount.com`) already has this from the
  other pipelines.

Read-only. This service never writes to BigQuery.
