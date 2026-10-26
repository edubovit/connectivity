# Development

[User guides](../README.md) · [Architecture](ARCHITECTURE.md) · [Configuration contract](CONFIGURATION.md) · [API](API.md) · [Speedtest protocol](SPEEDTEST_PROTOCOL.md) · [Frontend](FRONTEND.md) · [Testing](TESTING.md) · [Releases](RELEASING.md)

Use Python **3.14**. Node.js runs JavaScript tests; it is not an application dependency. Browser automation uses a separate optional environment.

## Setup

From the repository root:

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/python -m pip install --no-deps -e .
```

On Windows, create the environment with a Python 3.14 interpreter (`python -m venv .venv`) and substitute `.venv\Scripts\python.exe` in the remaining commands. Activate the environment or use its interpreter explicitly.

Linux needs `iputils-ping`. Integration tests require IPv4/IPv6 loopback, subprocesses and free local ports. Use ignored `.local/` for configs, logs, captures and release staging. Never test against production configuration or install fixture certificates into the OS trust store.

## Working rules

- Reproduce a defect, add a regression, make the smallest clear change, then run applicable suites.
- Preserve security boundaries, measurement semantics, cancellation and configuration atomicity.
- `pyproject.toml` owns direct dependency/build pins and package metadata. `requirements.lock` includes transitive runtime pins; `requirements-dev.lock` adds build/test dependencies; `requirements-browser.lock` isolates browser tooling. These pin versions, not artifact hashes.
- Keep pins consistent and check Python/platform wheel compatibility when updating dependencies. Do not silently install newer runtime versions during deployment.
- Document current supported behavior, not implementation history or future promises. README is for users; operational guides stay short in `docs/`; developer contracts belong here. Keep one authoritative description per topic and repair links when moving it.
