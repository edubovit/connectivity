# Connectivity

Connectivity is a small, self-hosted network monitor with a browser dashboard. It checks whether your websites, servers, and network devices are reachable **from the machine running it**, and shows availability, latency, and recent failures.

One Python process. No database, frontend build, or external service.

## Features

- **Ping, HTTP, TCP, and TLS checks**, each with its own target, interval, and timeout.
- Up to **50 resources and 200 checks**, with independent scheduling.
- Live status, latency charts, certificate expiry, and availability history: one hour of detailed results and fourteen days of minute summaries.
- Add, edit, and reorder resources and checks in the browser. Settings are saved to YAML.
- Manual **browser-to-server and server-to-server speed tests**, measuring download/upload throughput and idle/loaded latency.
- One owner password, with optional HTTPS reverse-proxy support at a root URL or under a path prefix.

**History and sessions live in memory. Restarting clears them.** Connectivity has no alerts, persistent result database, or multi-user accounts. Speed tests can saturate your connection and transfer gigabytes.

## Run

You need **Python 3.14** with pip and venv. Linux also needs `iputils-ping`; Windows and macOS use their system ping tools. Run as a regular user, not root or administrator.

From a checkout, install the app and copy its example configuration:

**Linux / macOS**

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps .
mkdir -p .local
chmod 700 .local
cp connectivity/example.yml .local/config.yml
chmod 600 .local/config.yml
```

**Windows PowerShell**

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps .
New-Item -ItemType Directory -Force .local
Copy-Item connectivity\example.yml .local\config.yml
```

On Windows, restrict the config file and its directory to your account using file permissions.

Edit `.local/config.yml` and replace `CHANGE_ME_BEFORE_STARTING` with a strong, unique password. Then start the app:

```sh
.venv/bin/python -m connectivity --config .local/config.yml
```

On Windows:

```powershell
.\.venv\Scripts\python.exe -m connectivity --config .local/config.yml
```

Open **http://127.0.0.1:8080**, sign in, and add your first resource. Stop with Ctrl+C.

## Configure

The minimal configuration is:

```yaml
version: 1
server:
  host: 127.0.0.1
  port: 8080
  allowed_hosts: []
auth:
  password: "CHANGE_ME_BEFORE_STARTING"
ui:
  refresh_interval_seconds: 15
resources: []
```

- **Checks:** use the dashboard to add a resource and its checks. Ping needs a host; TCP/TLS need a host and port; HTTP needs a URL, method, and expected status. Default interval is 30 seconds, timeout 5 seconds. HTTP does not follow redirects.
- **Status:** a failed check makes its resource offline; missing or invalid observations are unknown, not downtime. Availability excludes unknown time; coverage shows how much of the selected range was observed.
- **Saved changes:** browser edits persist automatically when you save. Renames and reordering preserve history; measurement changes reset affected history. Stop the app before manually editing YAML, then restart.
- **Speedtest peers:** open Speedtest → Configure peers and enter another Connectivity node's canonical URL and owner password. That password grants full owner access, not just speed testing.
- **Network access:** the default listener is local-only. For a trusted LAN, choose a private interface and add the exact browser-facing DNS name to `allowed_hosts`; restrict access with your firewall. For HTTPS, keep the backend on loopback and follow the [reverse-proxy guide](docs/REVERSE_PROXY.md).

**Keep this a private tool.** Passwords are stored in plaintext in the restricted YAML; direct HTTP does not encrypt passwords or cookies. Do not expose the bare HTTP listener to the internet. HTTPS protects transport, but does not add access restrictions or multiple users.

For development, testing, configuration limits, and service deployment, see [CONTRIBUTING.md](CONTRIBUTING.md).
