# Linux Data Guardian

A local-first data guard for Linux laptops that sometimes run on a phone
hotspot. It watches NetworkManager, counts bytes from `/proc/net/dev`, enforces
quotas with an isolated `nftables` table, and gates package-manager downloads
while the designated metered connection is active.

Nothing leaves your machine: there is no telemetry, account, cloud service, or
network listener beyond a user-only Unix socket.

---

## What it actually does

* **Metered networks only.** Restrictions apply when NetworkManager reports the
  connection UUID/name you designated. Home and office networks are untouched
  and their traffic is still counted separately.
* **Metered-only quotas.** Daily and monthly budgets sum only rows recorded
  while the designated hotspot was active. Ordinary Wi-Fi usage cannot consume
  your mobile budget.
* **Two enforcement profiles plus off:**

  | Profile | Behaviour on the metered interface |
  | :--- | :--- |
  | `balanced` (default) | Allows established connections, DNS/DHCP/NTP, and all normal user traffic. New connections owned by UID 42 (APT) are dropped. |
  | `strict` | Only new connections from the focused X11 application's cgroup or the whitelisted tools (`opencode`, `antigravity`) are allowed. Without cgroup v2 delegation it falls back to a TCP port allowlist (22, 80, 443). Everything else is dropped. |
  | `off` | Monitoring only; the `data_guardian` table is removed. |

* **Quota enforcement.** At 80% and 95% of the daily budget you get one
  notification per threshold per day. At 100% (or when the monthly budget is
  reached) new outbound traffic is paused unless `action_on_exhaustion` is
  `warn_only`. A manually granted allowance raises today's effective limit.
* **Temporary APT window.** APT downloads are blocked while metered. Use
  `data-guardian allow-apt` (or the tray item) to open a timed window (default
  15 minutes, maximum 120). It closes automatically and the rules are
  re-applied. There is **no** root-owned APT hook or root Python import.
* **Download approval for CLI tools.** shims for `pip`, `pip3`, `npm`, and
  `cargo` ask for confirmation before download-style commands on a metered
  connection. Read-only commands (`pip list`, `npm --version`, ...) pass
  straight through.
* **Local history.** SQLite in WAL mode with 0600 permissions, pruned after 90
  days by default.
* **Status, dashboard, and tray.** A CLI (`status --json`), a GTK3 dashboard,
  and an XApp/AppIndicator/GTK status icon.

### Known limitations

* Strict-mode focus tracking is X11-only. On Wayland the focused-app cgroup is
  unavailable, so use `balanced` or the port fallback.
* cgroup matching requires cgroup v2 and the privileged helper. The installer
  installs it; `data-guardian doctor` reports whether it is usable.
* The APT window is a time box, not a package inspection: it allows UID 42 new
  connections for the chosen duration.
* The dashboard's chart requires `python3-matplotlib`; without it the rest of
  the dashboard still works.
* Memory use is tens of MiB, not the 35 MB claimed by earlier drafts. Measured
  RSS on Linux Mint XFCE (Python 3.12 + GTK3) was about 49 MiB for the daemon
  and 86–129 MiB for the GTK tray/dashboard clients, depending on GTK caches.

---

## Requirements

* Linux with systemd, NetworkManager (`nmcli`), nftables, and Python 3.10+.
* `python3-yaml`, `python3-gi`, GTK 3; `zenity` for approval dialogs.
* `xapp` or `libayatana-appindicator3` for the tray (falls back to
  `Gtk.StatusIcon`).
* Root only for the helper, sudoers rule, systemd unit, and icon.

## Install

```bash
git clone https://github.com/jagdishsah126/Linux-Data-Guardian.git
cd Linux-Data-Guardian

# 1. User-level: wrapper, config seed, shims, autostart entry, desktop file
./scripts/install.sh

# 2. System-level: helper, sudoers rule, systemd unit, icon
sudo ./scripts/install.sh --system
```

The user step never asks for a password. The system step installs exactly one
NOPASSWD command (`/usr/local/libexec/data-guardian-helper`); it does not grant
general sudo and writes no APT configuration.

Then designate your hotspot and check the installation:

```bash
data-guardian set-hotspot        # interactive picker
data-guardian doctor             # full health check
data-guardian status
```

The daemon runs as `data-guardian.service`; the tray starts from the XFCE
autostart entry on the next login.

### Deploying a runtime copy

`scripts/deploy.sh [destination]` rsyncs only `src/`, `scripts/`, `config/`,
`systemd/`, and `assets/` into a plain artifact directory (default:
`/home/jagdish/Desktop/System Files/Linux Data Guardian`). It refuses to write
into a second git clone and records the source commit in `DEPLOYED`.

---

## CLI

```
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

`--version` prints the version. `python3 -m src.data_guardian` is equivalent
when `PYTHONPATH` points at the project root.

---

## Files

| Path | Purpose |
| :--- | :--- |
| `~/.config/linux-data-guardian/config.yaml` | Quotas, profile, designated hotspot |
| `~/.config/linux-data-guardian/whitelist.yaml` | Strict-mode process/port allowlist |
| `~/.local/share/linux-data-guardian/guardian.db` | Usage, quota state, notifications, approvals |
| `/run/data-guardian/` | 0600 Unix socket + instance lock (system service; manual runs use `$XDG_RUNTIME_DIR` or `/run/user/<uid>`) |
| `/usr/local/libexec/data-guardian-helper` | Privileged firewall/cgroup helper |
| `/etc/systemd/system/data-guardian.service` | Daemon unit |

All paths honour `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and `XDG_RUNTIME_DIR`.

### Quota configuration

```yaml
quotas:
  daily_limit_mb: 1000        # metered only; 0 = unlimited
  monthly_limit_mb: 25000     # metered only; 0 = unlimited
  billing_cycle_day: 1        # 1-28
  warning_thresholds: [80, 95]
  action_on_exhaustion: "block_unapproved"   # or "warn_only"
  max_allowance_mb: 10000
```

Changing the daily limit updates the stored limit on the next evaluation.
Allowances never survive into the next day.

---

## Safety and recovery

* All rules live in `table inet data_guardian`; `ufw` and other tables are not
  modified.
* Every apply starts with `destroy table` in the same transaction, so retries
  and crashes cannot duplicate rules.
* If the daemon dies, `ExecStopPost` flushes the table. A live daemon keeps its
  socket; `data-guardian bypass` asks it to flush rather than deleting the
  socket.
* `./scripts/emergency-disable.sh` works even if Python is broken: it tries the
  daemon, then the helper directly.

Uninstall:

```bash
./scripts/uninstall.sh            # remove app + system integration, keep data
./scripts/uninstall.sh --purge    # also delete config and usage history
sudo ./scripts/uninstall.sh --purge   # when started through sudo
```

---

## Development

```bash
python3 -m pytest -q        # 130 hermetic tests, no root required
ruff check src tests        # optional; CI runs it
```

The test suite covers quota arithmetic, schema v2 → v3 migration, ruleset
rendering plus real `nft -c` parsing, helper argument validation, IPC
concurrency, configuration hardening, installer safety, and daemon command
handlers. See `ImprovePlan.md` for the audit that produced the current design.

Pull requests run the same suite on Python 3.10 and 3.12 via GitHub Actions.
