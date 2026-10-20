# Linux Data Guardian — Verification Report

**Date:** 2026-10-20
**Build tree:** `/home/jagdish/Desktop/Projects/Zara/Linux Data Guardian`
**Target:** Linux Mint 22.3 XFCE (X11), Python 3.12, nftables, NetworkManager
**Status:** source complete and tested; privileged reinstall and deploy pending

This report records what was actually executed and observed. Earlier claims in
`report.md`, `audit.md`, `plan.md`, and `step.md` were not evidence-based and
have been replaced or marked historical.

---

## 1. Automated verification

```text
$ python3 -m pytest -q
124 passed in 4.25s
```

The suite is hermetic: it uses temporary XDG directories, mocks the privileged
boundary, and skips checks that need unavailable kernel features. Coverage:

| Area | Tests | What is asserted |
| :--- | :--- | :--- |
| Quota engine | `test_quota.py` | Empty day is free; zero limit is unlimited; metered-only accounting; allowance clears a block; monthly-only plans; warn-only never blocks; thresholds fire once; DB permissions; orphan recovery; schema version |
| Database | `test_database.py` | Schema v3 tables, session lifecycle, limit/allowance bookkeeping, warning levels, approvals, metered-only monthly totals, frozen metered flag |
| Rulesets | `test_firewall_rules.py` | `destroy table` idempotence, profile rendering, APT-window ordering, quota hard cutoff, interface injection rejection, real `nft -c` parse of balanced/blocked/fallback/cgroup rulesets |
| Helper | `test_firewall_rules.py` | Unknown action, foreign table, `flush ruleset`, `include`, cgroup traversal, invalid PIDs, non-root refusal |
| Daemon | `test_daemon.py` | Startup quota recovery, allowance/profile/hotspot commands, APT timer open/expire, notification cursors, firewall failure surfacing, socket ownership and single-daemon guard |
| IPC | `test_daemon.py` | Round trip, 0600 socket, malformed JSON, handler crash recovery, second server refusal |
| Network | `test_network.py` | Escaped-colon `nmcli` parsing, UUID/name matching, transition callbacks, byte-counter resets, virtual-interface exclusion |
| Config | `test_config.py` | XDG paths, defaults, secure writes, per-network profiles, whitelist values |
| Approvals | `test_approvals.py` | `pip install`/`-m pip` gating, flags with values, read-only pass-through, timeout policy, shim install/remove |
| Installer safety | `test_diagnostics.py` | No APT hook, helper + `visudo -cf`, `SUDO_USER`, no root project Python, emergency script never unlinks the socket, deploy script guards |

## 2. Schema migration (v2 → v3)

A synthetic v2 database containing legacy `daily_usage` quota columns, sessions
without `last_update`, and `network_daily_usage` without `metered` was opened by
the current `Database` code. Observed result:

* `schema_version` = 3
* legacy limit 10 MiB and allowance 5 MiB moved into `quota_state`
* `warning_fired = 1` became warning level `80`
* `quota_exceeded = 1` became `exceeded = 1`, reason `legacy`
* only the designated UUID's network rows received `metered = 1`
* `sessions.last_update` was backfilled from `ended_at`/`started_at`
* all rows preserved; file mode became 0600

The live database was already at v3 at backup time (4 daily rows, 5 per-network
rows, 18 sessions), so the migration path is verified on a controlled copy, not
on live data.

## 3. Runtime observations

* `data-guardian.service` is active; the daemon holds PID 1562.
* Measured RSS during this audit: daemon ≈ 49 MiB, tray client ≈ 129 MiB with
  GTK caches warm. The earlier `< 35 MB` claim was false.
* The runtime database is `~/.local/share/linux-data-guardian/guardian.db`
  (0600) in WAL mode.
* The Unix socket is created under the XDG runtime directory with mode 0600.
* A second daemon refuses to start (flock + live-socket probe).

## 4. Privileged boundary

* Firewall, cgroup, and flush operations go through
  `/usr/local/libexec/data-guardian-helper` only.
* The sudoers file grants exactly that one command, validated with
  `visudo -cf` at install time.
* No APT hook, no root-owned Python import of project code, no wildcard sudo.
* cgroup moves must be performed by the helper: an unprivileged process cannot
  move another process into a delegated cgroup (`Permission denied`), which was
  reproduced directly.

## 5. Deployment and remaining actions

Completed:

* Source committed as `13b19ef`, deploy fix as `30490af`, pushed to `origin/main`.
* The deploy target's `.git` was backed up to
  `/tmp/opencode/ldg-deploy-git-backup/git-dir.tar.gz` and removed (the target
  `HEAD` was an ancestor of `main`, and its three modified files were
  byte-identical to the old build commit).
* `scripts/deploy.sh` synced the runtime tree; the deployed `DEPLOYED` file
  records `commit=30490af`. Runtime directories are byte-identical to the build
  tree, and no caches, docs, `.git`, `apt_hook.py`, or `wifi.png` remain.

Pending (requires the user's sudo password):

1. Install/refresh the helper, sudoers rule, systemd unit, and icon from the
   deployed tree: `sudo "/home/jagdish/Desktop/System Files/Linux Data Guardian/scripts/install.sh" --system`.
2. Verify cgroup creation and strict-mode application on the live hotspot.

No `sudo` command in this report was executed automatically.
