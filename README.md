# Linux Data Guardian

A local-first data guard for Linux laptops that sometimes use a phone hotspot.
It watches NetworkManager, counts bytes from `/proc/net/dev`, enforces quotas
with an isolated `nftables` table, and asks before package managers download on
a metered connection.

There is no account, cloud service, telemetry, or network listener. Everything
runs locally and the only socket is a user-only Unix socket.

> New to the codebase? Read **[File.md](File.md)** after this README. It explains
> every file, what it does, how it works, and why it exists.

---

## What it does

- **Metered networks only.** Restrictions apply when the connection you marked
  as your hotspot is active. Home and office networks remain untouched.
- **Metered-only quota.** Daily and monthly budgets count only traffic recorded
  while the designated hotspot was active.
- **Three profiles.** `balanced`, `strict`, and `off`.
- **Quota warnings and enforcement.** Warn at the configured thresholds and
  optionally block new outbound traffic when the budget is exhausted.
- **APT window.** Open a temporary timed window for `apt update` / `apt install`
  without disabling the rest of the protection.
- **Download approval for CLI tools.** `pip`, `pip3`, `npm`, and `cargo` shims
  ask before download-style commands on a metered connection.
- **Local history.** SQLite in WAL mode, owner-only permissions, automatic
  pruning.
- **CLI, GTK dashboard, and tray icon.**

### Profiles

| Profile | Behaviour on the metered interface |
| :--- | :--- |
| `balanced` (default) | Normal browsing works. New connections owned by APT's `_apt` user (UID 42) are dropped. |
| `strict` | Only new connections from the focused X11 application tree and the allowlisted tools (`opencode`, `antigravity`) are allowed. Everything else is dropped. |
| `off` | Monitoring only. The `data_guardian` firewall table is removed. |

Strict mode uses cgroup v2 when available. If cgroup matching cannot be set up,
it falls back to a TCP port allowlist (default `22`, `80`, `443`).

> Strict mode's focused-application tracking is **X11-only**. On Wayland, use
> `balanced` or `off`; the foreground cgroup cannot be tracked.

---

## Requirements

- Linux with **systemd**, **NetworkManager**, and **nftables**
- Python **3.10+**
- `python3-gi`, `python3-yaml`
- X11 (`xprop`) for strict-mode focus tracking
- `zenity` for graphical approval dialogs (a terminal prompt is used otherwise)

On Debian, Ubuntu, or Linux Mint:

```bash
sudo apt update
sudo apt install -y \
    python3 python3-gi python3-yaml \
    nftables network-manager \
    x11-utils zenity libnotify-bin rsync
```

Optional:

```bash
# Statistics chart
sudo apt install -y python3-matplotlib

# Native tray backend on desktops that provide XApp
sudo apt install -y xapp
```

The tray also supports AppIndicator3 and plain `Gtk.StatusIcon`, so XApp is not
required.

---

## Install

### Quick install from the clone

```bash
git clone https://github.com/jagdishsah126/Linux-Data-Guardian.git
cd Linux-Data-Guardian

sudo ./scripts/install.sh --system
```

`--system` installs both parts:

1. **System:** the root-owned helper, one sudoers rule, the systemd service,
   and the icon.
2. **User:** the `data-guardian` wrapper, config seed, CLI shims, tray
   autostart entry, and desktop entry.

The installer resolves the real desktop user from `SUDO_USER`, so user files do
not land in `/root`. It does not enable general passwordless sudo and does not
write an APT hook.

After installation:

```bash
data-guardian set-hotspot    # choose your metered Wi-Fi network
data-guardian doctor         # verify the installation
data-guardian status         # show current state
```

The daemon starts immediately. The tray starts automatically at your next login.
To start it now:

```bash
data-guardian tray &
```

### Recommended: install from a runtime copy

Use this if you want to keep the source clone separate from the installed app,
or if you may delete the clone later.

```bash
git clone https://github.com/jagdishsah126/Linux-Data-Guardian.git
cd Linux-Data-Guardian

./scripts/deploy.sh "$HOME/linux-data-guardian-runtime"

cd "$HOME/linux-data-guardian-runtime"
sudo ./scripts/install.sh --system
```

The runtime copy is self-contained: the service, wrapper, shims, and icon all
point at it, not at the source clone. You can delete the clone afterwards.

The deploy script writes a `DEPLOYED` file with the source path and commit. It
also includes `File.md` for offline reading.

### Update

If you installed from a runtime copy:

```bash
cd /path/to/Linux-Data-Guardian
git pull
./scripts/deploy.sh "$HOME/linux-data-guardian-runtime"
sudo "$HOME/linux-data-guardian-runtime/scripts/install.sh" --system
```

If you installed directly from the clone:

```bash
cd /path/to/Linux-Data-Guardian
git pull
sudo ./scripts/install.sh --system
```

Re-running the installer is safe; it is idempotent and does not overwrite an
existing user config or usage database.

---

## First use

```bash
# Pick the Wi-Fi network that is your phone hotspot
data-guardian set-hotspot

# Check everything
data-guardian doctor
data-guardian status
```

Open the dashboard:

```bash
data-guardian dashboard
```

The tray menu and dashboard both let you:

- switch between `balanced`, `strict`, and `off`;
- set the designated hotspot;
- grant a temporary allowance;
- open or close the APT window;
- trigger an emergency bypass;
- view per-network usage and history.

---

## CLI

```text
data-guardian status [--json]        Connection, profile, counters, quota verdict
data-guardian daemon                 Run the background daemon (systemd does this)
data-guardian tray                   Status icon
data-guardian dashboard              GTK dashboard
data-guardian set-hotspot [NAME]     Choose the designated metered network
data-guardian profile MODE           balanced | strict | off
data-guardian allow MB               Add to today's allowance
data-guardian allow-apt              Open the APT window (15 min)
data-guardian close-apt              Close it now
data-guardian bypass                 Flush all Data Guardian rules
data-guardian doctor [--json]        Diagnostics with per-check fixes
data-guardian install-shims          Write pip/pip3/npm/cargo shims
data-guardian remove-shims           Remove them
```

`python3 -m src.data_guardian` is equivalent when `PYTHONPATH` points at the
project root.

---

## Configuration

The installer seeds:

```text
~/.config/linux-data-guardian/config.yaml
~/.config/linux-data-guardian/whitelist.yaml
```

The defaults live in [`config/config.default.yaml`](config/config.default.yaml)
and [`config/whitelist.yaml`](config/whitelist.yaml). Missing keys fall back to
those defaults.

Quota example:

```yaml
quotas:
  daily_limit_mb: 1000        # metered only; 0 = unlimited
  monthly_limit_mb: 25000     # metered only; 0 = unlimited
  billing_cycle_day: 1        # 1-28
  warning_thresholds: [80, 95]
  action_on_exhaustion: "block_unapproved"   # or "warn_only"
  max_allowance_mb: 10000
```

Strict-mode example:

```yaml
strict_whitelist_processes:
  - opencode
  - antigravity

strict_allowed_ports_tcp:
  - 22
  - 80
  - 443
```

Local data:

| Path | Purpose |
| :--- | :--- |
| `~/.config/linux-data-guardian/config.yaml` | Profile, hotspot, quotas |
| `~/.config/linux-data-guardian/whitelist.yaml` | Strict process/port policy |
| `~/.local/share/linux-data-guardian/guardian.db` | Usage, quota state, notifications, approvals |
| `/run/data-guardian/data-guardian.sock` | User-only IPC socket |

All paths honour `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and `XDG_RUNTIME_DIR`.

---

## How it works

```text
systemd
  └─ data-guardian daemon
       ├─ NetworkMonitor        libnm / nmcli
       ├─ InterfaceByteCounter  /proc/net/dev
       ├─ Database              SQLite WAL, schema v3
       ├─ Quota engine          metered-only arithmetic
       ├─ NftablesManager       render rules, call helper
       │    └─ privileged helper ──► nft
       └─ IPC socket
            ├─ tray
            ├─ dashboard
            └─ CLI
```

- **The daemon is the only writer** of firewall state and quota state.
- **The helper is the only root process.** It accepts a fixed set of actions,
  validates every ruleset with `nft -c` before applying it, and touches only
  `table inet data_guardian`.
- **The UI never blocks on IPC.** Tray and dashboard use worker threads and
  `GLib.idle_add`.
- **Statistics and quota are separate.** Usage tables are for display;
  `quota_state` is the authority for limits, allowances, warnings, and blocks.

See **[File.md](File.md)** for the full file-by-file explanation.

---

## Safety and recovery

- All firewall rules live in `table inet data_guardian`. `ufw` and other tables
  are not modified.
- Every apply starts with `destroy table` in the same transaction, so retries
  and crashes cannot duplicate rules.
- If the daemon stops, `ExecStopPost` flushes the table.
- If you are locked out:

```bash
data-guardian bypass
# or, if Python is unavailable:
bash "$(cat ~/.config/linux-data-guardian/project_dir)/scripts/emergency-disable.sh"
# or directly:
sudo /usr/local/libexec/data-guardian-helper --flush
```

- The helper and sudoers rule can be checked with:

```bash
sudo -n /usr/local/libexec/data-guardian-helper --status
sudo visudo -cf /etc/sudoers.d/linux-data-guardian
```

### Privacy

- No telemetry, account, or remote service.
- Config and database are readable only by your user.
- The IPC socket is owner-only.
- When reporting a bug, **redact** network names, UUIDs, interface names,
  usernames, file paths, and any part of `config.yaml`, `guardian.db`, or logs
  that identifies your device or networks.

---

## Troubleshooting

Run this first:

```bash
data-guardian doctor
journalctl -u data-guardian.service -n 100 --no-pager
```

Common cases:

| Symptom | Check |
| :--- | :--- |
| Daemon offline | `sudo systemctl restart data-guardian.service` |
| IPC warning | Confirm `/run/data-guardian/data-guardian.sock` exists and is owned by you. |
| Firewall error | Confirm the helper is installed and the sudoers rule validates. |
| Strict blocks too much | Use `balanced`, or verify cgroup v2 and X11 focus tracking. |
| No tray icon | Install `xapp` or an AppIndicator package, then log out/in or run `data-guardian tray &`. |
| No approval dialog | Install `zenity`; otherwise the gate uses a terminal prompt. |
| Chart missing | Install `python3-matplotlib`; the rest of the dashboard still works. |
| APT still blocked | Open the APT window from the tray/dashboard or run `data-guardian allow-apt`. |

The dashboard's **CLI** tab has shortcuts for many of these checks.

---

## Uninstall

```bash
./scripts/uninstall.sh            # remove the app, keep config and history
./scripts/uninstall.sh --purge    # also delete config and usage history
```

If the installed runtime copy is separate, run its uninstaller:

```bash
"$HOME/linux-data-guardian-runtime/scripts/uninstall.sh" --purge
```

The uninstaller stops and disables the service, flushes the firewall table,
removes the helper, sudoers rule, icon, shims, wrapper, autostart entry, and
desktop entry. It does not delete the source clone or the runtime directory.

---

## Development

```bash
python3 -m pytest -q        # 130 hermetic tests, no root required
ruff check src tests        # optional; CI also runs it
```

The suite covers quota arithmetic, schema migration, nftables rendering and
validation, helper argument safety, IPC concurrency, configuration permissions,
installer safety, and daemon command handlers.

Pull requests run the same suite on Python 3.10 and 3.12.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for development rules and
[`SECURITY.md`](SECURITY.md) for the security model and reporting guidance.

---

## Project layout

```text
src/data_guardian/     application code
scripts/               installer, uninstaller, helper, deploy
config/                default config and sudoers template
systemd/               service and desktop templates
tests/                 hermetic test suite
assets/                application icon
File.md                file-by-file reverse-engineering guide
README.md              this file
```

`File.md` is the place to start if you want to understand the code rather than
just use it.
