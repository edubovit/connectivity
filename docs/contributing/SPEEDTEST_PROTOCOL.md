# Speedtest protocol 1

[Development](README.md) · [HTTP API](API.md)

The browser and Python peer client share one fixed measurement protocol. Paths below are relative to `<canonical-base>api/v1/speedtest`. Use exact scheme, hostname, port and prefix; no redirects, environment proxies or TLS-verification bypasses.

## Admission and endpoints

One slot per involved node covers incoming, outgoing and browser runs. There is no queue, automatic retry, schedule or probe suppression. Node runs outlive observing tabs. Peer credentials grant full owner access and are held in isolated client sessions.

| Method | Suffix | Contract |
|---|---|---|
| GET | `/capabilities` | `{instance,protocol:1,profile}`; instance changes on restart; self-tests rejected |
| GET | `/peers` | `{revision,peers:[{id,name,url,has_password}]}` |
| PUT | `/peers` | `{peers:[{id,name,url,password?}]}` plus `If-Match`; unchanged URLs can retain omitted passwords |
| POST | `/runs` | `{mode:"browser"}` or `{mode:"node",peer_id}`; accepted run, 202 |
| POST | `/accept` | Peer reservation `{protocol:1,profile,instance,remaining_seconds}`; accepted run, 202 |
| GET | `/state` | Capabilities, active involvement, latest outgoing summaries without detailed buckets |
| GET | `/runs/{id}` | Active or retained detailed result; unknown/expired IDs return 404 |
| DELETE | `/runs/{id}` | Authorized cancellation; committed terminal outcome is immutable |

All endpoints use the [session rules](API.md#session-and-request-boundary). Peers authenticate afresh with their owner password. Peer edits share configuration revisions; edits/deletion of an active outgoing target return 409. Generation guards prevent old callbacks publishing after edit/delete/recreate. Nodes retain the latest outgoing attempt per peer; browser/incoming terminal receipts expire after five seconds.

## Compatible profile

| Field | Value |
|---|---:|
| `version`, `streams` | 1, 4 |
| `warmup_seconds`, `measurement_seconds` | 2, 10 per direction |
| `timeout_seconds`, `setup_seconds`, `channel_seconds` | 60, 10, 5 |
| `idle_seconds`, `idle_samples`, `idle_interval` | 5, 20, 0.1 |
| `loaded_interval`, `rtt_timeout`, `liveness_seconds` | 0.2, 2, 5 |
| `chunk_bytes`, `window_bytes` | 65536, 2097152 per stream |

Profile equality is exact. Production APIs/YAML cannot override it; tests may inject a shortened internal profile. No silent downgrade.

## WebSocket admission

Upgrade `/channel` with the accepted run's owner session and exact Origin. Within five seconds, send at most 4 KiB of JSON:

```json
{"protocol":1,"run_id":"...","csrf":"...","channel":"control"}
```

For data, use `channel:"data"`, `direction:"download"|"upload"`, and `index:0..3`. Only one control channel and one join per direction/index are allowed, during the matching setup phase. Reply: `{type:"ready",protocol:1,run_id,instance}`. Validate both IDs; credentials never go in URLs. Unrelated owner sessions can cancel but cannot join another session's run.

At most eight channels are joined/pending at once. Pending handshakes are tracked for cleanup. Compression is disabled/rejected. Binary payload is exactly 65536 bytes; aiohttp's configured limit is 65537 to allow that boundary. Control messages are bounded to 4 KiB and flow acknowledgements to 128 bytes.

## Control exchange

Requests: `{id,op,data}`. Replies: `{id,data}`. IDs are unique nonnegative safe integers; late replies are ignored and IDs are not reused. Clients allow eight pending operations; the server caps requests at 64/second.

| Operation | Input and effect |
|---|---|
| `echo` | `{nonce}`; echoes nonce, upload-window membership and remaining window time |
| `keepalive` | `{}`; once per second while involved |
| `idle_done` | `{rtt}`; requires a valid summary with samples |
| `prepare` | `{direction}`; download follows idle, upload follows completed download |
| `go` | `{}`; requires four channels; starts senders or receiver meter |
| `stop_download` | `{metric,rtt}`; validates receiver counts against bytes sent, closes data |
| `stop_upload` | `{rtt}`; requires the receiver window to finish; returns receiver metric and closes data |
| `finish` | `{}`; finalizes outcome/metrics and closes the run |

Unknown fields/operations or wrong phases fail. Events are `progress` (phase/live, roughly once/second), `window` (upload start with remaining seconds, then end), and `terminal` (result). Terminal completion can resolve an in-flight finish. Close code 4001 signals cancellation; ordinary early closure is not success.

## Measurement and flow credit

Each stream reuses a random 64 KiB payload and starts with 2 MiB credit. Receivers acknowledge cumulative **actually received** bytes as `{ack:N}`, normally each 256 KiB. Acks must be monotonic and cannot exceed sent bytes. Four streams cap unacknowledged credit at 8 MiB; socket/library buffers are additional. No complete-transfer buffer, file or byte quota exists.

Warm-up counts toward flow control, not measured throughput. The receiver counts the half-open ten-second window into ten one-second buckets. Mbit/s is recomputed from received bytes and duration, never from sender enqueue rate or a peer-supplied rate. Require safe nonnegative counts, complete duration and consistent bucket/total sums. Totals include warm-up and observed tail up to the snapshot; discarded cleanup tail is not wire/billing accounting.

Download installs the initiating receiver's meter before `go`. Upload starts the responding receiver's meter at `go`; its window events bound loaded RTT sampling. No clocks from different machines are subtracted. A loaded-upload echo counts only when the receiver confirms window membership and remaining time covers the measured RTT. Priming, late, timed-out and boundary-crossing samples are not median inputs.

Metric fields: `complete`, `measurement_seconds`, `received_bytes`, `warmup_bytes`, `total_bytes`, `bucket_bytes`, derived `mbps`. RTT fields: `samples`, `median_ms`, `min_ms`, `max_ms`, `timeouts`, `discarded`, `timer_limited`. Missing or unfinished measurements are N/A; cancellation during upload retains completed download.

## Cleanup

The watchdog checks owner lifetime, setup/channel/liveness/phase/overall deadlines and clock discontinuities. Input paths also enforce deadline and run identity. Finalization is bounded, shielded and idempotent; retain the slot until producers and child tasks stop, including cancellation before the outgoing task starts.

Each server socket shares one close task. Interrupt its receive loop before the handshake and keep its request handler alive until close completes. Clean close is not followed by unconditional abort; expired/failed closure aborts. Peer cleanup withstands repeated cancellation, stops readers/senders and always disposes its session/connector. Peer cancellation/logout is best-effort within the remaining cleanup budget.

Browser cancellation records intent before REST and clears it if cancellation is rejected; closing transports first can misclassify the result. Hidden tabs do not automatically cancel tests. UI polling/navigation is separate from run lifetime; see [Frontend](FRONTEND.md).

For deployment, preserve Host/Origin/prefix, WebSocket forwarding and unbuffered/uncompressed traffic as shown in [Reverse proxy](../REVERSE_PROXY.md). Proxy inactivity timeouts and the application deadline are distinct; the JSON body limit is not a transfer quota.
