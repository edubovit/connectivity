# Tests and fixtures

[Development](README.md)

Run from the repository root with the development environment's Python:

```sh
python -m pytest -q
node --test tests/*.test.cjs
python -m pip check
git diff --check
```

| Tests | Coverage |
|---|---|
| `test_config.py` | Schema, locks, atomic writes, permissions, conflicts |
| `test_probes.py` | Controlled HTTP/TLS/TCP/IPv4+IPv6 ping, parsing, DNS, cancellation |
| `test_history_engine.py` | Cadence, freshness, clock gaps, generations, retention and aggregation |
| `test_api.py`, `test_proxy.py` | Sessions, CSRF/Host, body limits, APIs and HTTPS-prefix boundary |
| `test_cli.py` | Real process startup/save/restart/shutdown and lock exclusion |
| `test_speedtest.py`, `test_speed_cleanup.py` | Metering, TLS/auth, admission, channels, pre-start/repeated cancellation and disposal |
| `*.test.cjs` | Charts, ordering, selects, browser metering/cancellation and polling lifecycle |

Use controlled loopback fixtures and fake credentials. Do not skip native ping/filesystem failures or use public sites as deterministic test truth. State platform gaps explicitly; WebKit automation is not native Safari/device certification.

## Browser environment

```sh
python -m venv .local/browser-venv
.local/browser-venv/bin/python -m pip install -r requirements-browser.lock
.local/browser-venv/bin/python -m playwright install chromium firefox webkit
.local/browser-venv/bin/python tools/ui_browser_check.py
.local/browser-venv/bin/python tools/speed_ui_lifecycle_check.py --browsers chromium firefox webkit
```

On Windows use `.local\browser-venv\Scripts\python.exe`. Linux may need Playwright's platform libraries. Both checkers start isolated fixtures using `.venv`; `--python PATH` overrides the application interpreter. `--browsers` selects engines and `--report` selects the output JSON. Reports/screenshots belong under `.local/`.

The UI suite checks real probe types, editors, validation, filters, rolling ranges, touch/keyboard ordering, history/detail state, conflicts, failure recovery, peer secrets, transfers, cancellation and session revocation. `--skip-transfers` omits completed transfers. Full tests keep production profile durations and receiver validation but pace fixture binary frames by 0.0625 seconds to bound browser recorder overhead; reports disclose that delay. This is a functional check, not capacity measurement. Do not extend production deadlines to compensate for instrumentation.

The lifecycle checker uses real login/navigation and controlled speedtest GET responses, without transfers. It tests pending polls across reload, cancelled/failed navigation, hidden tabs and synthetic persisted restoration. Page errors remain failures; synthetic restoration is not a real BFCache audit.

HTTPS-prefix UI tests bridge an intercepted HTTPS origin and its cookie jar to a loopback backend. They verify paths, cookie flags, assets and navigation, **not** actual nginx/TLS termination. Verify deployed HTTPS separately. Layout/accessibility-name checks are not a complete accessibility audit.

## Interactive fixtures

```sh
python tools/devserver.py --config .local/browser.yml --port 8087
python tools/speedtest_fixture.py --port 18090 --peer-port 18091 --duration 900
```

Run separately, not as detached untracked processes. Stop with Ctrl+C and verify children/listeners exit.

- Devserver: app on 8087, HTTP fixture on 8091 (`/ok`, `/unhealthy`, `/redirect`, `/hanging`), process-trusted TLS on 8092. Existing fixture config is preserved; missing config is initialized empty. Optional `--speedtest-frame-delay` accepts 0–1 second, default 0.
- Speedtest fixture: two compatible nodes with temporary configs, configured peers and monitoring heartbeats. They share one process; results are not cross-host capacity evidence.
- Both use `test-only-password`. Never install fixture CA trust into the OS.

For unpaced, explicitly authorized fixture transfers:

```sh
.local/browser-venv/bin/python tools/speedtest_browser_check.py --url http://127.0.0.1:18090/ --browsers firefox webkit --report .local/speed-browser.json
```

The explicit URL is intentional; the checker has a different default port. `CONNECTIVITY_TEST_PASSWORD` overrides the fixture password. Transfers can move gigabytes. Network recording can itself delay handshakes under multi-gigabit loopback traffic; compare with an unrecorded run before blaming the link or weakening protocol checks.

## Memory and load

```sh
python tools/measure_history.py
python tools/benchmark.py --seconds 180 --report .local/benchmark.json
python tools/benchmark.py --seconds 180 --report .local/coexistence.json --speedtest-fixture-url http://127.0.0.1:18090/ --speedtest-runs 3
```

The storage tool measures a conservative owned-history graph against 350 MiB, not process RSS. The benchmark exercises 50 resources × four checks at one-second intervals with API polling; the backend target is below 512 MiB and must be measured. Record runtime/platform, workload, process and child memory, CPU, scheduler skips, query latency and monitor notices. Synthetic retention is not a fourteen-day soak. Coexistence runs require the speed fixture and intentionally generate load; never treat them as production health checks.
