# Upgrade safely

[Guides](README.md) · [Linux service](SYSTEMD_SERVICE.md)

**Restarting loses history, sessions and speedtest results.** Wait for active speedtests to finish and keep the current configuration.

1. Locate the running installation. On systemd, inspect `systemctl show connectivity -p ExecStart -p WorkingDirectory -p DropInPaths`. Updating an unused checkout changes nothing.
2. Back up the current configuration and service settings in a restricted directory. Retain the previous installed release.
3. Stage the desired source revision in a separate directory. Create its environment with Python 3.14; install that revision's `requirements.lock` and package. Do not move virtualenvs between paths or machines.
4. Test the candidate with an isolated config and another loopback port. Never start it against the live config while the old process owns it.
5. Stop Connectivity, take the final config backup, and switch its launch path to the candidate. Keep the existing production config path. If changing a systemd unit or override, run `systemctl daemon-reload`, then start Connectivity.
6. Verify login, assets, history, real check outcomes and logs. Keep nginx and unrelated services untouched unless their configuration also needs a deliberate change.

To roll back, stop the candidate, restore the previous launch settings and restart the previous release. Restore a configuration backup only if the config itself needs recovery; avoid overwriting newer saved settings. Rollback cannot recover RAM history.

Record the source revision and artifact checksum. The displayed package version alone may not distinguish installations.
