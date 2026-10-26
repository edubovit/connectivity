# Architecture and invariants

[Development](README.md)

Connectivity is one asyncio process serving aiohttp, scheduling independent probes and keeping NumPy-backed history in RAM. YAML is the only persisted application state. There is no database, frontend build, external scheduler, automatic YAML watcher or distributed history. Run one worker and one owner per configuration file.

## Code map

| Module | Owns |
|---|---|
| `connectivity/__main__.py` | Required `--config`, Python-minor check, listener, signals and shutdown |
| `config.py` | Validation, lock, revisions, conflict checks and atomic persistence |
| `probes.py` | OS DNS/ping, HTTP/TCP/TLS, result codes and cancellation |
| `engine.py` | Cadence, freshness, resource aggregation and generation reconciliation |
| `history.py` | Numeric logs, retention, minute summaries and query snapshots |
| `web.py` | Authentication, Host/Origin/CSRF, history APIs and static allowlist |
| `speed_protocol.py` | Transfer profile, counters, RTT validation, meter and flow credit |
| `speedtest.py` | Admission slot, run state, peer generations and finalization |
| `speed_client.py`, `speed_web.py` | Outgoing peer client and receiving HTTP/WebSocket endpoints |
| `static/` | Dashboard, charts, ordering, selects and browser speedtest client |

Paths in the table are relative to `connectivity/` unless fully qualified. Tests use `tests/`; reusable local fixtures and measurement scripts use `tools/`.

## Scheduling and observations

- Each check has its own deterministically staggered cadence. It never overlaps itself; missed starts are skipped rather than queued or retried. At most 200 probes execute concurrently.
- Results take effect at completion. Freshness ends after the next cadence slot following completion, plus the check timeout and two seconds. A pending probe does not erase a fresh result.
- `success` and attempted-network `failure` are observations. `error` means the monitor could not produce a valid observation: missing ping binary, permission/parser failure or resolver saturation must not count as target downtime.
- A resource is offline if any check has a fresh failure; otherwise unknown if any check is unknown; otherwise online.
- Monotonic time drives duration and scheduling. Wall-clock discontinuities clear history; monitor gaps invalidate observations and infer unknown time.
- Generations prevent late results from deleted/reset checks publishing. Renames and order changes preserve measurements and cadence; measurement edits reset only the affected check and resource aggregate.

## Probe semantics

DNS uses the OS resolver, at most 32 outstanding calls, and bounded daemon workers. An OS call may outlive its timeout and retain a worker until it returns. There is no application DNS cache, alternate-address retry or environment-proxy use.

Ping launches the native executable without a shell, bounds output, checks the reply address, and kills/reaps cancelled children. Cover IPv4/IPv6, scoped addresses, localized output, compressed address punctuation and upper-bound readings such as `<1 ms`.

HTTP sends no custom headers, body or cookies, follows no redirects and measures through final response headers, including DNS/setup. TCP measures connect time excluding DNS. TLS measures only the TLS handshake and validates hostname, trust and validity dates. Failed or missing latency remains unavailable, never zero or the timeout duration.

## History

- Retention: one hour of raw observations/events and fourteen days of UTC-minute summaries. History, sessions and speedtest results disappear on restart.
- Availability = online / (online + offline), or null without observations. Coverage = observed / selected duration. Unknown contributes to neither uptime nor downtime.
- Timeline buckets show the worst state present: any offline duration wins; otherwise missing time is unknown; otherwise online. A red bucket is not a claim that its entire duration was offline.
- Older partial-minute durations are prorated and labeled approximate. Counts remain integers. Effective count/latency ranges may include complete boundary minutes and are disclosed. Raw/summary seams must not double count; the partial minute crossing the retention cutoff is omitted.
- Queries use one coherent revision/generation snapshot and calculate off the event loop. At most two expensive history queries run concurrently. Keep point limits, pagination, error buffers and retention pruning bounded.
- Recent diagnostics retain at most 20 errors per check from the last hour, each bounded to 512 UTF-8 bytes. Historical summaries do not archive full strings.

## Security and lifecycle

All data APIs require an owner session. Writes require JSON, exact Origin and CSRF. Validate exact Host even on wildcard binds; never trust forwarded Host/protocol or enable permissive CORS. HTTPS proxy mode trusts `X-Real-IP` only through the explicit loopback boundary. See [API](API.md) for limits.

Do not log passwords, peer credentials, session cookies, CSRF tokens or raw credential-bearing exceptions. Keep service code read-only to its account and configuration privately writable. Shutdown stops producers and awaits bounded cleanup before releasing ownership; a service being active does not prove probes or public routing work.
