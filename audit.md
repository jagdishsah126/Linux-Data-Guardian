# Linux Data Guardian — Comprehensive Independent Audit Report (Revision 2)

**Branch:** `main`  
**Commit Audited:** `7caf94d` (working tree clean)  
**Audit Date:** 2026-10-15  
**Review Type:** Deep Security Architecture, Privilege Boundary, and Implementation Verification  

---

## 1. Executive Summary

Linux Data Guardian is currently a functional monitoring and UI prototype, but **its security architecture, privilege boundary, and firewall enforcement have critical vulnerabilities and structural gaps**.

Prior assumptions—specifically that simple sudoers additions or environment variable hacks in root hooks could fix privileged operations—are invalid and dangerous. Specifically, running root hooks against user-writable Python repositories introduces a **Critical Local Privilege Escalation (LPE)** risk. Furthermore, firewall enforcement is currently completely non-operational because the unprivileged user daemon lacks an authorized execution path to `nft`.

### Current System Health Matrix

| Subsystem | Verified State | Production Readiness |
|---|---|---|
| **Network Detection** | Verified libnm/nmcli polling; UUID matching logic works. | 🟡 Usable, requires strict manual assignment lock. |
| **Traffic Accounting** | Verified `/proc/net/dev` interface reader and delta math. | 🟢 Working correctly at interface level. |
| **Database & Storage** | SQLite schema v2 with WAL mode; sessions and daily tables. | 🟡 Working, needs live per-network wiring. |
| **Dashboard UI & CLI** | GTK3 3-tab layout, live chart, interactive command runner. | 🟢 Working and responsive. |
| **System Tray** | XApp.StatusIcon native to Linux Mint panel. | 🟢 Verified showing in panel. |
| **Firewall Enforcement** | `apply_profile()` fails silently (unprivileged user lacks sudo). | 🔴 **Broken (Zero enforcement active).** |
| **Strict Mode (Cgroups)** | References non-existent `/sys/fs/cgroup/data_guardian`. | 🔴 **Non-functional / Hazardous if enabled.** |
| **APT Gating Hook** | Root hook executes code from user-writable desktop path. | 🔴 **Critical Security Risk (LPE).** |
| **Desktop Notifications** | Silent failure under systemd due to missing session bus/display. | 🔴 Broken in background service. |

---

## 2. Feature Implementation Matrix

| Feature | Design Specification | Verified Actual Behavior | Verdict & Impact | Evidence (File & Line) |
|---|---|---|---|---|
| **Daemon Execution** | Auto-starts at boot, runs persistently as `jagdish`. | Daemon runs active (15.5 MB RAM) via systemd. | 🟢 **Verified Functional** | `systemd/data-guardian.service` |
| **Hotspot Detection** | Identifies designated mobile hotspot by UUID/name. | Matches UUID/name; manual setting in `config.yaml`. | 🟡 **Functional with Caveats** (Flapping risk if UI alters config) | `src/data_guardian/network/nm_monitor.py:115-158` |
| **Interface Accounting** | Tracks delta RX/TX bytes from `/proc/net/dev`. | Parses active interface lines, handles counter reset. | 🟢 **Verified Functional** | `src/data_guardian/network/byte_counter.py:46-95` |
| **Per-Network DB Stats** | Stores daily RX/TX segregated by connection UUID. | Table exists (schema v2); populated via migration, but daemon omits UUID during runtime. | 🟡 **Partially Integrated** (Historical OK, live live-feed missing UUID) | `src/data_guardian/database.py:65-85`, `daemon.py:149-153` |
| **Daily Quota Evaluation** | Compares today's total bytes against `daily_limit_mb`. | Evaluates threshold, sets `_is_quota_blocked`, but firewall fails to apply. | 🟡 **Logical Only** (No kernel packet drop) | `src/data_guardian/daemon.py:149-178` |
| **Monthly Quota Enforcement**| Evaluates monthly usage against `monthly_limit_mb`. | Config key exists; daemon never evaluates it. | 🔴 **Missing Implementation** | `src/data_guardian/daemon.py` |
| **Balanced Profile Firewall**| Drops `_apt` (UID 42), permits general web surfing. | Ruleset string built correctly, but `nft` call fails: `sudo: a password is required`. | 🔴 **Non-Functional** | `src/data_guardian/firewall/nft_manager.py:35-55` |
| **Strict Profile Firewall** | Restricts traffic exclusively to foreground window. | nftables rule looks for cgroupv2 path that is never initialized. | 🔴 **Hazardous / Broken** | `src/data_guardian/firewall/rulesets.py:47-55` |
| **APT Download Interception**| Prompts approval before downloading package archives. | `/etc/apt/apt.conf.d/99data-guardian` executes user desktop Python script as root. | 🔴 **Critical Privilege Flaw** | `/etc/apt/apt.conf.d/99data-guardian` |
| **CLI Shims (pip/npm/cargo)**| Gating wrapper scripts in `~/.local/bin`. | Shims exist and intercept user execution; prompt runs in user context. | 🟢 **Verified Logic** | `src/data_guardian/approvals/shims.py:12-42` |
| **Emergency Recovery** | Flushes rules without breaking existing UFW/system rules. | `scripts/emergency-disable.sh` targets only `table inet data_guardian`. | 🟢 **Verified Safe** | `scripts/emergency-disable.sh` |
| **Per-Network Profile** | Remembers profile (Balanced/Strict/Off) per network. | Persisted in `config.yaml` by UI, but daemon ignores per-network profile on network transition. | 🟡 **Partially Implemented** | `src/data_guardian/ui/dashboard.py`, `daemon.py:86-105` |

---

## 3. Detailed Critical & High-Priority Findings

### SEC-001: Absence of Validated Privileged Execution Path for Firewall
- **Severity:** Critical  
- **Confidence:** Confirmed (Live Verification: `sudo -n true` exits 1, `nft` requires root)  
- **Technical Analysis:** The background service runs as unprivileged user `jagdish` (`User=jagdish` in systemd). `NftablesManager.apply_profile()` executes `sudo -n nft -f -`. Because `jagdish` does not have passwordless sudo for `nft`, the command fails with `sudo: a password is required`. The daemon silently discards the returned `(False, error_msg)` tuple (`daemon.py:100`).
- **Hazardous Misconception:** Granting `jagdish ALL=(root) NOPASSWD: /usr/sbin/nft` in sudoers is **extremely insecure**. A user with arbitrary `nft` access can flush the entire system firewall (`nft flush ruleset`), disable UFW, redirect arbitrary traffic, or eavesdrop on unencrypted network packets.
- **Root Cause:** Lack of a dedicated, root-owned privileged helper with strict command validation.

---

### SEC-002: Insecure Execution of Unprivileged Code from Root Context (APT Hook)
- **Severity:** Critical (Local Privilege Escalation Vulnerability)  
- **Confidence:** Confirmed (`ls -ld` on repo shows owner `jagdish:jagdish`, mode `0775`)  
- **Technical Analysis:** `/etc/apt/apt.conf.d/99data-guardian` invokes:
  ```
  APT::Acquire::Pre-Invoke {
      "PYTHONPATH='/home/jagdish/Desktop/System Files/Linux Data Guardian' /usr/bin/python3 -m src.data_guardian.approvals.gate --tool apt install || exit 1";
  };
  ```
  When `sudo apt install` or unattended upgrades run, the hook executes with **root privileges**. It imports Python modules directly from `/home/jagdish/Desktop/System Files/...`, which is writable by unprivileged user `jagdish`.
- **Exploitation Scenario:** Any malicious process, script, or extension running under the unprivileged user account can modify `gate.py` or inject malicious code into `__init__.py`. The next time the user runs `sudo apt`, root executes the compromised script, granting instant, undetectable root compromise.
- **Secondary Failure:** When executed by root, `Path.home()` evaluates to `/root`, looking for `/root/.config/linux-data-guardian/config.yaml`. That file does not exist, causing `is_designated_hotspot_active()` to return `False` and silently pass through without gating.

---

### SEC-003: Strict Profile cgroupv2 Hierarchy Completely Missing
- **Severity:** High  
- **Confidence:** Confirmed (`ls /sys/fs/cgroup/data_guardian` returns `No such file or directory`)  
- **Technical Analysis:** `rulesets.py:50` generates:
  ```nft
  socket cgroupv2 level 2 "data_guardian/foreground" accept
  socket cgroupv2 level 2 "data_guardian/whitelist" accept
  ct state new drop
  ```
  No component of the application ever creates `/sys/fs/cgroup/data_guardian/foreground` or attaches process PIDs to `cgroup.procs`.
- **Failure Impact:** If firewall enforcement were active, switching to Strict mode would drop **100% of all new outbound connections** for all applications, including the active browser, rendering networking entirely dead.

---

### BUG-001: Live Traffic Accounting Omits Network Identity
- **Severity:** Medium  
- **Confidence:** Confirmed (`daemon.py:149-153`)  
- **Technical Analysis:** In `daemon.py`, the periodic loop records traffic deltas via:
  ```python
  usage = self.db.record_usage_delta(
      delta_rx=delta_rx,
      delta_tx=delta_tx,
      quota_limit_bytes=daily_limit_bytes
  )
  ```
  It fails to pass `connection_uuid` and `connection_name`. Consequently, while the combined `daily_usage` table increments, the new `network_daily_usage` table receives zero updates during live daemon execution. The per-network history and Statistics tab only reflect data imported during one-off migrations.

---

### BUG-002: Quota Exhaustion Fails to Terminate Existing Established Connections
- **Severity:** High  
- **Confidence:** Confirmed (`rulesets.py:27`)  
- **Technical Analysis:** The nftables ruleset places:
  ```nft
  ct state established,related accept
  ```
  **above** the quota exhaustion drop rule:
  ```nft
  ct state new drop
  ```
- **Real-World Failure:** When a daily limit (e.g., 1000 MB) is exceeded while a large file download (ISO, video stream, torrent, system update) is in progress, the active TCP connection is `established`. `ct state new drop` only blocks *new* connection handshakes. The active download will continue transferring data uninhibited until completion, potentially incurring significant mobile carrier charges.

---

### BUG-003: Notification Delivery Architecture Broken Under Background Service
- **Severity:** Medium  
- **Confidence:** Confirmed (`systemd/data-guardian.service` lacks `DISPLAY` and session bus)  
- **Technical Analysis:** `daemon.py:78` tests `os.environ.get("DISPLAY")`. Because the service runs as a background systemd service without an attached display server, `notify-send` is never called.
- **Failure Impact:** Quota alerts (80%, 100%), hotspot transitions, and emergency bypass notifications are never displayed to the user.

---

## 4. Privileged Helper Architectural Comparison

To eliminate `SEC-001` without introducing security regressions, three architectural patterns were evaluated:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          ARCHITECTURAL COMPARISON                           │
├────────────────────────────┬────────────────────────────┬───────────────────┤
│ Option A: Sudo / Polkit    │ Option B: Root Daemon      │ Option C: Root    │
│ Direct `nft` Execution     │ Persistent UNIX Socket     │ Privileged Helper │
├────────────────────────────┼────────────────────────────┼───────────────────┤
│ User runs `sudo nft`       │ Persistent daemon as root  │ Minimal root      │
│ directly.                  │ with socket peer check.    │ executable run    │
│                            │                            │ via strict sudo.  │
│ ❌ CRITICAL RISK:          │ ⚠️ MEDIUM RISK:            │ 🟢 MINIMAL RISK:  │
│ Allows flushing all system │ Long-running root process  │ Zero persistent   │
│ firewalls, UFW tampering,  │ listening on IPC; complex  │ root memory; runs │
│ and packet sniffing.       │ process management.        │ only on transition│
│                            │                            │ with strict args. │
└────────────────────────────┴────────────────────────────┴───────────────────┘
```

### Detailed Evaluation of Option C (Recommended):

A dedicated, root-owned helper script or compiled binary installed at:
`/usr/local/libexec/data-guardian-helper` (Permissions: `0755 root:root`).

**Supported Operations Contract:**
1. `--apply-balanced <interface>`: Validates interface with regex `^[a-zA-Z0-9_\.\-]+$`, checks syntax with `nft -c`, applies rules atomically to `table inet data_guardian`.
2. `--apply-strict <interface>`: Validates interface and cgroup prerequisites.
3. `--apply-blocked <interface>`: Applies hard cut-off rules and flushes non-essential established states.
4. `--flush`: Deletes `table inet data_guardian` only.
5. `--status`: Queries `nft list table inet data_guardian` and returns exit code 0 if active.

**Sudoers Authorization Specification:**
In `/etc/sudoers.d/linux-data-guardian`:
```sudoers
jagdish ALL=(root) NOPASSWD: /usr/local/libexec/data-guardian-helper --apply-balanced [a-zA-Z0-9_\.\-]*
jagdish ALL=(root) NOPASSWD: /usr/local/libexec/data-guardian-helper --apply-strict [a-zA-Z0-9_\.\-]*
jagdish ALL=(root) NOPASSWD: /usr/local/libexec/data-guardian-helper --apply-blocked [a-zA-Z0-9_\.\-]*
jagdish ALL=(root) NOPASSWD: /usr/local/libexec/data-guardian-helper --flush
jagdish ALL=(root) NOPASSWD: /usr/local/libexec/data-guardian-helper --status
```
**Security Guarantees:**
- No arbitrary commands can be passed to `nft`.
- No system tables (UFW, Docker, nat) can be modified.
- Arguments are strictly whitelist-validated.
- The helper code is root-owned and cannot be modified by unprivileged users.

---

## 5. APT Gating Architectural Analysis

To resolve `SEC-002` safely:

1. **Rejection of "Run User Code as Root":** Changing the hook to `runuser -u jagdish python3 /home/jagdish/...` or exporting `HOME=/home/jagdish` while running as root still allows unprivileged desktop files to dictate root or APT workflow outcomes.
2. **Recommended Safe Architectural Models:**
   - **Model 1: Firewall-Level Sandbox Enforcement (Preferred & Zero-Code in APT):**
     In Balanced and Strict modes, the firewall helper permanently enforces `skuid 42 ct state new drop` on the metered interface. APT's background worker (`_apt`, UID 42) is physically blocked by the kernel. When the user explicitly wants to update packages, they grant allowance or bypass via the Data Guardian UI/CLI, which opens a temporary 10-minute window for UID 42. No Python hooks are needed in `/etc/apt/apt.conf.d/`.
   - **Model 2: Root-Owned Static Hook Script:**
     If interactive terminal prompting before APT downloads is strictly required, the hook binary must be installed to `/usr/local/bin/data-guardian-apt-gate` (owned by `root:root`, mode `0755`), containing **zero imports from user space**. It queries the daemon's runtime socket `/run/user/1000/data-guardian.sock` over an unprivileged UNIX client to request status and authorization.

---

## 6. Strict Mode: Technical Feasibility Assessment

True foreground-only enforcement requires five mandatory architectural components:

1. **Window Identification:** `xprop -root _NET_ACTIVE_WINDOW` resolving to `_NET_WM_PID` (Available and working in `x11_tracker.py`).
2. **Process Hierarchy Resolution:** Browsers (Chrome, Brave, Firefox) spawn multiple child processes across different PIDs. The tracker must recursively discover all child PIDs using `/proc/<pid>/task` or `pgrep -P`.
3. **cgroupv2 Controller Setup:** Systemd must delegate the `pids` and `memory` controllers to the user slice (`/sys/fs/cgroup/user.slice/user-1000.slice/`). A sub-cgroup `data_guardian/foreground` must be managed dynamically.
4. **Socket Association:** Linux nftables `socket cgroupv2 level 2` inspects the cgroup of the socket *at creation time*. Existing open sockets do not automatically transition when a PID is moved to a new cgroup.
5. **Whitelisting:** Development tools (e.g., OpenCode, Antigravity CLI, git, ssh) running without a GUI window must be explicitly registered in a background whitelist cgroup to prevent silent connection termination.

**Conclusion:** Strict mode is an advanced capability requiring isolated prototyping. It must remain disabled in the firewall layer until its cgroup management and child-process tracking are fully implemented and verified.

---

## 7. Monthly Quota Data Model Analysis

To prevent double-counting and ensure mathematical accuracy:

- **Authoritative Data Source:** `network_daily_usage` is the single source of truth for per-network usage. `daily_usage` remains the aggregate fallback.
- **Network Scope:** Monthly quota enforcement must evaluate *only* the bytes consumed while connected to the **designated metered hotspot**, not home Wi-Fi or ethernet.
- **Monthly Boundary Definition:**
  - Standard: Calendar Month (`date >= date('now', 'start of month')`).
  - Billing Cycle: Option in `config.yaml` (`billing_cycle_day: 1` to `31`). If set to `15`, usage aggregates from the 15th of the previous month to the 14th of the current month.
- **Query Formulation:**
  ```sql
  SELECT COALESCE(SUM(rx_bytes + tx_bytes), 0) AS monthly_total
  FROM network_daily_usage
  WHERE connection_uuid = ?
    AND date >= ?
    AND date <= ?;
  ```

---

## 8. Desktop Notification Delivery Architecture

The background service cannot assume fixed desktop session variables (`DISPLAY=:0`, `XAUTHORITY`, or fixed D-Bus sockets).

### Recommended Solution: IPC-Triggered Client Notifications
Instead of the background daemon spawning desktop processes directly:
1. The daemon detects events (hotspot active, 80% warning, quota exhausted).
2. The daemon broadcasts the event over the local UNIX socket (`/run/user/1000/data-guardian.sock`).
3. The System Tray Applet (`ui/tray.py`) or Dashboard (`ui/dashboard.py`), which are already running inside the user's active desktop session, receive the IPC message.
4. The tray applet invokes native desktop notifications via `gi.repository.Notify` or `notify-send`, ensuring 100% reliable display in XFCE without elevated privileges or session bus guessing.

---

## 9. Testing & Verification Summary

- **Current Automated Suite:** 21 unit tests passing (`tests/test_*.py`).
- **Test Limitations:** Existing tests validate string generation and SQLite queries in temporary directories. Zero tests currently validate real nftables rule parsing, socket transitions, or cgroup attachment.
- **Required New Test Coverage:**
  1. `test_privileged_helper_arg_validation`: Ensure helper rejects invalid actions, malicious characters, and unknown interfaces.
  2. `test_quota_exhaustion_recovery`: Verify that restarting the daemon with exhausted daily quota restores the blocked state immediately.
  3. `test_live_network_accounting_wiring`: Verify `record_usage_delta()` persists connection UUID into `network_daily_usage`.

---

## 10. Decisions Required from User

The following three architectural decisions must be resolved before proceeding with Phase 1 execution:

### DECISION-01: Privileged Helper vs. Direct Firewall Handling
- **Option A (Recommended):** Install a small root-owned privileged helper (`/usr/local/libexec/data-guardian-helper`) with targeted sudoers rules. Complete command validation, zero arbitrary `nft` access, safest architecture.
- **Option B:** Revert daemon to run as `root` in systemd. Eliminates sudoers configuration, but gives the entire Python monitoring daemon root privileges.

### DECISION-02: APT Protection Architecture
- **Option A (Recommended):** Firewall-level isolation. Drop UID 42 (`_apt`) at the kernel level on metered hotspot. User opens temporary 10-minute download windows via the UI/CLI. Removes all risky root Python hooks.
- **Option B:** Root-owned standalone C/Python hook script in `/usr/local/bin` that queries the daemon socket. Preserves terminal prompts during `sudo apt install`.

### DECISION-03: Strict Profile Staging
- **Option A (Recommended):** Keep Strict mode in "Experimental / Staged" status in the UI with a clear disclaimer; do not enforce in firewall until cgroupv2 supervisor is fully verified.
- **Option B:** Implement a simplified Strict mode: block all non-essential outbound ports (DNS/DHCP only) regardless of window focus when activated.

---
*End of Audit Report. No system configuration or source files have been modified.*
