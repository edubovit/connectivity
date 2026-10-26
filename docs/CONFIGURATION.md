# Configuration

[Home](../README.md) · [Guides](README.md)

Start from [example.yml](../connectivity/example.yml). Set a unique password before running. The dashboard manages resources, checks, refresh settings and speedtest peers; use YAML for the listener and owner password.

```yaml
version: 1
server:
  host: 127.0.0.1
  port: 8080
  allowed_hosts: []
auth:
  password: "CHANGE_ME_BEFORE_STARTING"
ui:
  refresh_interval_seconds: 30
resources: []
```

The placeholder is rejected. Keep the file private and writable by the account running Connectivity; that account also needs write access to its directory. **Stop before manual edits.** Browser saves may replace YAML formatting and comments.

## Add checks

Select **Add resource**, name it, then add one or more checks.

| Check | Enter | Success means |
|---|---|---|
| Ping | Hostname or IP | An echo reply arrives |
| HTTP | URL, method, expected status | The response has that status; redirects are not followed |
| TCP | Host and port | A connection opens |
| TLS | Host and port | The TLS handshake and certificate validation succeed |

Defaults: interval **30 seconds**, timeout **5 seconds**. HTTP methods other than GET can modify the target; choose deliberately. Capacity is 50 resources, 200 total checks, and 20 checks per resource.

One failed check makes a resource offline. Unknown means no valid fresh observation, not necessarily downtime. Changing a check's measurement settings resets its affected history; renaming and reordering do not.

## Settings and peers

- **Settings:** dashboard refresh accepts 1–300 seconds. Existing saved intervals are preserved when the default changes. This does not alter probe schedules.
- **Custom history:** enter a positive number of hours or days, including fractions, up to fourteen days. The window moves with each refresh; a day means 24 elapsed hours.
- **Speedtest → Configure peers:** use the peer's exact base URL, including port and path prefix. Leave an existing password blank to keep it when the URL is unchanged. A changed URL requires its password again.
- A node accepts one speedtest at a time. Node-to-node tests continue when the observing tab closes; browser tests belong to their tab. An interrupted upload does not erase a completed download.

## Network access

The default is local-only. For trusted-LAN HTTP access, bind a private interface address and add the browser-facing DNS name to `server.allowed_hosts`; restrict access with the firewall. HTTP remains unencrypted.

For HTTPS, keep the backend on loopback and follow [Reverse proxy](REVERSE_PROXY.md). Changing the listener, password or public URL requires a restart, which clears in-memory data and sessions.
