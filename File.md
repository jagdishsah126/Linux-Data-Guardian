# Linux Data Guardian — File Guide

This document explains every file in the project and how the pieces fit
together. It is written for reverse engineering: start with the runtime map, then
read the files in the order suggested at the end.

The same document is copied into the runtime artifact by `scripts/deploy.sh`, so
it remains available even after the source checkout is removed.

| Item | Value |
|---|---|
| Source tree | the directory where this repository is cloned |
| Runtime tree | the path passed to `scripts/deploy.sh`; recorded in `DEPLOYED` |
| Runtime metadata | `DEPLOYED` in the runtime tree |
| Runtime entry point | `/usr/bin/python3 -m src.data_guardian.main` |
| System service | `/etc/systemd/system/data-guardian.service` |
| User wrapper | `~/.local/bin/data-guardian` |
| Live config | `~/.config/linux-data-guardian/config.yaml` |
| Live whitelist | `~/.config/linux-data-guardian/whitelist.yaml` |
| Live database | `~/.local/share/linux-data-guardian/guardian.db` |
| IPC socket | `/run/data-guardian/data-guardian.sock` |

The runtime tree contains no `.git` directory. Editing files there changes the
running installation on the next process restart, but those edits are not
source-controlled and will be overwritten by the next deployment. Make real code
changes in the source tree, commit them, then deploy again.

`File.md` is included in the runtime artifact so the file guide is available
offline. It is documentation only; no runtime code reads it.

---

## 1. Runtime map

```text
systemd
  └─ data-guardian.service
       └─ python3 -m src.data_guardian.main daemon
            └─ DataGuardianDaemon
                 ├─ ConfigManager ───────► ~/.config/linux-data-guardian/*.yaml
                 ├─ Database ────────────► ~/.local/share/linux-data-guardian/guardian.db
                 ├─ NetworkMonitor ──────► libnm / nmcli
                 ├─ InterfaceByteCounter ► /proc/net/dev
                 ├─ X11WindowTracker ────► xprop / loginctl / /proc
                 ├─ Quota engine ────────► quota_state + metered statistics
                 ├─ NftablesManager
                 │    └─ rulesets.py ────► privileged helper ──► nft
                 └─ IpcServer ───────────► /run/data-guardian/data-guardian.sock
                                              ▲
                     data-guardian status ─────┤
                     tray applet ──────────────┤
                     GTK dashboard ────────────┘
```

There are two independent user interfaces:

- **Tray applet** — small always-running panel process.
- **Dashboard** — GTK3 window opened on demand.

Both talk to the daemon over the same UNIX socket. Neither one owns the firewall
or the quota state.

The **daemon** is the only process that:

- writes firewall rules,
- records usage,
- evaluates quota,
- mutates daemon state.

The **privileged helper** is the only root process. It is deliberately small and
accepts only a fixed set of actions.

---

## 2. What happens at boot / login

1. systemd starts `data-guardian.service` as the user who ran the installer.
2. The service runs `python3 -m src.data_guardian.main daemon` from this folder.
3. `DataGuardianDaemon.start()`:
   - acquires a lock in `/run/data-guardian/data-guardian.lock`,
   - starts the IPC server,
   - installs SIGINT/SIGTERM handlers,
   - enters a poll loop.
4. Every `poll_interval_seconds` (default 5 seconds), `_iteration()`:
   - asks NetworkManager for the active connection,
   - detects whether the designated hotspot is active,
   - reads `/proc/net/dev` for byte deltas,
   - records statistics and quota bookkeeping,
   - evaluates the quota,
   - applies or updates firewall rules,
   - rebuilds an in-memory status snapshot for UI clients.
5. At desktop login, the autostart entry starts `data-guardian tray`.
6. The tray polls the daemon every 3 seconds and displays status in the panel.
7. `data-guardian dashboard` opens the GTK window on demand.

---

## 3. Profiles, quota, and strict mode in one page

### Profiles

| Profile | Behaviour while the designated hotspot is active |
|---|---|
| `balanced` | Normal browsing is allowed. New connections from APT's `_apt` UID 42 are dropped. |
| `strict` | Only the focused X11 application tree and allowlisted tool trees may open new connections. Everything else on the metered interface is dropped. |
| `off` | Monitoring only. The `data_guardian` nftables table is removed. |

Strict mode uses cgroup v2:

- `data_guardian/foreground` contains the focused app and all of its descendants.
- `data_guardian/whitelist` contains `opencode`, `antigravity`, and their descendants.
- nftables matches new sockets with `socket cgroupv2`.
- If cgroups cannot be created, strict mode falls back to a TCP port allowlist,
  normally `22, 80, 443`.

### Quota

- Quota is based **only on metered rows** in `network_daily_usage`.
- Daily and monthly limits are evaluated independently.
- `0` means unlimited, not exhausted.
- `quota_state` is authoritative for the stored limit, temporary allowance,
  fired warnings, and exceeded flag.
- When the quota is exceeded, only DHCP/DNS and an optional APT window survive;
  all other traffic on the metered interface is dropped.
- `action_on_exhaustion: warn_only` reports the condition without blocking.

### APT

There is no APT hook in this design. APT's download user is `_apt` (UID 42).
The firewall drops new UID-42 connections on the metered interface unless:

- the user opens a timed APT window, or
- the quota-exceeded ruleset explicitly allows it.

---

## 4. Every file, one by one

### 4.1 Root

#### `README.md`

The user-facing entry point. It explains what the app does, the three profiles,
requirements, installation (direct clone or runtime copy), first-run steps, CLI
commands, configuration, safety/recovery, troubleshooting, uninstall, and
development. Start here if you want to install or operate the app.

#### `CONTRIBUTING.md`

The contributor guide. It covers the development setup, the design invariants
that must be preserved, how to add tests, style rules, privacy rules for test
data, and which files are security-sensitive.

#### `SECURITY.md`

The security policy. It explains private vulnerability reporting, the privilege
boundary, what the project does and does not defend against, and how to sanitize
diagnostics before sharing them.

#### `.gitignore`

Excludes generated and local-only files from Git: Python bytecode, pytest/ruff
caches, coverage output, build artifacts, SQLite databases and WAL/SHM files,
sockets, virtual environments, and editor files. This is why the repository does
not contain your live database or runtime caches.

#### `pyproject.toml`

Project metadata and tool configuration:

- package name, version, Python requirement (`>=3.10`), and MIT license metadata;
- runtime dependency declaration for PyYAML;
- pytest settings (`testpaths`, `pythonpath`, warning filters);
- Ruff target version, line length, and lint rules.

The project is installed as a source tree by `scripts/install.sh`; it is not
published as a wheel, so system packages provide PyGObject and the rest.

#### `.github/workflows/tests.yml`

GitHub Actions CI. It runs the hermetic test suite on Python 3.10 and 3.12, then
runs `ruff check src tests` in a separate job. It installs only test-time system
packages (`nftables`, `util-linux`) and Python test packages.

#### `DEPLOYED`

```text
source=/path/to/source-checkout
commit=<short-commit>
deployed_at=<ISO-8601 timestamp>
```

Deployment metadata written by `scripts/deploy.sh`. It records which build tree
and commit produced this runtime copy. It is informational only; no runtime code
reads it. The source path in this file is not used by the daemon, the wrapper, or
systemd, so deleting the build tree does not affect the deployment.

#### `File.md`

This guide. It is documentation only. It is not imported, executed, or read by
the application.

---

### 4.2 `assets/`

#### `assets/icon.png`

The green application icon. It is copied by `scripts/install.sh --system` to:

```text
/usr/share/icons/hicolor/512x512/apps/linux-data-guardian.png
```

The tray, dashboard, and desktop entries refer to it by the icon-theme name
`linux-data-guardian`. The tray intentionally does not swap in red/yellow/green
theme icons; it always uses this icon and communicates state through the tooltip
and menu text.

---

### 4.3 `config/`

#### `config/config.default.yaml`

The built-in default configuration used as the base layer by `ConfigManager`.
The user file at `~/.config/linux-data-guardian/config.yaml` is deep-merged over
it, so a missing user key falls back to this file.

Important defaults:

- `app.default_profile: balanced`
- `app.poll_interval_seconds: 5`
- daily quota `1000 MB`, monthly `25000 MB`
- warning thresholds `[80, 95]`
- `action_on_exhaustion: block_unapproved`
- approvals enabled for APT/pip/npm/cargo
- database path `~/.local/share/linux-data-guardian/guardian.db`

This file is a seed and fallback. Editing it does not change an existing user
config until that config is removed or the missing key is absent.

#### `config/whitelist.yaml`

The default strict-mode policy. It defines:

- `strict_whitelist_processes: [opencode, antigravity]`
- `strict_allowed_ports_tcp: [22, 80, 443]`

`ConfigManager` deep-merges the user whitelist over this file. The process list
is used to build the `whitelist` cgroup; the port list is used only when cgroup
v2 matching is unavailable.

#### `config/sudoers-data-guardian`

A sudoers template. `scripts/install.sh --system` replaces `@USER@` and installs
it as `/etc/sudoers.d/linux-data-guardian` with mode `0440` after validating it
with `visudo -cf`.

It grants exactly one passwordless command:

```text
/usr/local/libexec/data-guardian-helper
```

No wildcard is used. The helper validates its own arguments, so the sudoers file
does not need to understand subcommands.

---

### 4.4 `scripts/`

#### `scripts/data-guardian-helper`

The only privileged surface. It is installed as root:root at
`/usr/local/libexec/data-guardian-helper` with mode `0755`.

It refuses to run unless UID 0, finds `nft`, and accepts only these actions:

| Action | What it does |
|---|---|
| `--status` | Reports whether `table inet data_guardian` exists. |
| `--flush` | Deletes only that table and verifies it is gone. |
| `--apply-stdin` | Reads a bounded ruleset from stdin, validates it, then applies it. |
| `--cgroup-init` | Creates `data_guardian/foreground` and `data_guardian/whitelist`. |
| `--cgroup-set GROUP PID...` | Replaces the PIDs in one cgroup with the supplied tree. |
| `--cgroup-clear GROUP` | Moves all PIDs back to the root cgroup. |

Defence-in-depth in `--apply-stdin`:

- maximum ruleset size 64 KiB,
- rejects `flush ruleset`, `include`, `exec`, `define`, `redefine`,
- every table statement must target `table inet data_guardian`,
- the ruleset must actually contain that table,
- `nft -c -f -` syntax check before `nft -f -`,
- never sources or imports caller-supplied code.

`--cgroup-set` first drains the old cgroup membership back to the root cgroup,
then moves the new PIDs in. This prevents a previously focused application from
remaining allowed forever. PIDs are validated to be numeric and in a sane range,
and a maximum of 512 PIDs is accepted per call.

#### `scripts/deploy.sh`

The deployment script. It copies runtime directories plus `File.md` from the
source tree into the destination:

```text
src/ scripts/ config/ systemd/ assets/ File.md
```

It uses:

```text
rsync -a --delete --delete-excluded --include ... --exclude '*'
```

so files outside that set are removed from the destination. It refuses to
deploy if the destination contains `.git`, which prevents accidentally turning
the runtime tree into a second source checkout. After copying, it removes
`__pycache__` and `*.pyc`, then writes `DEPLOYED` with the source path, short
commit, and timestamp.

Run it from the source tree:

```bash
./scripts/deploy.sh "$HOME/linux-data-guardian-runtime"
```

If no destination is given, it defaults to:

```text
${XDG_DATA_HOME:-$HOME/.local/share}/linux-data-guardian-runtime
```

#### `scripts/install.sh`

The idempotent installer. It has two modes.

**User mode** (`./scripts/install.sh`):

- creates `~/.local/bin`, `~/.config/autostart`, `~/.local/share/applications`,
  and `~/.config/linux-data-guardian`;
- writes the `data-guardian` wrapper pointing at this deployed tree;
- seeds user `config.yaml` and `whitelist.yaml` only if they do not already exist;
- renders the tray autostart and dashboard desktop entries;
- installs the `pip`, `pip3`, `npm`, and `cargo` shims.

**System mode** (`sudo ./scripts/install.sh --system`):

- installs the helper to `/usr/local/libexec/data-guardian-helper`;
- renders and validates the sudoers rule;
- renders and installs the systemd service;
- installs the icon into the hicolor theme;
- reloads systemd, enables the service, and restarts it.

The installer resolves the human user from `SUDO_USER` when run through sudo, so
user files do not land in `/root`. It does not write an APT hook and never
imports user-controlled Python as root.

#### `scripts/uninstall.sh`

The official uninstaller.

Default behaviour:

- stops and disables the service,
- flushes the firewall table,
- removes the helper, sudoers rule, APT hook if present, icon, shims, wrapper,
  autostart entry, and desktop entry,
- keeps `~/.config/linux-data-guardian` and the usage database.

`--purge` additionally removes the user configuration directory and the usage
database. It does not delete the deployed tree or the build tree; those are
removed separately if desired.

#### `scripts/emergency-disable.sh`

Emergency escape hatch. It first asks the running daemon to perform
`emergency_bypass`, which resets profile state, quota flags, and firewall rules
together. If the daemon is offline, it falls back to a direct `--flush` via the
helper. It never unlinks the daemon's socket.

---

### 4.5 `systemd/`

#### `systemd/data-guardian.service`

The system service template. `install.sh --system` replaces `@USER@`, `@HOME@`,
and `@PROJECT_DIR@`.

Key choices:

- runs as the desktop user, not root;
- `WorkingDirectory` and `PYTHONPATH` point at this deployed tree;
- `RuntimeDirectory=data-guardian` creates `/run/data-guardian` with mode `0700`
  and the correct owner, even before `/run/user/<uid>` exists;
- `XDG_RUNTIME_DIR` is intentionally not set, because a system manager would
  expand `%U` to `0`;
- `ExecStart` runs the daemon module;
- `ExecStopPost` calls the helper's `--flush`, so a stopped daemon does not leave
  stale firewall rules;
- `Restart=on-failure`.

#### `systemd/data-guardian.desktop`

The application-menu entry. `install.sh` renders it into
`~/.local/share/applications/data-guardian.desktop`. It launches:

```text
~/.local/bin/data-guardian dashboard
```

with the `linux-data-guardian` icon.

#### `systemd/data-guardian-tray.desktop`

The XDG autostart entry. `install.sh` renders it into
`~/.config/autostart/data-guardian-tray.desktop`. It launches the tray after a
10-second delay at login.

---

### 4.6 `src/data_guardian/` — package root

#### `src/data_guardian/__init__.py`

Package metadata: version `2.0.0`, author, and a one-line description. Importing
the package does not start anything.

#### `src/data_guardian/__main__.py`

Allows:

```bash
python3 -m src.data_guardian ...
```

It simply imports `main.main` and exits with its return code. This is also what
the wrapper ultimately executes.

#### `src/data_guardian/main.py`

The command-line entry point and dispatcher. It defines the parser and routes
subcommands:

| Command | Behaviour |
|---|---|
| `status` | Combines daemon IPC state with local database truth. |
| `daemon` | Starts `DataGuardianDaemon`. |
| `tray` | Starts the GTK tray applet. |
| `dashboard` | Opens the GTK dashboard. |
| `set-hotspot` | Lists saved Wi-Fi networks and saves the chosen UUID/name. |
| `profile` | Sets the global default profile, via IPC when the daemon is online. |
| `allow` | Grants a temporary daily allowance, via IPC or directly in the DB. |
| `allow-apt` | Asks the daemon to open a timed APT window. |
| `close-apt` | Asks the daemon to close the APT window. |
| `bypass` | Emergency bypass; falls back to a direct firewall flush. |
| `doctor` | Runs the diagnostic suite. |
| `install-shims` | Installs the package-manager shims. |
| `remove-shims` | Removes them. |

`gather_status()` is important: it does not trust only one source. It asks the
daemon for its snapshot, then independently checks NetworkManager and the
database. That is why `status` still shows useful usage data when the daemon is
offline.

#### `src/data_guardian/config.py`

Configuration paths, YAML loading, validation, and typed accessors.

- `default_config_dir()` = `$XDG_CONFIG_HOME/linux-data-guardian` or
  `~/.config/linux-data-guardian`.
- `default_data_dir()` = `$XDG_DATA_HOME/linux-data-guardian` or
  `~/.local/share/linux-data-guardian`.
- `PROJECT_ROOT` is the parent of `src/`, i.e. this deployed tree.
- `BUILTIN_CONFIG_FILE` and `BUILTIN_WHITELIST_FILE` point at `config/` here.
- `ConfigManager` deep-merges the user YAML over the built-in YAML.
- A broken user config is logged and ignored rather than crashing startup.
- `save_user_config()` writes atomically with mode `0600`.
- Properties convert strings to bounded integers, validate profiles, and expose
  quota, polling, retention, strict-process, and strict-port settings.
- `db_path` expands `~` and defaults to the XDG data directory.

#### `src/data_guardian/database.py`

SQLite persistence and schema migration. This is the largest data module.

Connection handling:

- WAL mode, `synchronous=NORMAL`, foreign keys enabled;
- every short-lived operation uses `_connection()`, which commits/rolls back and
  **closes** the connection;
- file permissions are hardened to `0600`, directories to `0700`.

Schema v3 tables:

| Table | Purpose |
|---|---|
| `schema_version` | Applied migration versions. |
| `sessions` | One row per hotspot session, with connection metadata and RX/TX totals. |
| `daily_usage` | Combined daily statistics for all interfaces; chart data only. |
| `network_daily_usage` | Per-network daily statistics; `metered` is frozen at write time. |
| `quota_state` | Authoritative daily limit, allowance, warnings, exceeded flag, reason. |
| `notifications` | Persistent notification feed consumed by the tray. |
| `approval_events` | Audit log of package-manager approval decisions. |

Important methods:

- `start_session` / `update_session` / `end_session` / `close_orphan_sessions`.
- `record_usage_delta`: increments both stats tables and updates quota limit.
- `get_today_usage`: combines statistics with quota state.
- `get_network_today_usage`: per-network stats for the dashboard.
- `grant_temporary_allowance`: adds bytes and clears the exceeded state.
- `mark_warning_fired`: fires each threshold at most once per day.
- `set_quota_exceeded`, `set_daily_limit`.
- `metered_total` and `metered_total_cycle`: the only quota basis.
- `get_monthly_usage`, `get_all_networks_history`, `get_known_networks`.
- `add_notification`, `get_notifications_since`, `prune_notifications`.
- `prune_history`, `checkpoint`.
- `record_approval_event`, `get_recent_approvals`.

The key design rule is stated in the module docstring: **quota decisions must
never be derived from statistics tables.** Statistics are for display; quota is
`quota_state` plus the metered totals.

#### `src/data_guardian/daemon.py`

The orchestrator and the only writer of firewall state.

Lifecycle:

- `acquire_instance_lock()` prevents two daemons from fighting over the socket
  and firewall.
- `start()` starts the IPC server and enters the loop.
- `stop()` is idempotent: ends the active session, flushes the firewall,
  checkpoints the DB, and releases the lock.

Per-tick flow:

1. Poll NetworkManager and fire hotspot activation/deactivation callbacks.
2. Expire the APT window if necessary.
3. Read the focused X11 window and update the active app/PID.
4. In strict mode, move the focused tree and whitelisted trees into cgroups.
5. Read byte deltas and record them.
6. Evaluate quota and enforce any state change.
7. Run maintenance every 60 ticks.
8. Rebuild the immutable status snapshot.

Transition behaviour:

- On hotspot activation: end any previous session, start a new session, reset
  byte counters, recompute quota, apply the profile, notify the UI.
- On deactivation: end the session, clear both cgroups, close the APT window,
  flush the firewall, clear the quota-block flag, notify the UI.

Firewall behaviour:

- `_apply_firewall()` is the single writer.
- If profile is `off` and there is no quota block and no APT window, it flushes.
- Otherwise it asks `NftablesManager` to apply the effective profile.
- Failures are stored in `_firewall_error` and surfaced in status and
  notifications.

IPC commands implemented here:

`ping`, `get_status`, `set_profile`, `set_network_profile`,
`grant_allowance`, `allow_apt_window`, `close_apt_window`, `emergency_bypass`,
`set_hotspot_uuid`, `report_focus`.

#### `src/data_guardian/ipc.py`

UNIX-domain-socket IPC.

Runtime directory selection, in order:

1. `RUNTIME_DIRECTORY` if it is a private directory owned by this user;
2. `/run/data-guardian` if private;
3. `XDG_RUNTIME_DIR` if private;
4. `/run/user/<uid>` if private;
5. `/tmp/data-guardian-<uid>`, created `0700` and ownership-checked.

The `lstat`-based private-directory check deliberately rejects symlinks. This
prevents a shared `/tmp` entry from redirecting the socket or lock.

Server properties:

- protocol version 2;
- request limit 64 KiB, response limit 256 KiB;
- socket file mode `0600`;
- bind to a temporary name, then `os.replace()` into place;
- stale-socket detection: a live peer is not stolen;
- each client gets a daemon thread.

Client:

- `send_ipc_command()` returns `None` when the daemon is absent or the request
  fails, which callers treat as “daemon unavailable”.

#### `src/data_guardian/quota.py`

The single quota arithmetic implementation.

`evaluate()`:

- reads the daily/monthly configured limits;
- keeps the stored daily limit in sync;
- adds the temporary allowance;
- uses `metered_total()` for the day and `metered_total_cycle()` for the month;
- treats `0` as unlimited;
- chooses `monthly` before `daily` when both are hit;
- computes warning level from the configured thresholds;
- sets the `exceeded` flag in `quota_state`;
- sets `enforce` from `action_on_exhaustion`.

`newly_fired_thresholds()` returns thresholds that have been crossed but not yet
recorded for the day. The daemon marks them and emits one notification each.

#### `src/data_guardian/diagnostics.py`

Implements `data-guardian doctor`. It runs 11 checks:

1. Python dependencies (`yaml`, `gi`).
2. Helper exists and is root:root, executable, not group/other writable.
3. Helper supports all required actions.
4. Sudoers rule works without a password.
5. `nft` exists and the table state is readable.
6. Strict-mode cgroups are available/creatable.
7. Daemon is active, IPC responds, memory is sane, no firewall error.
8. SQLite integrity, schema version, session/network counts.
9. Database and config permissions.
10. Icon, tray process, autostart entry.
11. Active connection and designated-hotspot match.

Exit codes: `0` all pass, `2` warnings, `1` failures. `--json` emits the raw
results; `--no-color` disables ANSI colour.

#### `src/data_guardian/processes.py`

Low-level `/proc` introspection used by strict mode.

- `comm_for(pid)` reads `/proc/<pid>/comm`.
- `ppid_for(pid)` parses `/proc/<pid>/stat` after the final `)` so process names
  containing spaces/parentheses do not break parsing.
- `all_pids()` lists numeric `/proc` entries.
- `descendant_pids(root)` builds a parent→children map and walks the tree to a
  bounded depth (12).
- `find_pids_by_names(names)` matches exact or prefix `comm` values, covering
  wrappers such as `opencode`.

---

### 4.7 `src/data_guardian/approvals/`

#### `src/data_guardian/approvals/__init__.py`

Package marker and docstring. No runtime logic.

#### `src/data_guardian/approvals/gate.py`

The approval gate invoked by the shims.

`DOWNLOAD_ACTIONS` maps tools to the subcommands that actually fetch bytes.
`READ_ONLY_ACTIONS` lists commands that are always allowed. `find_subcommand()`
skips flags and flags-with-values, and handles `python -m pip install ...`.

`should_gate()` returns true only when:

- Data Guardian is enabled;
- the active connection is the designated hotspot, or is reported metered;
- the network profile is not `off`;
- approval for that tool is enabled;
- the command is a real download action.

`prompt_user()`:

- uses Zenity when `DISPLAY` is set and Zenity exists;
- otherwise uses a TTY prompt;
- otherwise applies `timeout_action` (`deny` or `allow`);
- records every decision in `approval_events`;
- sends a desktop “Download blocked” notification on denial.

`main()` is the shim entry point: exit `0` means proceed, exit `1` means deny.

#### `src/data_guardian/approvals/shims.py`

Generates and removes transparent shims in `~/.local/bin` for:

```text
pip, pip3, npm, cargo
```

Each shim:

1. reads `~/.config/linux-data-guardian/project_dir` if present;
2. if the deployed source exists, calls the approval gate;
3. if the gate is unavailable, allows read-only commands but refuses obvious
   download commands;
4. otherwise searches `PATH` for the real binary, excluding itself and its own
   directory, then `exec`s it.

`write_project_marker()` makes the shims survive the project being moved or
renamed. `remove_shims()` removes only files containing the Data Guardian
marker, leaving unrelated binaries untouched.

---

### 4.8 `src/data_guardian/firewall/`

#### `src/data_guardian/firewall/__init__.py`

Package marker and docstring. No runtime logic.

#### `src/data_guardian/firewall/rulesets.py`

The single source of truth for nftables text. It is pure Python and unit-testable
without root.

Every non-off ruleset starts with:

```text
destroy table inet data_guardian
table inet data_guardian { ... }
```

so applying is idempotent and never appends duplicate rules.

Common rules:

- accept loopback;
- accept established/related;
- drop invalid;
- accept DNS/DHCP UDP and DNS TCP;
- optionally accept NTP;
- drop new APT (`skuid 42`) connections on the metered interface.

Balanced mode: after the common rules, everything else is allowed.

Strict mode with cgroups:

```text
socket cgroupv2 level N "data_guardian/foreground" ct state new accept
socket cgroupv2 level N "data_guardian/whitelist" ct state new accept
```

then drop all other new connections on the metered interface.

Strict fallback without cgroups: allow only the configured TCP ports
(default 22/80/443) for new connections, then drop the rest.

Quota-exceeded mode: keep DNS/DHCP and optional APT, then drop everything else on
the metered interface.

`is_valid_interface()` restricts interface names to a kernel-safe shape.

#### `src/data_guardian/firewall/nft_manager.py`

The Python side of the privileged helper.

- `get_helper_path()` prefers `/usr/local/libexec/data-guardian-helper`, then the
  copy in this tree's `scripts/` directory.
- `_helper_cmd()` runs the helper directly as root, otherwise via `sudo -n`.
- `can_apply_rules()` probes `--status`; it deliberately does not use
  `sudo -n true`, because the sudoers rule grants only the helper command.
- `ensure_cgroups()` calls `--cgroup-init`.
- `cgroup_set()` calls `--cgroup-set` or `--cgroup-clear`.
- `apply_profile()` renders a ruleset and sends it to `--apply-stdin`.
- `open_apt_window()` / `close_apt_window()` re-apply the current profile with
  the window flag.
- `flush()` removes the table and verifies it is gone.
- `table_present()` returns `True`, `False`, or `None` when unknown.

If strict cgroups cannot be created, `apply_profile()` falls back to the port
allowlist and reports that in its message.

---

### 4.9 `src/data_guardian/network/`

#### `src/data_guardian/network/__init__.py`

Package marker and docstring. No runtime logic.

#### `src/data_guardian/network/byte_counter.py`

Reads `/proc/net/dev`.

- RX is field 1, TX is field 9 in each interface line.
- `get_delta()` stores the previous reading per interface and returns
  `(delta_rx, delta_tx, speed)`.
- The first sample returns zeros.
- Counter wrap or interface reset is handled by treating the new counter as the
  delta.
- With no explicit interface, it picks a physical-looking interface and excludes
  loopback, container, bridge, tunnel, and WireGuard-style names.
- `reset_interface()` clears the cached reading on reconnect.

#### `src/data_guardian/network/nm_monitor.py`

NetworkManager integration.

- Prefers libnm (`NM.Client`) for active connections and metered flags.
- Falls back to `nmcli -t` output if libnm is unavailable.
- `split_nmcli_line()` correctly handles escaped colons in SSIDs.
- `default_route_interface()` parses `/proc/net/route` to prefer the interface
  carrying the default route.
- `get_active_connection()` returns the primary active Wi-Fi/Ethernet
  connection.
- `matches_designated()` matches by UUID first, then by name.
- `poll_check()` fires activation/deactivation callbacks exactly on transitions.
- `list_saved_connections()` supplies the hotspot picker and dashboard list.

---

### 4.10 `src/data_guardian/ui/`

#### `src/data_guardian/ui/__init__.py`

Package marker and docstring. No runtime logic.

#### `src/data_guardian/ui/async_ipc.py`

Offloads IPC calls from the GTK main thread. It starts a daemon worker thread,
calls `send_ipc_command()`, and delivers the result back to the GTK loop with
`GLib.idle_add()`. This prevents the UI from freezing while the daemon is doing
privileged firewall work.

#### `src/data_guardian/ui/dashboard.py`

The GTK3 dashboard, with three tabs.

**Overview**

- status banner (unmetered, metered, quota exceeded, firewall failure);
- current connection and focused application;
- network dropdown populated from saved Wi-Fi plus known tracked networks;
- “Set as Hotspot” button;
- per-network RX/TX/total and a quota progress bar;
- +100 MB, +500 MB, APT window, and emergency bypass buttons;
- per-network profile radio buttons;
- recent approval log.

**Statistics**

- period selector (1/7/15/30/45/60 days);
- metric selector (RX/TX/total);
- lazily imported Matplotlib chart, one line per network plus a total line;
- per-network summary table with RX, TX, total, and last-seen date.

Matplotlib is imported only when the Statistics tab is built, so the overview
starts quickly and does not pay the import cost unless needed.

**CLI**

- curated command list grouped by purpose;
- each button runs a shell command in a background thread;
- output streams into a read-only text view with colour tags;
- 30-second timeout and clear button.

The dashboard polls the daemon every 3 seconds in a worker thread; it never
blocks the GTK main loop.

#### `src/data_guardian/ui/tray.py`

The panel applet. It supports three backends in order:

1. XApp.StatusIcon;
2. AppIndicator3;
3. Gtk.StatusIcon.

The icon is always `linux-data-guardian` (the green app icon). State is conveyed
by tooltip and menu text, not by swapping icon colours.

Menu items:

- status line;
- today's metered usage;
- warning line when needed;
- Balanced / Strict / Off profile radios;
- +100 MB allowance;
- APT window open/close;
- dashboard;
- emergency bypass;
- quit.

The tray polls every 3 seconds in a worker thread and delivers notifications
with `notify-send`. In strict mode it also reports the focused X11 window to the
daemon, which is important when the daemon is a system service that cannot see
the desktop session's environment directly.

---

### 4.11 `src/data_guardian/window_tracker/`

#### `src/data_guardian/window_tracker/__init__.py`

Package marker and docstring. No runtime logic.

#### `src/data_guardian/window_tracker/x11_tracker.py`

X11 focus detection.

- `is_x11_available()` checks `DISPLAY` and `/usr/bin/xprop`.
- `autodetect_environment()` asks `loginctl` for the user's X11 session and sets
  `DISPLAY`/`XAUTHORITY` when a system service starts before the desktop
  environment exists.
- `get_active_window()` reads `_NET_ACTIVE_WINDOW`, then queries
  `_NET_WM_PID`, `WM_CLASS`, and `WM_NAME`.
- It resolves the process name from `/proc/<pid>/comm`, falling back to
  `WM_CLASS`.
- `get_process_family_pids()` is a legacy recursive `pgrep` helper; strict mode
  primarily uses `processes.descendant_pids()` instead.

---

### 4.12 `tests/`

The test suite is hermetic. It uses temporary XDG directories and temporary
SQLite files, mocks privileged operations, and skips checks that need root or
unavailable kernel features. The normal suite has 130 tests.

#### `tests/support.py`

Shared fixture `IsolatedTestCase`. It creates a temporary config file, whitelist,
and database, points `ConfigManager` at them, and deletes the directory on
teardown. Tests using it never touch the real user config or database. It also
provides a small `record()` helper for usage rows.

#### `tests/test_approvals.py`

Tests the approval gate and shims:

- `pip install` and `python -m pip install` detection;
- flags that consume a following value;
- read-only commands passing through;
- timeout policy;
- shim installation and removal;
- refusal of obvious downloads when the gate is unavailable.

#### `tests/test_config.py`

Tests configuration and XDG behavior:

- defaults and bounded numeric values;
- invalid profile rejection and fallback;
- per-network profiles not changing the global default;
- hotspot name/UUID persistence;
- private config file permissions;
- billing-cycle clamping;
- strict whitelist defaults and port validation;
- XDG directory selection.

#### `tests/test_daemon.py`

Tests daemon lifecycle and IPC:

- startup quota recovery;
- profile, allowance, APT-window, and hotspot commands;
- notification cursors;
- firewall failure surfacing;
- socket ownership and single-daemon protection;
- malformed JSON and handler-crash recovery;
- runtime-directory selection.

#### `tests/test_database.py`

Tests schema and persistence:

- schema v3 tables and version;
- private database permissions;
- short-lived connections closing their file descriptors;
- session lifecycle;
- daily statistics and quota bookkeeping;
- zero means unlimited;
- warnings recorded once;
- approval event logging;
- metered-only monthly totals;
- the frozen `metered` flag.

#### `tests/test_diagnostics.py`

Tests the doctor, CLI wiring, and installer/deploy safety:

- all expected checks are registered;
- report rendering and exit codes;
- `doctor`, `--version`, and command help;
- no APT hook, helper/sudoers installation, `SUDO_USER` handling;
- no root import of project Python;
- emergency script never unlinks the socket;
- deploy script guards and runtime include list, including `File.md`.

#### `tests/test_firewall_rules.py`

Tests the firewall renderer and helper safety:

- every profile starts with `destroy table`;
- balanced APT rules, strict cgroup rules, fallback ports;
- quota hard cutoff and APT-window ordering;
- interface-name injection rejection;
- real `nft -c` parsing when `nft` is available;
- helper rejection of unknown actions, foreign tables, `flush ruleset`,
  `include`, cgroup traversal, invalid PIDs, and non-root execution.

#### `tests/test_network.py`

Tests NetworkManager parsing and byte counting:

- `nmcli` colon escaping;
- UUID/name matching;
- activation/deactivation transitions firing once;
- byte formatting;
- synthetic counter deltas and interface resets;
- virtual interfaces not being auto-selected.

#### `tests/test_quota.py`

Tests the quota engine:

- empty day is free;
- zero limit is unlimited;
- metered-only accounting;
- allowance clears a block;
- monthly-only plans;
- `warn_only` never blocks;
- warning thresholds fire once;
- database permissions, orphan recovery, and schema version.

#### `tests/test_ui_sync.py`

Tests UI synchronization:

- per-network profile persistence when the daemon handles
  `set_network_profile`;
- the tray's `_loading_profile` guard preventing feedback loops while it
  updates radio buttons.

---

## 5. Generated `__pycache__` files

Python creates these automatically. They are **compiled bytecode**, not source.
They are safe to delete and will be recreated on the next import. They are not
part of a fresh deployment; `scripts/deploy.sh` removes them before writing
`DEPLOYED`.

Typical cache groups:

| Directory | Files |
|---|---|
| `src/data_guardian/__pycache__/` | `__init__`, `config`, `daemon`, `database`, `diagnostics`, `ipc`, `main`, `processes`, `quota` |
| `src/data_guardian/approvals/__pycache__/` | `__init__`, `shims` |
| `src/data_guardian/firewall/__pycache__/` | `__init__`, `nft_manager`, `rulesets` |
| `src/data_guardian/network/__pycache__/` | `__init__`, `byte_counter`, `nm_monitor` |
| `src/data_guardian/ui/__pycache__/` | `__init__`, `async_ipc`, `dashboard`, `tray` |
| `src/data_guardian/window_tracker/__pycache__/` | `__init__`, `x11_tracker` |

Each `foo.cpython-312.pyc` is the CPython 3.12 bytecode for `foo.py`. A module
gets a cache file after it is imported. A fresh checkout does not contain these
files, and they should not be committed.

---

## 6. Live files outside this folder

These are not part of the deployed artifact, but the application cannot run
without them:

| Path | Purpose |
|---|---|
| `~/.config/linux-data-guardian/config.yaml` | User configuration; merged over `config/config.default.yaml`. |
| `~/.config/linux-data-guardian/whitelist.yaml` | Strict-mode process and port policy. |
| `~/.config/linux-data-guardian/project_dir` | Points shims at this deployed tree. |
| `~/.local/share/linux-data-guardian/guardian.db` | SQLite usage, quota, sessions, notifications, approvals. |
| `~/.local/bin/data-guardian` | Wrapper that sets `PYTHONPATH` to this tree and runs the CLI. |
| `~/.local/bin/{pip,pip3,npm,cargo}` | Generated approval shims. |
| `~/.config/autostart/data-guardian-tray.desktop` | Starts the tray at login. |
| `~/.local/share/applications/data-guardian.desktop` | Application-menu entry. |
| `/run/data-guardian/data-guardian.sock` | IPC rendezvous point. |
| `/run/data-guardian/data-guardian.lock` | Single-daemon lock. |
| `/usr/local/libexec/data-guardian-helper` | Installed privileged helper. |
| `/etc/sudoers.d/linux-data-guardian` | Passwordless rule for exactly that helper. |
| `/etc/systemd/system/data-guardian.service` | Installed, rendered service unit. |
| `/usr/share/icons/hicolor/512x512/apps/linux-data-guardian.png` | Installed icon. |

---

## 7. Suggested reverse-engineering order

If you want to understand the system from the outside in:

1. `systemd/data-guardian.service` and `scripts/install.sh` — how it starts and
   what gets installed.
2. `src/data_guardian/main.py` — the CLI surface and status composition.
3. `src/data_guardian/daemon.py` — the actual control loop.
4. `src/data_guardian/config.py` and `config/config.default.yaml` — inputs.
5. `src/data_guardian/database.py` — persistence and schema.
6. `src/data_guardian/network/nm_monitor.py` +
   `src/data_guardian/network/byte_counter.py` — detection and measurement.
7. `src/data_guardian/quota.py` — the policy arithmetic.
8. `src/data_guardian/firewall/rulesets.py` +
   `src/data_guardian/firewall/nft_manager.py` +
   `scripts/data-guardian-helper` — enforcement and privilege boundary.
9. `src/data_guardian/ipc.py` and `src/data_guardian/ui/async_ipc.py` — how the
   UI talks to the daemon without blocking.
10. `src/data_guardian/ui/dashboard.py` and `src/data_guardian/ui/tray.py` —
    presentation and controls.
11. `src/data_guardian/approvals/gate.py` and
    `src/data_guardian/approvals/shims.py` — package-manager gate.
12. `src/data_guardian/window_tracker/x11_tracker.py` and
    `src/data_guardian/processes.py` — strict-mode focus tracking.

---

## 8. Useful inspection commands

```bash
# What is the daemon doing?
data-guardian status
data-guardian status --json
data-guardian doctor

# Service and logs
systemctl status data-guardian.service --no-pager
journalctl -u data-guardian.service -n 100 --no-pager

# Firewall state
sudo nft list table inet data_guardian

# IPC socket and lock
ls -l /run/data-guardian/

# Database inspection
sqlite3 ~/.local/share/linux-data-guardian/guardian.db \
  '.tables' \
  'SELECT date, rx_bytes, tx_bytes FROM daily_usage ORDER BY date DESC LIMIT 5;' \
  'SELECT date, connection_name, metered, rx_bytes, tx_bytes FROM network_daily_usage ORDER BY date DESC LIMIT 10;'

# Strict-mode cgroups
cat /sys/fs/cgroup/data_guardian/foreground/cgroup.procs
cat /sys/fs/cgroup/data_guardian/whitelist/cgroup.procs

# Current config and whitelist
cat ~/.config/linux-data-guardian/config.yaml
cat ~/.config/linux-data-guardian/whitelist.yaml
```

---

## 9. Design invariants worth remembering

1. **One daemon.** The instance lock prevents two writers.
2. **One firewall writer.** Only the daemon applies profile changes.
3. **One privileged helper.** It accepts fixed actions and validates rulesets.
4. **One quota engine.** `quota.evaluate()` is the only arithmetic.
5. **Statistics are not quota.** Display data and quota bookkeeping are separate.
6. **Metered is frozen per row.** Changing the designated hotspot does not
   re-label historical bytes.
7. **The UI never blocks on IPC.** Worker threads + `GLib.idle_add`.
8. **The deployed tree is self-contained.** The systemd unit, wrapper, project
   marker, shims, and icon all point here, not at the build tree.
9. **The build tree is disposable.** The only reference to it in this tree is
   the informational `source=` line in `DEPLOYED`.
