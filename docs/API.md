# Internal API v1

All paths start `/api/v1` in direct mode. With `server.public_url: https://HOST/monitor/`, they start `/monitor/api/v1`; the endpoint suffixes below are unchanged. See [REVERSE_PROXY.md](REVERSE_PROXY.md). JSON timestamps are UTC RFC 3339 with `Z`. All ranges are half-open `[from,to)`. Finite durations use `_seconds`; protocol metrics use `_ms`. Every response is `Cache-Control: no-store`. No CORS is enabled.

## Authentication and writes

`POST /session` with `{"password":"..."}` signs in. Send `Content-Type: application/json` and an `Origin` exactly equal to the validated request origin. A successful response sets a host-only `HttpOnly; SameSite=Strict` cookie (`Path=/` in direct mode, the configured base path in proxy mode) and returns `{authenticated:true,csrf_token:...}`. Never place the password/token in a URL.

`GET /session` returns the current session's CSRF token, not its cookie token. All other data/config endpoints require the session cookie. `DELETE /session` invalidates it. All mutations including logout require JSON content type, exact Origin, and `X-CSRF-Token`. Login requires Origin but not CSRF. Direct HTTP cookies intentionally lack `Secure`; explicit local HTTPS proxy mode sets `Secure` and scopes the cookie to the public URL's path. Sessions expire absolutely after 24 hours, restart invalidates them, and at most 20 exist. Login failures are limited to five per source IP/minute and 100 globally/minute. Forwarded host/scheme and X-Forwarded-For are ignored. Proxy mode accepts a validated `X-Real-IP` only from its trusted loopback connection; nginx must overwrite that header.

## Endpoints

| Method | Path | Response / purpose |
|---|---|---|
| GET | `/config` | `{resources,ui,revision}` and ETag. Entire auth/server sections excluded. |
| PUT | `/config` | Body must contain exactly `{resources,ui}`; `If-Match: REVISION` required. Full validation and file conflict check, atomic write, activation, then new editable config. |
| GET | `/status` | Current resources/checks, latest details and completion, freshness deadlines, reasons, generation/reset bounds, `server_time`, `started_at`, `config_revision`, refresh interval, retention bounds, monitor health. |
| GET | `/history` | Overview resource summaries/timelines, no per-check raw histories. |
| GET | `/resources/{id}/history` | One resource plus all its check summaries, timelines, successful latency statistics. |
| GET | `/checks/{id}/results` | Recent raw results newest first; opaque `next_cursor` or null. |

IDs and check types are immutable for an existing check. IDs may not be moved across resources. Timing defaults are interval 30 seconds, timeout 5 seconds. Runtime updates cannot change listener or password. Validation failures and persistence errors leave active settings/history unchanged. External file conflicts need reconciliation and restart, not repeated blind retries.

The order of the `resources` array and each resource's `checks` array is meaningful: it is saved to YAML and returned in configuration/status responses in that order. Reordering existing entries, without changing their measurement settings, preserves histories, generations, cadence, and active executions. It uses the same `PUT /config` revision, validation, and persistence rules as other edits; no separate reorder endpoint exists.

## Speedtest extension

The authenticated control/configuration endpoints and versioned WebSocket transfer protocol are documented in [SPEEDTEST_PROTOCOL.md](SPEEDTEST_PROTOCOL.md). All paths remain under this same prefixed API root. `/config` continues to accept only resources/UI; it preserves the private `speedtest` section. Peer configuration is changed through the revision-checked `/speedtest/peers` endpoint, whose read response never includes passwords.

WebSocket upgrades at `/speedtest/channel` require an already accepted run, matching session/Origin, and a bounded first-message CSRF/run/channel handshake. Merely opening a socket does not start a test. Ordinary JSON content-type/body limits and existing monitoring endpoints are unchanged.

## Query parameters

History accepts `from`, `to`, `max_points` (default 600, 1–1200). Default range is server-now minus one hour through server-now. Start must precede end, end may not be in the future, and span is at most 14 days. Older ranges are allowed but missing/out-of-retention time is unknown. All timestamps require an offset. Current status is independent of these ranges.

Results also accepts `from`, `to`, `limit` (default 100, 1–500), and `cursor`. Cursors are authenticated opaque values tied to the process, check generation, and expiry. Reset/expired/restarted cursors return 409. Raw results are immediately filtered to the last hour even before pruning.

Example history query (URL-encode offset `+` when not using `Z`):

```text
GET /api/v1/resources/router/history?from=2026-09-18T10:00:00Z&to=2026-09-18T11:00:00Z&max_points=600
```

## History shape and precision

Top-level response: `{server_time,config_revision,requested_range,resources:{ID:SERIES},checks:{ID:SERIES}}`. One coherent generation is copied into bounded numeric snapshots before calculation. At most two expensive queries execute concurrently; excess returns 429 with `Retry-After`.

Each series includes requested range, retention and history bounds, reset reason/time, raw boundary, precision label, approximation flag, effective older summary range, chart bucket width, online/offline/unknown seconds, availability (null with zero observations), coverage, and a bounded timeline. Timeline points contain from/to, state (`online`, `offline`, or `unknown`), and all three durations. The displayed state is failure-first: any offline duration means `offline`; otherwise unobserved time means `unknown`; otherwise `online`. Sub-microsecond floating-point residue in unknown duration is ignored when classifying an otherwise observed block. The state does not mean the whole block had that state: duration totals and availability remain unchanged. Unknown is computed from the selected interval, not only allocated buckets.

Check series also include execution counts, diagnostic-code presence, latency sample count/sum-derived mean/min/max, bounded-sample approximation flag, and `count_latency_effective_range`. Older partial-minute durations are prorated; counts never become fractional. Count/latency effective ranges may extend to complete minute boundaries, including the raw/summary seam, and are explicitly disclosed. There is no double count at the seam. The bucket straddling the moving fourteen-day cutoff is omitted. Raw duration segments supply recent boundary information.

Recent results contain started/completed timestamps, outcome, code, elapsed seconds, protocol latency or null, and upper-bound flag. Current status separately carries destination, HTTP status, certificate expiry/remaining seconds, and at most 20 errors of at most 512 UTF-8 bytes per check from the last hour. Historical detailed strings are not archived.

## Errors

```json
{"error":{"code":"revision_conflict","message":"Configuration changed in another tab. Reload before saving.","fields":[]}}
```

| Status | Meaning |
|---|---|
| 400 | Malformed JSON/query/content type |
| 401 | Missing, invalid, expired session or wrong password |
| 403 | Host, Origin, CSRF rejected |
| 404 | Missing route/resource/check |
| 409 | External-file or result-cursor generation conflict |
| 412 | Stale configuration revision |
| 413 | Request exceeds 1 MiB |
| 422 | Invalid configuration |
| 428 | If-Match missing |
| 429 | Login/query concurrency limit; Retry-After supplied |
| 503 | Configuration could not be persisted |
| 500 | Unexpected error, no traceback/secrets returned |

Probe failures are ordinary result data, not API failures. API clients must not infer monitoring observations from browser connectivity or from successful HTTP API requests.
