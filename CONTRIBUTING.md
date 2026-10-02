# Development and maintenance

This guide covers setup, architecture, behavioral contracts, tests, packaging, and safe operation. Keep changes small and readable. Fix the demonstrated problem, add a regression test, and avoid unrelated refactors.

## Development environment

Use Python **3.13**. The CLI and package metadata intentionally reject other minor versions. Node.js is optional for JavaScript tests and syntax checks; it is not a runtime dependency. Browser automation uses a separate optional Playwright environment.

Create and activate a virtual environment from the repository root:

```sh
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
```

On Windows PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

The remaining commands use `python` from the activated environment. On Windows you can instead use `.\.venv\Scripts\python.exe` explicitly. Linux needs the distribution's `iputils-ping` package. Tests require working IPv4 and IPv6 loopback, subprocesses, and free local ports.

Dependency files:

| File | Purpose |
|---|---|
| `pyproject.toml` | Python range, direct runtime/test dependencies, build backend, console entry point, bundled assets |
| `requirements.lock` | Exact runtime versions, including transitive dependencies |
| `requirements-dev.lock` | Runtime lock plus test and build tools |
| `requirements-browser.lock` | Optional browser tooling, isolated from the runtime |

These are pip-compatible version pins, not hash-verified lockfiles. Keep direct pins and lockfiles consistent when updating a dependency. Review upstream release notes, platform wheels, Python compatibility, and security changes; test both source and installed packages. Do not silently resolve newer runtime versions during a deployment.

Store working configs, logs, screenshots, generated reports, release staging, and personal notes in ignored `.local/`. Never use a production config for tests. The example placeholder is deliberately invalid; test fixtures use clearly fake passwords.

## Code map

| Location | Responsibility |
|---|---|
| `connectivity/__main__.py` | CLI, mandatory config path, signals, listener and process lifecycle |
| `connectivity/config.py` | Strict YAML/schema validation, file lock, revisions, conflict detection, permission-preserving atomic saves |
| `connectivity/probes.py` | Bounded DNS resolver, OS ping, HTTP/TCP/TLS probes, result/error codes and subprocess cleanup |
| `connectivity/engine.py` | Independent scheduling, freshness, resource aggregation, configuration reconciliation, clock/gap handling |
| `connectivity/history.py` | Compact NumPy-backed history, retention, aggregation, snapshots and queries |
| `connectivity/web.py` | aiohttp app, authentication, Host/Origin/CSRF checks, JSON APIs, history work limits and static assets |
| `connectivity/speed_protocol.py` | Fixed transfer profile, receiver meters, RTT validation and flow credit |
| `connectivity/speedtest.py` | Single-slot run lifecycle, peer generations, cancellation, finalization and expiry |
| `connectivity/speed_client.py` | Outgoing node-to-node authentication, channels and measurement |
| `connectivity/speed_web.py` | Speedtest HTTP endpoints and authenticated WebSocket admission |
| `connectivity/static/app.js` | Dashboard, config editors, navigation and polling |
| `connectivity/static/latency-chart.js`, `drag-order.js` | Interactive charts and accessible reordering |
| `connectivity/static/speed-browser.js`, `speed-ui.js` | Browser measurement/transport and speedtest UI |
| `tests/` | Deterministic unit tests and isolated network/process integration tests |
| `tools/` | Reusable local fixtures, browser checks, memory and load measurements |

Runtime is one asyncio process with one web worker. There is no database, external scheduler, frontend build, CDN, plugin system, automatic YAML watcher, or distributed history. One process must own each config file. Do not deploy multiple web workers against the same state.

Public contracts: [JSON API](docs/API.md), [speedtest protocol](docs/SPEEDTEST_PROTOCOL.md), [HTTPS reverse proxy](docs/REVERSE_PROXY.md). Update these with behavior changes; they are not substitutes for tests.

## Configuration contract

`connectivity.config.load(bytes)` parses and validates without taking a lock or starting probes. `ConfigStore(path)` is different: it acquires the lock, opens the file for writing, and tests atomic replacement. Do not instantiate it against a config already owned by a running app.

Only version 1 is accepted. YAML must be one safe document with unique string keys: no aliases, custom tags, unknown schema fields, or non-finite numbers. The file and JSON request limit is 1 MiB.

| Setting | Contract |
|---|---|
| `version` | Required integer `1` |
| `auth.password` | Required nonblank string, at most 1024 UTF-8 bytes; example placeholder rejected |
| `server.host` | Literal bind IP, default `127.0.0.1` |
| `server.port` | Integer 1–65535, default 8080 |
| `server.allowed_hosts` | Up to 100 exact additional DNS names for direct HTTP mode; not wildcard patterns |
| `server.public_url` | Optional canonical HTTPS base URL; requires loopback binding; no credentials, query or fragment; plain path segments only |
| `ui.refresh_interval_seconds` | Integer 1–300, default 15 |
| `resources` | Required list, may be empty; at most 50 resources and 200 total checks |
| Resource `id`, `name`, `checks` | Required; 1–20 checks per resource; unique resource IDs and case-insensitive names |
| Check `id`, `name`, `type` | Required; globally unique check IDs, names unique within a resource, one of `ping`, `http`, `tcp`, `tls` |
| Check `interval_seconds` | Finite 1–86400, default 30 |
| Check `timeout_seconds` | Finite 0.1–300, default 5; may exceed the interval |
| `speedtest.peers` | Optional, defaults empty; at most 50, separate from check capacity |
| Peer `id`, `name`, `url`, `password` | Required in YAML; unique IDs, names and canonical URLs; peer owner password is a full-access secret |

IDs are 1–64 ASCII letters, digits, underscores or hyphens, starting with a letter/digit. Names are trimmed, nonblank, at most 100 characters, with no control characters. Host fields accept validated DNS names or IP literals, not URLs or command arguments.

Check-specific fields:

| Type | Required target/settings | Meaning of successful latency |
|---|---|---|
| `ping` | `host` | OS-reported echo RTT, possibly an approximate upper bound |
| `tcp` | `host`, integer `port` | TCP connect time, excluding DNS |
| `tls` | `host`, integer `port` | Validated TLS handshake time, excluding DNS/TCP |
| `http` | `url`, `method`, integer `expected_status` | Time to final response headers, including DNS/setup |

HTTP methods: GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS. Expected status is 200–599. A configured error status can be success. Requests send no body, custom headers or cookies and follow no redirects; non-GET methods can still change the target. TLS always uses system trust and hostname/date validation. Certificate expiry is displayed, not a configurable warning threshold.

### Persistence and reconciliation

- Normal edits go through revision-checked APIs; missing `If-Match` is 428, stale revision 412, and externally changed files 409. Browser saves normalize YAML; comments/formatting are not retained.
- Writes preserve hidden auth/server settings and unrelated peer settings. Peer reads return only `has_password`, never stored passwords. Omitted peer password preserves the existing one only when the URL is unchanged.
- Save to a temporary file in the same directory, preserve owner/group/mode and supported ACL/security labels, flush/fsync, atomically replace, then activate. Failed pre-commit saves leave active config/history unchanged. A committed-but-unverifiable/unactivatable save stops the app rather than running mismatched state.
- File **and parent directory** must be writable by the service user. Use a regular local file, not a symlink or network filesystem. The empty sidecar `.lockfile` may remain after shutdown; lock ownership is advisory OS state, not file existence.
- Renames and reordering preserve history, IDs, cadence and active work. Measurement edits reset the affected check and resource aggregate; add/delete resets the resource aggregate. Surviving unrelated check histories remain. A check cannot move resources or change type in place.
- Stop before manual YAML edits; restart afterward. Password/listener/public URL changes require restart. Do not edit concurrently with a browser save.

## Monitoring and history invariants

- Each check has an independent cadence; executions of the same check never overlap. Missed starts are skipped, not queued or retried. Concurrency is bounded at 200 checks.
- A result takes effect at completion. Freshness ends after the next cadence slot following completion, plus that check's timeout and two seconds. A pending execution does not erase a fresh prior result.
- `success` means a valid successful observation; `failure` means an attempted network check failed; `error` means the monitor could not make a valid observation. Missing tools, permissions, unparseable ping output, or DNS-worker saturation must not become invented downtime.
- Resource state is failure-first: any fresh failed check → offline; otherwise any unknown check → unknown; otherwise online.
- Availability is online seconds divided by online + offline seconds. Coverage measures observed time; unknown is neither uptime nor downtime. Current state is independent of the selected history range.
- One hour of raw results and fourteen days of UTC-minute summaries are held in RAM. No automatic backups, result logs, exports, pause controls or notifications. Restart discards history and sessions.
- Timeline colors indicate the worst state present in a window: downtime wins, then unobserved time, then online. Colors are not duration proportions. Partial older minutes are estimates and labeled. Failure/error samples do not become zero or timeout-valued latency points; missing samples stay gaps.
- Monotonic time controls scheduling/durations. Wall-clock discontinuities clear history; monitor gaps invalidate old observations and infer unknown time. Preserve generation checks so late work from a deleted/reset check cannot publish results.
- DNS uses the OS resolver with at most 32 outstanding calls and no app cache. Timed-out OS calls may remain running until the OS returns; their bounded daemon workers remain occupied. There is no automatic alternate-address retry or environment-proxy use.
- Ping is a shell-free, bounded-output OS subprocess. Test matching reply addresses, IPv4/IPv6 scopes, compressed trailing `::` with punctuation, localized output, `<1 ms` bounds, timeouts and kill/reap paths. Do not force IPv4 to hide a parser defect.
- History queries snapshot one coherent generation and run bounded calculations off the event loop. At most two expensive queries execute concurrently; excess requests get 429. Keep response sizes, error buffers, raw pagination and retention cleanup bounded.

## Authentication and speedtest invariants

- All data APIs require the owner session. Mutations require JSON, exact Origin and CSRF; login requires Origin but has no prior CSRF token. Preserve Host validation even on wildcard binds.
- Sessions are RAM-only, expire absolutely after 24 hours and have a cap of 20. Login failures are limited per source and globally. Cookies are host-only/HttpOnly/SameSite=Strict; explicit HTTPS proxy mode adds Secure and the configured path.
- Do not trust forwarded Host/protocol headers. Proxy-mode X-Real-IP is accepted only inside the explicit trusted loopback boundary; nginx must overwrite it. Never add permissive CORS or a TLS-verification bypass.
- Do not log credentials, cookies, CSRF tokens, private config or raw credential-bearing exceptions. Fixture certificates belong in the test process's TLS context, never the OS trust store.
- One manual speedtest may involve a node at a time, whether browser, outgoing or incoming. No queue, scheduling, retries, hidden-tab policy, or monitoring pause/suppression. A node-to-node test does not depend on the browser remaining open.
- The fixed compatible profile uses four uncompressed streams, two seconds of warm-up and ten measured seconds per direction within a 60-second deadline. Receiver-counted bytes determine throughput; validate complete windows, bucket totals and safe counters. RTT is application echo, not ICMP. Never subtract clocks from different machines.
- Preserve bounded credit/backpressure, run/session/Origin/channel binding, pending-socket cleanup and no redirects. Peer passwords grant full owner access and must remain private.
- Cancellation/finalization must be shielded, bounded and idempotent. Do not release the slot before transfer work stops or rewrite an already committed terminal result. Preserve completed metrics on failure/cancel; incomplete values are N/A. Guard peer delete/recreate generations against late publication.
- Browser cancellation records local intent before REST and clears it on rejection. Closing transports before the cancellation request can race the handshake and falsely report connection loss. Keep the Python and JavaScript regression tests for this behavior.

## Automated tests

Run from the repository root:

```sh
python -m pytest -q
node --test tests/latency_chart.test.cjs tests/drag_order.test.cjs tests/speed_browser.test.cjs
python -m pip check
git diff --check
```

| Suite | Main coverage |
|---|---|
| `test_config.py` | Validation, lock conflicts, atomic writes, permissions, external edits |
| `test_probes.py` | Real local HTTP/TLS/TCP/IPv4+IPv6 ping; parser, resolver and cleanup failures |
| `test_history_engine.py` | Scheduling/freshness, edits, clock/gap handling, retention and numeric queries |
| `test_api.py`, `test_proxy.py` | Auth/CSRF/Host, JSON limits, conflict semantics, API/history and prefix routing |
| `test_cli.py` | Real child-process startup/save/restart, lock exclusion and graceful shutdown |
| `test_speedtest.py` | Metering, peer auth/TLS/redirects, admission, channels, cancellation and cleanup |
| `*.test.cjs` | Pure chart/reordering helpers and browser speed metering/cancellation |

Use a focused failing test to reproduce a bug, then run the full suites. Integration tests use controlled loopback fixtures; external websites are not test truth. Do not skip real ping or native filesystem failures to make a platform appear supported. Explain environmental limitations separately.

On hardened Linux services, a test pass as the right user **outside systemd** does not prove in-service ICMP works. Test both address families under effective unit restrictions and inspect real scheduled results after installation.

## Interactive fixtures and browser checks

Create `.local/` before tools that write reports. Run fixtures in a separate terminal; stop with Ctrl+C and verify their listeners/children exit. Do not detach a process without tracking its PID and cleanup. Tests manage their own temporary processes.

Dashboard fixture:

```sh
python tools/devserver.py --config .local/browser.yml --port 8087
```

It serves the dashboard on loopback8087, controlled HTTP on8091 (`/ok`, `/unhealthy`, `/redirect`, `/hanging`) and test TLS on8092. It initializes an empty config if missing, with password `test-only-password`; existing fixture config is preserved. Fixture CA trust is process-local.

Two speedtest nodes:

```sh
python tools/speedtest_fixture.py --port 18090 --peer-port 18091 --duration 900
```

Both use `test-only-password`, temporary configs, monitoring heartbeats and a configured peer. They share one process: useful for functional tests, **not** a cross-host capacity claim.

For optional browser automation, create a separate `.local/browser-venv`, install `requirements-browser.lock` into it, then install the required browsers with that environment's `python -m playwright install firefox webkit`. Linux may require additional system browser libraries; follow Playwright's platform instructions rather than changing the app runtime. Run:

```sh
python tools/speedtest_browser_check.py --url http://127.0.0.1:18090/ --browsers firefox webkit --report .local/browser-results.json
```

Here `python` must be the browser environment's interpreter. The explicit URL matches the fixture above; do not rely on the browser checker's different default port. An optional `CONNECTIVITY_TEST_PASSWORD` environment variable overrides the fake fixture password. Only target authorized test nodes: this checker runs full transfers and can move gigabytes.

Manual browser checklist for UI changes:

- Login/logout, stale/expired sessions, keyboard and mobile/touch layout; no script errors, secrets or third-party asset requests.
- Add/edit/delete all check types, expected failures, input escaping, empty state, filters, details, charts and recent results.
- Reordering by mouse/touch/keyboard; cancellation, stale revisions, save failures and refresh while arranging.
- Rolling hour/day/fourteen-day ranges, custom browser-local times and DST gaps/overlaps; missing data remains unknown.
- Offline/reconnect behavior; no overlapping polling or browser-driven reset of backend generations.
- Direct HTTP plus HTTPS root and prefix routing; non-default public port, correct cookies, Host/Origin/CSRF rejection.
- Speedtest success, immediate and mid-transfer cancel, unavailable phases, reload/logout, peer edit/redaction and busy conflicts. Wait for authenticated UI, not an enabled button in a hidden panel.

## Memory and performance checks

```sh
python tools/measure_history.py
python tools/benchmark.py --seconds 180 --report .local/benchmark.json
```

The storage tool checks a conservative owned-history graph against a 350 MiB budget. This is not whole-process memory. The load tool prepopulates retained history and runs 50 resources × four checks at one-second intervals, controlled failures and normal API polling. The design target is a backend below512 MiB at this load, but must be measured on the actual platform.

Optional coexistence run, while the controlled speed fixture is running:

```sh
python tools/benchmark.py --seconds 180 --report .local/coexistence.json --speedtest-fixture-url http://127.0.0.1:18090/ --speedtest-runs 3
```

This is an explicit high-load test, not a deployment health check. Record OS/runtime, dependency versions, workload, sample counts, poll/query settings, backend RSS/high-water, ping-child memory, browser/peer memory separately, status/chart latency, scheduling delays, skipped starts and monitor notices. A sampler missing brief ping children is not evidence of zero child memory. Synthetic retention is not a fourteen-day real-time soak, and localhost throughput is not network capacity.

Native Linux and Windows tests are available; native macOS/Safari, broader locale/ACL cases and physical suspend/clock-change behavior require independent verification. A WebKit automation pass is not a native Safari pass. Report untested cases and stress-induced scheduling gaps honestly.

## Packaging and release checks

Build from a clean, reviewed tree using Python3.13:

```sh
python -m pip wheel --no-deps --wheel-dir .local/dist .
```

Normal build isolation installs the pinned backend from `pyproject.toml`. `--no-build-isolation` requires the build backend already installed and is not a fix for missing build dependencies. Do not package the whole working directory with local configs/reports.

Before a release:

1. Run all applicable Python/JS tests and native platform checks. Verify dependency pins and `pip check`.
2. Inspect wheel contents: only runtime package, bundled static assets/example YAML and package metadata; no tests, personal paths, configs, reports, browser binaries or private material. The example password stays a placeholder.
3. Install the wheel and locked dependencies in a fresh environment. From **outside the checkout**, check import location, console entry point and `python -m connectivity`; start with a new private fixture config and verify root/assets/API plus graceful stop/lock release.
4. Check README rendering and relative links; keep API/protocol/proxy docs in sync. For a package metadata change, consider whether the version needs a bump.
5. Record exact artifact SHA256 and source commit/dirty state in private release evidence. Do not identify different wheels by version alone or rewrite an immutable manifest after later commits.
6. Review `git status`, staged diff and tracked file list for credentials, local accounts/paths, personal hostnames, reports and old private history. Ignore rules do not remove files already tracked. Sanitize author/committer metadata when anonymity is required.
7. Publish only the reviewed commit/artifact. Never attach `.local`, private backups, virtualenvs or the raw workspace. Publishing a repository and selecting a software license are separate decisions; do not invent a license or copyright holder.

## Service deployment and maintenance

Run under a dedicated unprivileged user, one process per config, with root-owned code/runtime and private service-owned state. Install Python3.13 and native locked dependencies without replacing the OS Python. Virtualenvs are not portable across machines, paths or architectures.

Example systemd unit (adapt paths and account before use):

```ini
[Unit]
Description=Connectivity network monitor
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
User=connectivity
Group=connectivity
WorkingDirectory=/opt/connectivity
ExecStart=/opt/connectivity/.venv/bin/python -m connectivity --config /opt/connectivity/state/config.yml
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=5
TimeoutStopSec=10
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths=/opt/connectivity/state

[Install]
WantedBy=multi-user.target
```

Use state directory0750/config0600 owned by that service user/group. Ensure the service can preserve file ownership/ACLs during atomic saves. Validate the unit on the target before enabling it; do not blindly apply a whole-host provisioning script.

**Ping under Linux hardening:** NoNewPrivileges blocks gaining the ping executable's file capability. Inspect the service's effective groups and `net.ipv4.ping_group_range`. If the range excludes the service group, review a persistent narrowly scoped grant under `/etc/sysctl.d/` and apply only that file. Do not copy another machine's numeric GID, overwrite an existing permitted group range, run as root, weaken the sandbox, or give Python raw-socket capabilities just to enable echo requests. Verify actual scheduled pings, not merely result counts.

For Windows, use an unprivileged Task Scheduler account, startup trigger, absolute interpreter/config paths, correct working directory, restart-on-failure and no overlapping instances or time limit. Hard termination may skip graceful cleanup. On macOS, use launchd with absolute program/config paths, working directory, restart-on-failure and a stop timeout. No native service wrapper is bundled; test lifecycle on the target OS.

For HTTPS use the [proxy guide](docs/REVERSE_PROXY.md). Before touching an existing host, inventory ports/sites/certificates and unrelated services. Preserve other workloads and renewal/routing rules. Keep a reviewed rollback of affected code/unit/proxy config; secret-bearing backups stay restricted on the host.

Upgrade checklist:

1. Verify artifact hashes, Python/dependency compatibility and available memory/disk. Test the candidate in isolation.
2. Confirm no active speedtest; explain the loss of RAM history/results/sessions on restart. Preserve current config rather than replacing it with an old example or migration snapshot.
3. Switch only the intended app. Validate proxy config before graceful reload, then poll readiness; nginx reload is asynchronous.
4. Verify public HTTPS and login/assets/API/history, cookie scope, loopback listener, service UID/hardening, configuration identity and unrelated workloads.
5. Inspect actual scheduled probe outcomes, especially ping permissions, plus logs, skips/notices and resource usage. External network failures are data, not automatically an application defect.
6. Test WebSockets/cancellation if needed; do not run full bandwidth/load tests on production without explicit approval. Verify certificate renewal and its reload hook separately.
7. Retain rollback until acceptance, then remove only owned staging artifacts. A rollback cannot restore lost RAM history.

### Troubleshooting

| Symptom | Check |
|---|---|
| Startup refused | Python minor version, valid non-placeholder YAML, regular file, file/parent ownership, another process holding its lock |
| Host/Origin403 | Exact public hostname **and port**, prefix, loopback proxy mode, preserved Host/Origin; do not disable validation |
| Login works but app path fails | Proxy stripped the URI prefix, wrong cookie path, or stale canonical URL |
| Config save conflict | Stale revision or external file change; reload/reconcile deliberately, not blind retries |
| `ping_permission` / `ping_output` | Effective service sandbox/groups/ping policy, trusted executable and real OS output/address parsing |
| `resolver_busy` | Saturated outstanding OS DNS calls; check resolver startup/reachability, not just process liveness |
| Clock reset / monitor gap | Time synchronization, suspend or blocked event loop; do not label unobserved time as online |
| Speedtest stuck/failed | Canonical peer URL, owner credentials, TLS, exact profile, WebSocket forwarding, node busy state and overall deadline |
| Poor throughput or scheduler skips | CPU/proxy/route/browser contention, query concurrency, actual measurement load and separate process memory |

Logs avoid per-observation targets and secrets, but review diagnostics before sharing them. A journal priority filter alone can miss Python WARNING/ERROR text recorded as ordinary stderr. Redact private hostnames, paths and all authentication material in public issues.
