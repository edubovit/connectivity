# Configuration contract

[Development](README.md) · [User configuration](../CONFIGURATION.md)

`config.load(bytes)` parses and validates without acquiring ownership. `ConfigStore(path)` locks the file, opens it writable and probes permission-preserving atomic replacement. Never instantiate another store against live production configuration.

## Schema

Only version 1 is accepted. YAML is one safe document with unique string keys: no aliases, custom tags, unknown fields or nonfinite values. YAML and ordinary JSON requests are bounded to 1 MiB.

| Field | Constraint / default |
|---|---|
| `version` | Required integer `1` |
| `auth.password` | Required nonblank secret, at most 1024 UTF-8 bytes; example placeholder rejected |
| `server.host` | Literal bind IP; `127.0.0.1` |
| `server.port` | Integer 1–65535; `8080` |
| `server.allowed_hosts` | At most 100 exact additional DNS names for direct HTTP; empty |
| `server.public_url` | Optional canonical HTTPS URL; requires loopback binding; no credentials, query or fragment; plain path segments |
| `ui.refresh_interval_seconds` | Integer 1–300; `30`; explicit saved values remain unchanged |
| `resources` | Required list, at most 50 resources and 200 total checks; empty allowed |
| Resource `id`, `name`, `checks` | Required; 1–20 checks; resource IDs and case-insensitive names unique |
| Check `id`, `name`, `type` | Required; globally unique ID, name unique within resource; `ping`, `http`, `tcp`, `tls` |
| Check `interval_seconds` | Finite 1–86400; `30` |
| Check `timeout_seconds` | Finite 0.1–300; `5`; may exceed the interval |
| `speedtest.peers` | At most 50 peers; defaults empty; separate from probe capacity |
| Peer `id`, `name`, `url`, `password` | Required in YAML; unique IDs, names and canonical URLs |

IDs contain 1–64 ASCII letters/digits/underscores/hyphens and start with a letter or digit. Names are trimmed nonblank strings of at most 100 characters, without control characters. Host fields accept DNS names or IP literals, not URLs or command arguments.

| Check | Additional required fields |
|---|---|
| `ping` | `host` |
| `tcp`, `tls` | `host`, integer `port` 1–65535 |
| `http` | `url`, `method`, integer `expected_status` 200–599 |

HTTP methods: GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS. A configured error status can be a success. Peer URLs are canonical HTTP/HTTPS base URLs without embedded credentials; HTTPS verification cannot be disabled. Peer passwords grant full owner access.

## Save transaction

1. Validate the candidate and revision. Missing `If-Match` → 428; stale revision → 412.
2. Check the on-disk fingerprint (identity, metadata and content). External change/replacement/deletion → 409, without overwriting it.
3. Write normalized YAML to a same-directory temporary file. Preserve ownership/mode and supported ACL/security labels, flush/fsync and atomically replace.
4. Verify the committed snapshot, reconcile runtime state and publish the revision.

Pre-commit failure leaves active settings/history intact. An unverifiable or unactivatable committed save stops the process instead of keeping mismatched file/runtime state. Comments and original formatting are not preserved.

Both file and parent directory must be writable by the service account. Use a regular local file, not a symlink or network filesystem. The sidecar `.lockfile` may remain after shutdown; actual ownership is the OS advisory lock, not file existence.

Resource/UI saves preserve hidden auth/server and peer sections. Peer reads expose `has_password`, not passwords. Omitting an existing peer password preserves it only if its canonical URL is unchanged. Active outgoing target edits/deletion are rejected.

Renames/reordering preserve histories, generations, cadence and active work. Measurement changes reset the affected check and resource aggregate; add/delete resets the resource aggregate while unrelated surviving check histories remain. Existing check type and parent resource cannot change in place. Peer generations reject late callbacks after edit/delete/recreate.

There is no YAML watcher. Manual edits and password/listener/public-URL changes require stop/edit/restart. Do not race manual edits with browser saves.
