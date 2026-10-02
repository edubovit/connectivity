# Speedtest protocol 1

This document defines Connectivity's speedtest HTTP and WebSocket contract. Use the node's canonical base URL, including its prefix. Below, `API` means `<base>api/v1`; WebSocket URLs use the corresponding `ws`/`wss` scheme. Only compatible Connectivity nodes participate.

## Authentication and admission

The browser uses its existing session and CSRF token. An initiating node opens a fresh, isolated cookie jar, logs in through `POST API/session` with the configured peer owner password, and retains the returned CSRF token privately. It uses the peer's canonical origin for Origin checks. HTTPS verification remains enabled; HTTP requires an explicitly configured HTTP peer. Neither REST nor WebSocket requests follow redirects. Environment proxies are not used.

- `GET API/speedtest/capabilities`: authenticated `{instance, protocol: 1, profile}`. `instance` changes on process restart. Self-instance tests are rejected.
- `GET API/speedtest/peers`: `{revision, peers:[{id,name,url,has_password}]}`. No secret values.
- `PUT API/speedtest/peers`, `If-Match: revision`: `{peers:[{id,name,url,password?}]}`. Omission preserves an existing secret only for an unchanged canonical URL. New/changed URL requires re-entry. Saves are atomic and share the global configuration revision. Active outgoing target/credential edits/deletion return 409.
- `POST API/speedtest/runs`: `{mode:"browser"}` or `{mode:"node",peer_id:"..."}`. Returns 202 with the accepted run. Arbitrary URLs, durations, passwords, or stream counts are not accepted here.
- Node initiator authenticates to its peer and calls `POST API/speedtest/accept` with `{protocol:1,profile,instance,remaining_seconds}`. The peer validates the profile, instance, budget, and its single slot before returning 202 with its run ID.
- `GET API/speedtest/state`: capabilities, active involvement, and latest outgoing summaries, excluding detailed bucket series.
- `GET API/speedtest/runs/{id}`: current or retained detailed result. Unknown/expired IDs return 404, never another run.
- `DELETE API/speedtest/runs/{id}`: authorized cancellation. A committed terminal result is not rewritten. No queue or automatic retry exists.

Both nodes reserve a local slot; incoming/outgoing/browser tests compete for that one slot. Opposite simultaneous reservations fail busy rather than wait. Nodes retain results only for outgoing configured peers. Browser/incoming terminal delivery receipts expire after five seconds. Peer URL/password changes and delete/recreate operations change a separate settings generation; stale callbacks cannot publish into a newer generation.

## Fixed profile

The capability profile must match exactly. Production values are:

| Field | Value |
|---|---:|
| `version` | 1 |
| `streams` | 4 |
| `warmup_seconds` | 2 |
| `measurement_seconds` | 10 |
| `timeout_seconds` | 60 |
| `setup_seconds` | 10 |
| `channel_seconds` | 5 |
| `idle_seconds` | 5 |
| `idle_samples` | 20 |
| `idle_interval` | 0.1 |
| `loaded_interval` | 0.2 |
| `rtt_timeout` | 2 |
| `liveness_seconds` | 5 |
| `chunk_bytes` | 65,536 |
| `window_bytes` | 2,097,152 per stream |

Tests can inject a shortened internal profile in test code. Production configuration/API cannot change it. A mismatch fails before transfer; no silent downgrade.

## Channel handshake

Every channel upgrades `GET API/speedtest/channel`. Cookies and exact Origin are required. There must already be an accepted receiving run owned by that session. Unrelated owner sessions may cancel a run but cannot join its channels.

Within five seconds, send a JSON text message, at most 4 KiB:

```json
{"protocol":1,"run_id":"...","csrf":"...","channel":"control"}
```

For payload channels, add `channel:"data"`, `direction:"download"|"upload"`, and `index:0..3`. One control channel is allowed. Each direction/index pair can join once, only in its setup phase. The reply is `{type:"ready",protocol:1,run_id,instance}`. The client verifies the run and responder identities. Credentials/grants never appear in the URL.

Compression is neither negotiated nor permitted. Clients reject unexpected WebSocket extensions. Binary payload messages are exactly 65,536 bytes; the implementation sets aiohttp's message limit to 65,537 because its boundary check rejects a message at the limit. Other frame sizes/types are protocol errors. Control messages have a separate 4 KiB application limit and flow-credit text frames a 128-byte limit.

There are at most eight joined/pending server channels for the active run. Pending unauthenticated handshakes are tracked and closed on cancellation, not left behind when a new slot is admitted.

## Control channel

Requests are `{id,op,data}` with a unique nonnegative safe-integer ID. Replies are `{id,data}`. Expired/timed-out IDs are not reused; late replies are ignored. At most eight operations may be pending on a client; the server caps control messages at 64/second.

| Operation | Data and effect |
|---|---|
| `echo` | `{nonce}`; returns the same nonce, current upload-window membership, and receiver-side remaining seconds. The priming echo is not a latency sample. |
| `keepalive` | `{}`; one per second while the client participates. |
| `idle_done` | `{rtt}`; validates an idle RTT summary with at least one sample. |
| `prepare` | `{direction}`; download must follow idle, upload must follow completed download. |
| `go` | `{}`; requires all four streams. Starts senders for download or the receiver clock for upload. |
| `stop_download` | `{metric,rtt}`; accepts the initiating receiver's complete window/counts, bounded by the responder's sent count, then closes download channels. |
| `stop_upload` | `{rtt}`; valid only after the receiver's complete window. Stops upload channels and returns the authoritative receiver metric. |
| `finish` | `{}`; commits completion/failure after both directions, then returns metrics/outcome and closes the run. |

Unknown operations/fields and wrong phase transitions fail. The server emits:

- `{event:"progress",phase,live}` approximately once/second.
- `{event:"window",direction:"upload",state:"start",remaining_seconds}` and a corresponding `state:"end"` event from the upload receiver's clock.
- `{event:"terminal",result}` before closure when possible. A terminal event can resolve an in-flight finish request. Once finish is committed, racing cancellation does not change that outcome.

WebSocket close code 4001 communicates cancellation, including on data channels where closure may be observed before the control event. Early ordinary/error closure is not successful completion. Clients preserve valid completed metrics and report unavailable ones as N/A.

## Payload flow control and measurement

Each sender reuses a per-stream random 64 KiB payload and starts with a 2 MiB credit window. Each receiver acknowledges cumulative **actually received** payload bytes with `{ack: N}`, normally every 256 KiB. Credit must be monotonic and cannot exceed bytes sent. The sender never has more than 2 MiB of unacknowledged payload per stream; four streams therefore cap credited in-flight data at 8 MiB. Socket/library buffers are additionally bounded and measured. No complete-transfer buffers or files exist.

Acknowledgements include warm-up data because they control flow, not scoring. The receiver independently counts bytes in its shared half-open measurement window after two seconds of warm-up. Ten one-second byte buckets sum to `received_bytes`. Headline Mbit/s is recomputed from receiver bytes and the ten-second duration, not trusted from a peer-provided rate or a sender's enqueue count. Totals are receiver observations through the report snapshot, including warm-up and any tail observed by then; data discarded later during transport cleanup is not billed/wire accounting.

Download: the initiator installs its meter before `go`, samples loaded RTT only in its receiver window, then requests stop and supplies validated receiver statistics. Upload: the responder starts the meter at `go`, announces the measured window, and supplies the result after its window ends. No timestamps from different machines are subtracted.

Loaded upload RTT sampling starts after the receiver's window-start event. A reply counts only when the receiver reports upload-window membership and its remaining time is at least the measured RTT. This conservative bound keeps the sample inside the receiver's window without synchronized clocks. Late/time-out/cross-boundary samples are not median inputs.

Metric fields: `complete`, `measurement_seconds`, `received_bytes`, `warmup_bytes`, `total_bytes`, `bucket_bytes`, and derived `mbps`. Counts are nonnegative safe integers; a valid complete duration equals the negotiated measurement duration; buckets sum to the received count. RTT fields: `samples`, `median_ms`, `min_ms`, `max_ms`, `timeouts`, `discarded`, `timer_limited`.

The browser and Python client use the same window, acknowledgement, direction, and RTT rules. Sender counters are only a sanity bound and never substitute for receiver confirmation. There is no per-test byte quota.

## Expiry and cleanup

The node watchdog validates owner-session lifetime, setup/channel deadlines, control/data liveness, monotonic and UTC discontinuities, and the overall budget. The browser has equivalent measurement/overall guards but no visibility-change behavior. Protocol operations also check the deadline in the input path.

Cancellation stops producers, closes all registered and pending sockets, and does not release the slot until logical transfer work stops. Expired runs abort transports rather than extending the deadline for a polite close. Peer cancellation/logout is best-effort within the remaining cleanup budget; cookie jars are isolated and disposed. The normal auth-session expiry/cap bounds remnants after an abrupt crash.

## Reverse proxy

Preserve Host and the configured origin, overwrite `X-Real-IP`, disable compression and request/response buffering, and allow at least 60 seconds of protocol lifetime. The [nginx example](REVERSE_PROXY.md) uses generic HTTP/WebSocket forwarding; only the application's authenticated channel route accepts an upgrade. Proxy inactivity timeouts are distinct from the app's overall run deadline. The ordinary application JSON request limit remains 1 MiB; no global body-limit or CORS relaxation is necessary. The page CSP includes only its validated `ws`/`wss` origin in addition to `'self'`.
