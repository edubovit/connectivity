# Run as a Linux service

[Guides](README.md) · [HTTPS](REVERSE_PROXY.md) · [Upgrades](UPGRADING.md)

Use a dedicated unprivileged account. This example assumes:

- Installed Python 3.14 environment: `/opt/connectivity/.venv`.
- Private config: `/opt/connectivity/state/config.yml`.
- Service account/group: `connectivity`.

Install the app using the [installation steps](../README.md#install-and-run), adapted to these paths. Keep code and the environment root-owned. Give the service account ownership of `state/` (mode 0750) and `config.yml` (0600). Set the owner password before starting. Do not run a second instance against the same config.

## Unit

Create `/etc/systemd/system/connectivity.service`:

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

```sh
sudo systemd-analyze verify /etc/systemd/system/connectivity.service
sudo systemctl daemon-reload
sudo systemctl enable --now connectivity
sudo systemctl status connectivity --no-pager
sudo journalctl -u connectivity -n 50 --no-pager
```

Check actual dashboard observations, not just the service's active state. Restarting clears history, sessions and speedtest results.

## If ping fails

Install `iputils-ping`. With `NoNewPrivileges`, ping cannot gain file capabilities. Check the service account's groups and `sysctl net.ipv4.ping_group_range`; if needed, allow its actual group through a narrowly scoped persistent sysctl setting. Preserve existing allowed groups. Do not run Connectivity as root or weaken the sandbox.

Verify IPv4 and IPv6 checks under the running service. A shell test outside systemd does not prove the service has permission.
