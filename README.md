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
| `contact` | Contact UUID. Searched in the **request body AND the URL**. See the coverage note below. |
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

### `contact` coverage — read this before trusting an empty result

Measured across all 40,683 fires (2026-07-12):

| | fires | |
|---|---|---|
| contact UUID in the request body | 26,539 | 65% |
| contact UUID only in the URL (e.g. Alchemer gift-card calls) | 486 | 1% |
| **no contact anywhere** | **13,892** | **34%** |

**That 34% is correct behavior, not a gap.** Those are config and reference lookups —
`Get Organization Info`, `Determine State and VAMC from Zip`, `Get Point People
Emails`, the classifier calls. The request is not *about* a subscriber, so there is no
contact in it to search for.

The split is essentially per-webhook, not per-flow. A flow with several Call Webhook
nodes will have some traceable and some not:

| flow | endpoint | traceable |
|---|---|---|
| Unrecognized Message | `add-to-db/upsert` | 100% |
| | `unrecognized-message-classification/classify` | 0% |
| Follow-Up After Referral | `sheet-service/write` | 100% |
| | `prompt-ai/call_gemini` | 0% |

So: **`?contact=` is the right tool for subscriber-data webhooks and useless for config
lookups.** An empty result does NOT mean "nothing failed for this subscriber" — it may
mean the failing call never carried a contact. The API returns a `hint` field saying
exactly that rather than a bare `[]`. When you hit it, search by `flow_uuid` or `url`
instead.

### Examples

```bash
# Everything that failed in a flow this week
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$BASE/failures?flow_uuid=c639b895-...&days=7"

# The failure for one subscriber (contact UUID comes straight from the IT email).
# Works for subscriber-data webhooks; returns a hint (not a bare []) for config lookups.
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

## Fidelity — this is a faithful superset of the TextIt UI

Verified 2026-07-12 by diffing `httplog_id 121122757` against
`textit.com/httplog/read/121122757/` field by field. The httplog detail page shows:
flow name + editor link, date, elapsed ms, the raw request block, and the raw response
block. **All of it is captured.** Nothing on that page is dropped.

Differences, all in our favour:
- `fired_at` is **more precise** — the UI truncates to the minute; we keep microseconds.
- We add `httplog_id` and `flow_uuid` as queryable keys.
- **Credentials are masked.** The TextIt UI renders the password in plaintext; we
  return `[REDACTED]`.

Not in the httplog at all (so not a gap in this API): the retry/attempt number. The
"Final attempt failed (attempt #2)" in the IT error emails comes from TextIt's retry
logic, which the httplog page does not render.

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
