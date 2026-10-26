# Packaging and release verification

[Development](README.md) · [Operational upgrade](../UPGRADING.md)

## Build

From a clean, reviewed revision with Python 3.14:

```sh
python -m pip wheel --no-deps --wheel-dir .local/dist .
```

Build isolation installs the pinned backend from `pyproject.toml`. `--no-build-isolation` requires that backend already installed. Keep direct pins and all lockfiles synchronized; inspect native-wheel availability for target architectures. Do not replace the OS Python or move an existing virtualenv to a new path.

## Acceptance

1. Run applicable unit, integration and browser suites. Check dependency compatibility and whitespace. Do not substitute aggregate pass counts for the actual coverage/limitations.
2. Inspect the wheel: runtime package, static assets, example YAML and metadata only. Compare bundled assets to source; exclude configs, credentials, `.local/`, tests, tools and browser binaries.
3. Install the wheel and locked runtime dependencies in a fresh environment. From outside the checkout, verify import location, console entry point, `python -m connectivity --help`, startup, assets/API, graceful stop and lock release.
4. Validate documentation links, commands, YAML examples and screenshots. README remains the user entry point; no implementation detail or obsolete migration narrative belongs there.
5. Record exact source revision, dirty state and artifact SHA-256 in private release evidence. A package version alone is not artifact identity; do not overwrite immutable release manifests.
6. Review staged files for personal paths, hostnames, secrets and private logs. Publish only reviewed artifacts. Do not invent a license or copyright owner.

## Deployment verification

Follow the [upgrade procedure](../UPGRADING.md). Keep code/runtime root-owned and state privately service-owned. One process owns each config. Preserve production inventory, auth, peer credentials and unrelated services; never overwrite them with an example or stale snapshot.

Verify public HTTPS/login/assets/API/history, cookie scope, loopback listener, actual service UID/hardening and scheduled probe outcomes. A network failure can be real target data; missing ping permissions are a monitor fault. Check effective groups and `ping_group_range` inside systemd rather than weakening the sandbox.

Keep rollback until acceptance. Certificate renewal and its reload hook require their own checks. Do not initiate bandwidth/load tests on production without authorization. Review logs before sharing: priority-only journal filters can miss Python warnings recorded as ordinary stderr, and private targets still need redaction.
