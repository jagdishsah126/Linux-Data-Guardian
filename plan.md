# Linux Data Guardian — Architecture and Maintenance Plan

This supersedes the original planning document. For the audit trail, bug
registry, and phased roadmap see `ImprovePlan.md`; for observed behaviour see
`report.md`.

## Runtime shape

```
systemd (User=<login user>, PYTHONPATH=<deploy dir>)
  └── data_guardian.main daemon
        ├── NetworkMonitor ── nmcli polling, designated UUID/name match
        ├── InterfaceByteCounter ── /proc/net/dev deltas
        ├── Database (SQLite WAL, schema v3)
        │     ├── daily_usage / network_daily_usage  (statistics)
        │     ├── quota_state                        (limits, allowance, block)
        │     └── notifications / approval_events
        ├── quota.evaluate() ── single quota decision point
        ├── NftablesManager ── ruleset renderer + helper client
        └── IpcServer (0600 Unix socket, immutable snapshot + cursor feed)
              ├── tray (3 s poll, notify-send delivery)
              ├── dashboard (GTK3, worker-thread IPC)
              └── CLI
```

Privileged surface:

```
/usr/local/libexec/data-guardian-helper   (root, invoked through one sudoers line)
  --status | --flush | --apply-stdin | --cgroup-init
  --cgroup-set <foreground|whitelist> PID... | --cgroup-clear ...
```

The helper validates fixed actions, a single owned table (`inet data_guardian`),
interface names, cgroup names, and positive PIDs. It rejects `flush ruleset`,
`include`, and foreign tables.

## Invariants to preserve

1. **One writer.** Only the daemon mutates firewall state. UI actions go through
   IPC; `bypass` asks the daemon instead of unlinking its socket.
2. **One quota implementation.** Nothing outside `quota.py` may compare usage to
   limits.
3. **Metered means frozen.** A usage row records `metered` at write time; changing
   the designated network later must not rewrite history.
4. **Zero limit means unlimited**, never "exhausted".
5. **Rules are replaced, not appended.** Every apply begins with
   `destroy table inet data_guardian` in the same transaction.
6. **No root Python.** Scripts may drop to the target user, never import project
   code as root.
7. **Fail visibly.** A firewall failure is reported in status, notifications, and
   `doctor`; it is never swallowed.
8. **Private data.** Config and DB are 0600, directories 0700, IPC socket 0600.

## Maintenance

* Schema changes: add a numbered block in `Database._migrate`, keep v1/v2 paths
  working, and test with a synthetic old database (see `report.md` §2).
* New UI action: add a handler in `daemon._handle_ipc_request`, validate input
  there, and call it through `ipc_async` from GTK.
* New privileged action: extend the helper's case statement and its tests; never
  add a second sudoers entry.
* New ruleset: extend `build_rulesets`, then run the `nft -c` parsing tests.
* Deployment: `scripts/deploy.sh` copies runtime trees only and records the
  commit in `DEPLOYED`. The deploy directory must not be a git clone.
