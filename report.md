# Linux Data Guardian — Final Engineering & System Audit Report

**Date:** October 19, 2026  
**System:** Linux Mint 22.3 XFCE (Dell Inspiron 3501, Kernel 6.8, x86_64)  
**Production Root:** `/home/jagdish/Desktop/System Files/Linux Data Guardian`  
**GitHub Repository:** `https://github.com/jagdishsah126/Linux-Data-Guardian.git` (`main`)  
**Status:** **Production Ready & Verified Active**

---

## 1. Executive Summary

**Linux Data Guardian** is a lightweight, local-first mobile SIM data saver, bandwidth accounting engine, and download gating manager built specifically for Linux Mint XFCE. It monitors cellular hotspot connections, prevents background operating system updaters and telemetry from consuming metered cellular data, enforces daily and monthly quotas, and provides both an interactive desktop dashboard and a native panel tray icon.

Following an extensive architectural and security review, all critical vulnerabilities (`SEC-001`, `SEC-002`, `SEC-003`) and accounting bugs (`BUG-001`, `BUG-002`, `BUG-003`) were resolved using the user-approved **Option A** framework. All features have undergone live testing on active mobile tethering and pass 100% of the automated unit test suite.

---

## 2. Security & Architecture Audit Results

| Item ID | Description | Severity | Resolution Implemented | Status |
| :--- | :--- | :--- | :--- | :--- |
| **SEC-001** | Unprivileged daemon failed to apply nftables rules | **Critical** | Created standalone root-owned privileged helper (`/usr/local/libexec/data-guardian-helper`) with targeted sudoers policy. | **RESOLVED** ✅ |
| **SEC-002** | Root APT hook executed user-writable Python code (LPE vector) | **Critical** | Removed root APT hook. Replaced with kernel-level isolation (`skuid 42 ct state new drop` on metered interface) with a 15-minute temporary allowance window. | **RESOLVED** ✅ |
| **SEC-003** | Strict profile dropped all traffic due to missing cgroup hierarchy | **High** | Scoped Strict mode to drop unapproved ports while safely whitelisting HTTP (80), HTTPS (443), SSH (22), DNS (53), DHCP, and loopback. | **RESOLVED** ✅ |
| **BUG-001** | Per-network traffic accounting was not wired into daemon loop | **Medium** | Connected daemon monitoring loop to SQLite table `network_daily_usage` (schema v2) with UUID indexing. | **RESOLVED** ✅ |
| **BUG-002** | Established connections continued streaming data after quota exhaustion | **High** | Dropped all packets on the metered interface upon exhaustion (`oif "$IFACE" drop`) to halt active downloads immediately. | **RESOLVED** ✅ |
| **BUG-003** | Silent notification failures when daemon ran as background service | **Medium** | Built daemon-to-client IPC notification queue; notifications are delivered natively by the tray/dashboard in user session. | **RESOLVED** ✅ |

---

## 3. Implemented Features & Verification

### 3.1 Protection Modes
* **OFF (Monitor Only):** Zero firewall rules applied. Network traffic flows unrestricted; data accounting continues in SQLite. Used for unmetered home/office broadband (e.g. `ALHN-449C`).
* **BALANCED (Everyday Mobile Hotspot):** Blocks background OS updates (`_apt` UID 42) and high-bandwidth P2P ports at the Linux kernel level. Browsing (HTTP/HTTPS), DNS, SSH, and email remain fast and unrestricted.
* **STRICT (Maximum Data Preservation):** Blocks all non-essential ports. Restricts outbound traffic on the metered interface strictly to HTTP (80), HTTPS (443), SSH (22), and DNS (53). Loopback is unrestricted.

### 3.2 Privileged Helper (`/usr/local/libexec/data-guardian-helper`)
* **Ownership:** `root:root (0755)`
* **Sudoers Scoping:** `/etc/sudoers.d/linux-data-guardian` (`0440 root:root`) grants execution strictly to this helper without password prompt.
* **Argument Sanitation:** Accepts only fixed subcommands (`--status`, `--flush`, `--apply-balanced <iface>`, `--apply-strict <iface>`, `--apply-blocked <iface>`, `--allow-apt-window <iface>`). Interface names are strictly validated against regex `^[a-zA-Z0-9_\.\-]+$`.
* **Atomic Pre-validation:** Every generated ruleset is syntax-checked via `nft -c -f -` before being committed to the kernel.
* **Isolation:** Modifies **only** `table inet data_guardian`. Does not touch UFW, Docker, or system iptables.

### 3.3 Graphical Dashboard & Tray
* **Overview Tab:** Shows live connection status, active focused application, download/upload byte counters, and a network selector dropdown with independent profile toggles.
* **Statistics Tab:** Renders an interactive 60-day usage history line chart (rendered natively via Cairo) with 1, 7, 15, 30, 45, and 60-day filter options and per-network breakdowns.
* **CLI Commands Tab:** Offers quick-action buttons for common operations (`status`, `balanced`, `strict`, `off`, `allow-apt`, `diagnostics`) with inline color-coded terminal output.
* **Native System Tray Applet:** Integrates with Linux Mint XFCE panel using `XApp.StatusIcon` and the custom Data Guardian shield icon.

---

## 4. Duplicate Artifacts & System Footprint Inspection

During development across multiple phases, duplicate assets and development artifacts were identified and cleaned up as follows:

### 4.1 Icon Files (Cleaned Up)
* `/usr/share/icons/hicolor/512x512/apps/linux-data-guardian.png`  
  *(**Primary / Active**: Preserved as the single authoritative icon for desktop launchers, application menu, and GTK panel).*
* `~/.local/share/icons/linux-data-guardian.png`  
  *(**Removed**: Cleaned up duplicate).*
* `~/.local/share/icons/hicolor/512x512/apps/linux-data-guardian.png`  
  *(**Removed**: Cleaned up duplicate).*

---

### 4.2 Workspace Separation (Build vs. Production Deployment)
* `/home/jagdish/Desktop/Projects/Zara/Linux Data Guardian/`  
  *(**Development & Build Workspace**: Contains full test suite (`tests/`), development planning docs (`plan.md`, `audit.md`, `step.md`), and build assets. All tests pass here via `pytest`).*
* `/home/jagdish/Desktop/System Files/Linux Data Guardian/`  
  *(**Live Production Deployment**: Contains strictly runtime code (`src/`), config templates (`config/`), scripts (`scripts/`), and systemd units (`systemd/`). All tests, planning docs, and cache artifacts have been removed to keep the deployment lean and lightweight).*

---

### 4.3 Root Artifacts Cleaned Up
* Loose duplicate root image `wifi.png` removed (preserved in `assets/wifi.png`).
* Temporary scratch file `SUDO_INSTALL_STEPS.txt` removed.
* Local `.pytest_cache/` and `__pycache__` artifacts purged from deployment root.

---

### 4.4 CLI Interception Shims in `~/.local/bin/` (4 Installed)
* `~/.local/bin/pip`
* `~/.local/bin/pip3`
* `~/.local/bin/npm`
* `~/.local/bin/cargo`

These scripts were installed by `data-guardian install-shims` to intercept package installation commands and ask for confirmation before downloading on metered mobile data.
* If you run `pip install` or `npm install`, these shims verify if your active connection is metered before proceeding.
* If you prefer to rely on kernel-level firewall gating and keep your default terminal tools unintercepted, you can remove these shims at any time by running:
  ```bash
  data-guardian remove-shims
  ```

---

### 4.5 Co-existing Project: "Linux Data Monitor"
* Location: `/home/jagdish/Desktop/System Files/Linux Data Monitor/`
* Process: `dm-collector` (PID running on system).
* Symlinks: `~/.local/bin/data-monitor`, `dm-collector`, `dm-panel`, `dm-popup`.
* Note: This is an independent lightweight monitor tool created on Oct 19. It operates on separate database files and does not conflict with Linux Data Guardian.

---

## 5. System Inventory & File Locations

| Path | Purpose | Permissions | Management |
| :--- | :--- | :--- | :--- |
| `/usr/local/libexec/data-guardian-helper` | Root execution helper for nftables | `0755 root:root` | Sudo-controlled binary |
| `/etc/sudoers.d/linux-data-guardian` | Scoped sudoers policy for helper | `0440 root:root` | Sudoers configuration |
| `/etc/systemd/system/data-guardian.service` | Background systemd monitoring daemon | `0644 root:root` | System service (`active`) |
| `/usr/share/icons/hicolor/512x512/apps/linux-data-guardian.png` | Application & Tray icon | `0644 root:root` | Icon theme asset |
| `~/.local/bin/data-guardian` | User CLI executable wrapper | `0755 jagdish` | User executable |
| `~/.local/share/applications/data-guardian.desktop` | Application menu launcher | `0644 jagdish` | XDG Desktop Entry |
| `~/.config/autostart/data-guardian-tray.desktop` | Auto-start tray launcher on desktop login | `0644 jagdish` | XDG Autostart Entry |
| `~/.config/linux-data-guardian/config.yaml` | User configuration and quotas | `0644 jagdish` | YAML configuration |
| `~/.local/share/linux-data-guardian/guardian.db` | Persistent SQLite bandwidth database | `0600 jagdish` | SQLite database (WAL mode) |
| `/run/user/1000/data-guardian.sock` | Daemon IPC UNIX domain socket | `0600 jagdish` | Runtime socket |

---

## 6. Maintenance & Operational Commands

### Check Current Status & Active Profile
```bash
data-guardian status
```

### Switch Protection Profiles
```bash
data-guardian profile balanced   # Default mobile data saver
data-guardian profile strict     # High-restriction mode
data-guardian profile off        # Full unmetered access
```

### Open Temporary 15-Minute APT Window
```bash
data-guardian allow-apt
# or click "📦 Allow APT" in the dashboard
```

### Grant Extra Quota Allowance
```bash
data-guardian allow 500          # Adds +500 MB to today's budget
```

### Launch Graphical Dashboard
```bash
data-guardian dashboard
```

### Emergency Restore (Instant Network Flush)
If you ever want to instantly tear down all firewall rules:
```bash
bash "/home/jagdish/Desktop/System Files/Linux Data Guardian/scripts/emergency-disable.sh"
```

### Clean Uninstallation
If you ever want to cleanly remove Linux Data Guardian completely:
```bash
bash "/home/jagdish/Desktop/System Files/Linux Data Guardian/scripts/uninstall.sh"
```
*(The uninstall script cleans up the helper, sudoers file, systemd service, desktop launchers, shims, and icons without leaving orphaned rules).*
