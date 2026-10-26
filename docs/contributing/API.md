# HTTP API v1

[Development](README.md) · [Configuration](CONFIGURATION.md) · [Speedtest protocol](SPEEDTEST_PROTOCOL.md)

Base path: `/api/v1`, or `<public-prefix>api/v1` in HTTPS proxy mode. JSON timestamps use UTC RFC 3339 with `Z`; query timestamps must include an offset. Ranges are half-open `[from,to)`. Duration fields use seconds and latency fields milliseconds. Responses are not cached; CORS is not enabled.

## Session and request boundary

`POST /session` takes `{"password":"..."}` with JSON content type and exact Origin. It sets `connectivity_session` and returns `{authenticated:true,csrf_token}`. `GET /session` retrieves the current CSRF token; `DELETE /session` invalidates the session.

Every data read requires the cookie. Mutations, including logout, require JSON, exact Origin and `X-CSRF-Token`; login alone has no prior CSRF token. Never put credentials in URLs.

Cookies are host-only, HttpOnly and SameSite=Strict. Direct HTTP uses `/` without Secure; HTTPS proxy mode adds Secure and the configured path. Sessions expire absolutely after 24 hours, are capped at 20 and vanish on restart. Login failures are limited to five/source IP/minute and 100 globally/minute.

Host authority includes its port. Forwarded Host/protocol and X-Forwarded-For are ignored. Only explicit loopback proxy mode accepts validated `X-Real-IP`; nginx must overwrite it. See [proxy setup](../REVERSE_PROXY.md).

## Monitoring endpoints

| Method | Suffix | Result |
|---|---|---|
| GET | `/config` | `{resources,ui,revision}` and ETag; no auth/server secrets |
| PUT | `/config` | Exactly `{resources,ui}`, with `If-Match`; validated atomic save and activation |
| GET | `/status` | Resources/checks, current states, reasons, latest outcomes/details, freshness, generations/reset bounds, revision, server/start times, retention and monitor health |
| GET | `/history` | Resource summary/timeline map, no per-check raw history |
| GET | `/resources/{id}/history` | Resource and check summaries/timelines, successful latency statistics |
| GET | `/checks/{id}/results` | Raw results newest first, `next_cursor` or null |

Array order is persisted. IDs/check types cannot be reassigned; configuration writes preserve sections outside their scope. See [save semantics](CONFIGURATION.md#save-transaction).

## History queries and response shape

- `from`, `to`: default last hour ending at server time; start < end; no future end; maximum span fourteen days. Out-of-retention periods remain unknown.
- `max_points`: 1–1200, default 600.
- Raw results additionally accept `limit` (1–500, default 100) and opaque `cursor`. Cursors bind process, check generation and expiry; stale/reset cursors return 409. Results are filtered to the last hour.
- At most two expensive history calculations execute concurrently; excess returns 429 with retry guidance.

History returns `{server_time,config_revision,requested_range,resources:{ID:SERIES},checks:{ID:SERIES}}` from one coherent snapshot. Series contain range/retention/reset metadata, raw boundary, precision/approximation, bucket width, online/offline/unknown durations, availability, coverage and timeline. Timeline points have `from`, `to`, state and durations; the state is failure-first, not a duration proportion. Tiny floating-point residue is ignored when classifying fully observed buckets.

Check series also carry execution counts, diagnostic-code presence, latency sample count/mean/extrema, bounded-sample approximation and `count_latency_effective_range`. Older partial durations are prorated, not counts; effective ranges may cover full boundary minutes. Raw/summary seams avoid double counting.

Raw results expose start/completion, outcome/code, elapsed duration, latency or null, and upper-bound flags. Current status additionally includes destination, HTTP status, certificate expiry/remaining time and bounded recent error details. Full diagnostic strings are not retained in older summaries.

## Errors

Shape: `{"error":{"code":"revision_conflict","message":"...","fields":[]}}`.

| HTTP | Meaning |
|---|---|
| 400 | Malformed body, content type or query |
| 401 / 403 | Missing/invalid authentication / rejected Host, Origin or CSRF |
| 404 | Unknown route, resource, check or retained run |
| 409 / 412 / 428 | External-file/cursor/run conflict / stale revision / missing revision |
| 413 / 422 | Request too large / validation failed |
| 429 | Login, query or channel capacity limit |
| 503 / 500 | Persistence unavailable / unexpected server error without secrets or traceback |

Probe failures are valid observation data, not API failures. Browser disconnects and successful API requests are not monitoring observations.
