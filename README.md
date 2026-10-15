# Linux Data Guardian

> **A local-first, low-overhead mobile data saver and download approval manager built for Linux Mint XFCE.**

When tethered to a smartphone’s mobile data pack (e.g., NTC / Ncell mobile SIM data), Linux Data Guardian protects your data from background operating system updates, telemetry, unapproved package downloads, and background tabs.

---

## 🌟 Key Features

* **Automatic Hotspot Scoping:** Restrictions activate *only* when connected to your designated mobile hotspot. Normal Wi-Fi (home/office) remains 100% unrestricted.
* **Dual-Profile Data Saver:**
  * **Balanced Mode (Default):** Silences OS background updates (`apt-daily`, `packagekitd`, `unattended-upgrades`), blocks telemetry, allows normal web surfing (Brave/Firefox) and whitelisted developer tools (OpenCode CLI, Antigravity CLI, Git), and gates package manager downloads.
  * **Strict Mode (Foreground-Only):** Extends Balanced mode by routing network access *only* to the window currently active in XFCE (via X11 `_NET_ACTIVE_WINDOW`) plus whitelisted background coding tools.
* **"Download Only When I Approve" Gating:**
  * Pre-download inspection for **APT** (`apt install`, `apt upgrade`).
  * Transparent shims for developer tools (**pip**, **npm**, **cargo**).
  * Prompts with a desktop modal dialog showing the command and download scope before any bytes transfer.
* **Accurate Traffic Accounting & Quotas:**
  * Exact byte counts read directly from `/proc/net/dev` (`wlp0s20f3`).
  * Daily and monthly budgets with alerts at 80% and automated pause at 100%.
  * Local SQLite storage in `~/.local/share/linux-data-guardian/guardian.db` (WAL mode).
* **Linux Mint Native Bottom Status Bar Tray Applet:**
  * Built using native `XApp.StatusIcon` to dock directly in Linux Mint's bottom panel near the clock.
  * Right-click menu for quick profile toggling, temporary allowance grants, and emergency bypass.
  * Native GTK3 dashboard with progress bar, recent approval logs, and "+100 MB" allowance grants.
* **Fail-Safe Isolated Firewall (`nftables`):**
  * All rules reside in an isolated `table inet data_guardian`.
  * Never touches or interferes with system `ufw`.
  * One-command emergency recovery: `scripts/emergency-disable.sh`.

---

## 🚀 Step-by-Step Configuration Guide

### Step 1: Install User-Level Components
Clone the repository and run the installer:
```bash
git clone https://github.com/jagdishsah126/Linux-Data-Guardian.git
cd Linux-Data-Guardian
./scripts/install.sh
```
This script:
1. Links the `data-guardian` command into `~/.local/bin/`.
2. Installs transparent CLI shims for `pip`, `npm`, and `cargo`.
3. Registers the tray applet in your XFCE session autostart so it launches whenever you log in.

*(Optional: If you want kernel-level firewall enforcement and APT download gating, also run `sudo ./scripts/install.sh --system`)*.

---

### Step 2: Configure Your Designated Mobile Hotspot

You only need to designate your phone's hotspot **once**. Data Guardian remembers it permanently.

#### Method A: From the Graphical Dashboard (Recommended)
1. Open the dashboard:
   ```bash
   data-guardian dashboard
   ```
2. Locate the **"Active Network Connection"** section.
3. In the **"Designated Hotspot"** dropdown menu, select your phone's hotspot name.
4. Done!

#### Method B: Interactively from Terminal
Run:
```bash
data-guardian set-hotspot
```
You will see a numbered list of all your saved Wi-Fi connections. Simply type the number corresponding to your mobile hotspot:
```text
=== Select Designated Mobile Hotspot ===
  [1] ALHN-449C (UUID: a054f084...)
  [2] Moto Mobile (UUID: 590f9bbc...)
  [3] Hotspot-College (UUID: 1dd269c3...)

Enter number (1-3) or 'q' to cancel: 2
[✓] Successfully set designated hotspot to: 'Moto Mobile'
```

#### Method C: Directly by Name
```bash
data-guardian set-hotspot "Moto Mobile"
```

---

### Step 3: Start the Background Services

To start Data Guardian immediately:
```bash
# 1. Start the background monitoring daemon
data-guardian daemon &

# 2. Start the bottom status bar tray icon
data-guardian tray &
```

---

### Step 4: Using the Bottom Status Bar Tray Icon

Once started, the Data Guardian icon (shield / network symbol) appears in your **bottom status bar** (next to your battery icon and clock).

* **Left-Click:** Instantly opens the graphical **Dashboard**.
* **Right-Click:** Opens the quick-action menu:
  * **● Balanced Data Saver:** Allows web surfing and coding tools; silences background OS updates and telemetry.
  * **● Strict (Foreground Only):** Gives internet access *only* to the window you are actively looking at.
  * **○ Data Saver Off:** Monitoring only, no restrictions.
  * **+100 MB Temporary Allowance:** Grants a quick extra allowance if your daily budget is reached.
  * **Emergency Bypass:** Instantly removes all firewall restrictions.

---

### Step 5: How "Download Only When I Approve" Works

When connected to your designated mobile hotspot, package managers cannot download in the background without your explicit consent:

1. **Terminal Tools (`pip`, `npm`, `cargo`):**
   * If you run `pip install <package>` or `npm install <package>`, Data Guardian pops up a desktop dialog:
     ```text
     [ Linux Data Guardian — Download Approval ]
     Tool: pip
     Command: install requests
     Do you want to approve this download on metered data?
     [ Approve Download ]    [ Deny / Cancel ]
     ```
   * Clicking **Approve** allows the download to proceed.
   * Clicking **Deny** cancels the command immediately before any data is transferred.
   * Read-only commands like `pip list` or `npm -v` pass through instantly without prompting.

2. **APT System Updates (`apt install`, `apt upgrade`):**
   * When installed with `--system`, APT triggers a pre-download gate before fetching archive packages. Denying the prompt aborts APT safely.

---

## 🛠️ Handy CLI Commands

| Command | Description |
| :--- | :--- |
| `data-guardian status` | View active network, profile, today's RX/TX data usage, and quota status. |
| `data-guardian dashboard` | Open the GTK3 control window with live progress bar and recent logs. |
| `data-guardian tray` | Launch the bottom panel tray icon. |
| `data-guardian set-hotspot [name]` | Interactively pick or set your designated mobile hotspot. |
| `data-guardian profile <balanced\|strict\|off>` | Switch Data Saver mode from terminal. |
| `data-guardian allow <MB>` | Grant temporary megabytes to today's budget (e.g. `data-guardian allow 200`). |
| `data-guardian bypass` | Instantly flush all firewall rules and lift all restrictions. |
| `./scripts/emergency-disable.sh` | Standalone emergency script to flush rules even if Python is stopped. |
| `./scripts/uninstall.sh` | Cleanly uninstalls all shims, configs, and autostart entries. |

---

## ⚙️ Configuration Files

All configuration files are stored locally in your home directory:

* `~/.config/linux-data-guardian/config.yaml`: Quota limits, timeouts, and designated hotspot UUID.
* `~/.config/linux-data-guardian/whitelist.yaml`: Whitelisted developer tools (`opencode`, `antigravity`, `git`, `python3`).
* `~/.local/share/linux-data-guardian/guardian.db`: SQLite database storing historical usage and approval audit logs.

### Adjusting Quotas:
In `~/.config/linux-data-guardian/config.yaml`:
```yaml
quotas:
  daily_limit_mb: 1000     # Daily mobile data cap (MB)
  monthly_limit_mb: 25000  # Monthly mobile data cap (MB)
  warning_threshold_percent: 80
  action_on_exhaustion: "block_unapproved"
```

---

## 🛡️ Safety & Fail-Safe Architecture

1. **Isolated Firewall:** All filtering is contained within `table inet data_guardian` in `nftables`. It will never modify or break your system `ufw` configuration.
2. **Fail-Open Recovery:** If you ever need immediate open networking, running `./scripts/emergency-disable.sh` flushes Data Guardian rules atomically in 5 milliseconds.
3. **Low Resource Footprint:** Designed specifically for 4GB RAM laptops: uses **< 35 MB of RAM** and **< 0.2% CPU** when idle.
