# Linux Data Guardian — Implementation Steps (historical, superseded)

This was a phase-1 roadmap tied to the obsolete `7caf94d` audit. It is kept only
as a record; the current, evidence-based sequence lives in `ImprovePlan.md`.

## Completed in the current rewrite

1. Privileged helper + single sudoers rule; no APT hook, no root Python.
2. Schema v3: statistics separated from quota bookkeeping; metered flag frozen
   at write time; notifications table.
3. Single quota engine (`quota.py`) shared by daemon startup and the loop.
4. Atomic IPC socket, instance lock, stale-socket recovery, cursor-based
   notifications, immutable status snapshot.
5. Ruleset renderer with `destroy table` idempotence, APT-window ordering,
   strict cgroup rules and a port fallback.
6. Daemon: timed APT window, cgroup supervision, orphan-session recovery,
   firewall error surfacing.
7. UI: non-blocking IPC in tray/dashboard, APT countdown, notification delivery,
   X11 focus reporting.
8. Installer/uninstaller/deploy scripts with `SUDO_USER`, `visudo -cf`, secure
   paths, and `--purge`.
9. Hermetic test suite (130 tests) replacing environment-coupled duplicates.
10. Documentation truth pass; memory and capability claims corrected.

## Still requiring a human with sudo

1. `sudo ./scripts/install.sh --system` from the build or deployed tree.
2. Live verification of cgroup creation and strict-mode enforcement.
3. Removal of the stale `.git` in the deploy directory, then `scripts/deploy.sh`.

Nothing in this file should be read as a claim that those privileged steps have
already run.
