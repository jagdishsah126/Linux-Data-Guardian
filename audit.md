# Linux Data Guardian — Independent Audit Report

**Branch:** `main`  
**Commit audited:** `7caf94d`  
**Audit date:** 2026-10-15  
**Auditor:** Independent code review (all findings verified against actual source)

---

## 1. Executive Summary

Linux Data Guardian is a **working prototype** — not yet a production-safe tool.
Several core mechanisms either silently fail or are completely unimplemented despite appearing
functional in the UI. The monitoring and accounting layer works correctly. The firewall layer
**never actually applies rules** under normal operation. The download gate works for APT but
has a critical privilege issue. Statistics UI is backed by real data.

### What is actually working ✅
- Daemon starts, runs, and survives reboots (systemd service healthy)
- Network change detection (designated hotspot UUID matching, transition callbacks)
- `/proc/net/dev` usage accounting (RX/TX byte tracking per interface, delta math correct)
- Per-network daily usage DB (v2 migration ran successfully)
- SQLite database: sessions, daily_usage, network_daily_usage, approval_events — all correct
- IPC socket (tray ↔ daemon ↔ dashboard) — working
- Dashboard tabs: Overview, Statistics (matplotlib chart with real data), CLI tab
- Per-network profile saved in config.yaml (new feature, works correctly)
- Approval gate logic (should_gate, whitelist, off-mode bypass) — logic is correct
- APT hook installed at `/etc/apt/apt.conf.d/99data-guardian`
- Tray icon using XApp.StatusIcon (Linux Mint native) — confirmed showing
- Emergency disable script — safe, scoped to data_guardian table only
- 21/21 unit tests pass

### What must NOT be trusted in production ⚠️
- **Firewall rules are NEVER applied** — daemon runs as user `jagdish`, has no sudo, so every `apply_profile()` call silently fails. Balanced and Strict modes do nothing to actual traffic.
- **APT hook runs as root** — reads config from `/root/.config/` (not jagdish's), so `is_hotspot_active()` will always return `False`. APT approval gate passes through without asking.
- **Strict mode cgroup mechanism doesn't exist** — `socket cgroupv2` rules reference `data_guardian/foreground` cgroup that is never created.
- **Monthly quota is not implemented** — tracked in config but never checked or enforced.
- **`record_usage_delta()` does not pass connection UUID** — per-network accounting only populated from sessions table migration, not live tracking.
- **Notifications do not work from systemd** — daemon has no `DISPLAY` env var set.

---

## 2. Feature Implementation Matrix

| Feature | Intended Behavior | Actual Implementation | Status | Evidence |
|---|---|---|---|---|
| Daemon auto-start at boot | systemd service starts daemon | ✅ Works correctly | **Verified** | `systemctl status` active |
| Hotspot detection | Match designated UUID | ✅ `poll_check()` correct | **Verified** | daemon.py:L67, nm_monitor.py |
| Usage accounting (RX/TX) | Track bytes per session | ✅ `/proc/net/dev` delta | **Verified** | byte_counter.py, daemon.py:L134 |
| Per-network daily stats | Separate stats per network | ⚠️ Partially working | **Partial** | DB has it but daemon doesn't pass UUID (BUG-003) |
| Daily quota limit | Block at N MB/day | ⚠️ Logic exists, enforcement fails | **Partial** | quota_exceeded flag set, but nft never applied |
| Monthly quota limit | Block at N MB/month | ❌ Never checked in daemon | **Missing** | No monthly check in daemon.py |
| 80% warning notification | notify-send alert | ⚠️ Code exists, DISPLAY missing | **Partial** | daemon.py:L162, no DISPLAY in systemd (BUG-006) |
| Balanced profile firewall | Block UID 42 (apt), allow browsing | ❌ Rules never applied | **Not Enforced** | nft_manager.py: `can_apply_rules()` returns False (SEC-001) |
| Strict profile firewall | Foreground-only via cgroups | ❌ Rules never applied + cgroup not created | **Not Enforced** | SEC-001, SEC-002 |
| Off profile | Monitoring only, no rules | ✅ Works (no rules = correct) | **Verified** | rulesets.py off returns delete |
| APT download gating | Prompt before apt install | ⚠️ Hook installed but runs as root | **Broken** | BUG-001 |
| pip/npm/cargo shims | Intercept package installs | ✅ Shim logic correct | **Verified** (logic only) | shims.py, gate.py |
| Zenity approval dialog | GUI prompt on metered | ✅ Logic correct | **Unverified on hotspot** | gate.py:L93 |
| Emergency disable | Remove all nft rules | ✅ Scoped to data_guardian table | **Verified** | emergency-disable.sh |
| Per-network profile | Each network has own mode | ✅ Saved in config.yaml | **Verified** | config.py:get/set_network_profile |
| Statistics tab / chart | Line chart per network | ✅ Real DB data | **Verified** | dashboard.py, database.py |
| CLI tab | Run commands in-app | ✅ Working with inline output | **Verified** | dashboard.py |
| Tray icon | XApp.StatusIcon in XFCE panel | ✅ Confirmed showing | **Verified** | tray.py |
| IPC daemon ↔ UI | UNIX socket | ✅ Working | **Verified** | ipc.py |
| Whitelist (git, brave, etc.) | Never blocked | ⚠️ Whitelist config exists, approval gate uses it | **Partial** | gate.py:L47, but nft rules don't whitelist by name |
| Uninstall script | Clean removal | ⚠️ Not audited | **Unverified** | scripts/uninstall.sh |

---

## 3. Critical and High-Priority Findings

---

### SEC-001 — Firewall Rules Never Applied
**Severity:** Critical | **Confidence:** Confirmed

**Evidence:**
```python
# nft_manager.py — can_apply_rules()
res = subprocess.run(["sudo", "-n", "true"], ...)
return res.returncode == 0
# Daemon runs as User=jagdish, no passwordless sudo configured
# sudo -n true → REQUIRES PASSWORD → returns False
# Therefore apply_profile() returns (False, "Root privileges required...")
# The return value is NOT checked in daemon.py
```
```python
# daemon.py:L99 — return value silently discarded
self.firewall.apply_profile(self._current_profile, quota_exceeded=self._is_quota_blocked)
# No check of the returned (bool, str) tuple
```

**Confirmed:** `sudo -n nft list tables` requires password — verified live.

**Failure scenario:** User connects to Moto Mobile hotspot, daemon switches to Balanced or Strict mode, UI reports active enforcement, but `/proc/net/dev` shows full unrestricted traffic. No rules exist in nftables. The daemon logs no error.

**User impact:** All data-saving firewall features (Balanced, Strict, quota block) are completely ineffective. The system appears to work but does nothing.

**Recommended fix:** Add a polkit rule allowing user `jagdish` to run `nft` for the `data_guardian` table only — or add passwordless sudo for `nft` scoped to this table. The daemon then gains real enforcement without running as root.

**Regression risk:** Low — adding polkit or targeted sudoers entry affects only this table.

---

### SEC-002 — Strict Mode cgroup Never Created
**Severity:** Critical | **Confidence:** Confirmed

**Evidence:**
```nft
# rulesets.py — strict profile rule
socket cgroupv2 level 2 "data_guardian/foreground" accept
```
```bash
# Checked live:
ls /sys/fs/cgroup/data_guardian  # → "No such file or directory"
```

**Failure scenario:** Even if SEC-001 is fixed (nft rules get applied), Strict mode rules reference `/sys/fs/cgroup/data_guardian/foreground` which does not exist. All new outbound connections would be dropped — including the foreground app. The daemon has no code to create cgroups or assign processes to them.

**User impact:** Strict mode, once firewall enforcement is fixed, would block ALL new connections (same as quota exceeded), including the foreground browser. This is dangerous.

**Recommended fix (requires user decision):** Option A — Implement proper cgroup v2 management: create `data_guardian/{foreground,whitelist}` cgroups, move foreground app PID into foreground cgroup on X11 window change. This is complex. Option B — Replace Strict mode cgroup rules with a simpler UID-based or process-name approach. Option C — Remove Strict mode from firewall and keep it UI-only for now with a clear disclaimer.

---

### BUG-001 — APT Hook Runs as Root, Reads Wrong Config
**Severity:** High | **Confidence:** Confirmed

**Evidence:**
```
# /etc/apt/apt.conf.d/99data-guardian
APT::Acquire::Pre-Invoke {
    "PYTHONPATH='...' /usr/bin/python3 -m src.data_guardian.approvals.gate --tool apt install || exit 1";
};
```
APT pre-invoke hooks run as **root** (or `_apt` user). `gate.py` calls `ConfigManager()` which reads `~/.config/linux-data-guardian/config.yaml`. When process is root, `Path.home()` = `/root`, so it reads `/root/.config/linux-data-guardian/config.yaml` — which does not exist.

```python
# gate.py:L61
is_hotspot = self.monitor.is_designated_hotspot_active()
# designated_uuid = "" (not found in /root/.config/)
# is_designated_hotspot_active() → False
# → gate returns False → "not on hotspot" → passes without prompting
```

**Failure scenario:** User is connected to Moto Mobile hotspot and runs `sudo apt install curl`. APT hook fires, runs as root, finds no config, sees no hotspot → approves silently. No prompt appears.

**User impact:** APT download gating never triggers. Core feature broken.

**Recommended fix:** Pass explicit config path to gate.py via env var `DATA_GUARDIAN_USER=jagdish` and hardcode the real user's home path, OR change APT hook to run as user `jagdish` using `su` or `runuser`.

---

### BUG-002 — Schema Version Test Assert Wrong Version
**Severity:** Low | **Confidence:** Confirmed

**Evidence:**
```python
# tests/test_database.py:L22
cursor.execute("SELECT version FROM schema_version;")
self.assertEqual(cursor.fetchone()[0], 1)
# But CURRENT_SCHEMA_VERSION = 2, migration inserts version=2
```
The test passes only because `SELECT version FROM schema_version` fetches the first row (version=1 from `_init_db`), and the test passes accidentally. After migration, two rows exist: 1 and 2. This masks that the test is not verifying v2 migration.

**Recommended fix:** Assert `MAX(version) = 2` and add a dedicated migration test.

---

### BUG-003 — Per-Network Accounting Not Updated Live
**Severity:** Medium | **Confidence:** Confirmed

**Evidence:**
```python
# daemon.py:L149-153 — record_usage_delta called WITHOUT connection_uuid/name
usage = self.db.record_usage_delta(
    delta_rx=delta_rx,
    delta_tx=delta_tx,
    quota_limit_bytes=daily_limit_bytes
    # connection_uuid and connection_name NOT passed
)
```
The new `network_daily_usage` table added in v2 is only populated from session-based migration data. Live per-network tracking doesn't happen while the daemon runs.

**Failure scenario:** Statistics tab shows ALHN-449C data only from the migration snapshot. New hotspot (Moto Mobile) sessions won't appear in the statistics chart until a future migration or manual DB insert.

**Recommended fix:** Pass `connection_uuid=active_conn.uuid, connection_name=active_conn.name` in the `record_usage_delta()` call in daemon.py.

---

### BUG-004 — Daemon Loses Hotspot UUID on Profile Change via Dashboard
**Severity:** Medium | **Confidence:** Confirmed

**Evidence:**
```python
# daemon.py IPC handler: set_profile
self._current_profile = new_profile
self.config_mgr.active_profile = new_profile  # saves GLOBAL profile, not per-network
```
When user changes profile in dashboard (per-network UI), the IPC sends `set_profile` which updates the global profile. The daemon uses this global `_current_profile` for all networks. Per-network saved profiles in config.yaml are ignored by the daemon — it only reads `active_profile` on startup.

**User impact:** Changing profile for ALHN-449C also affects how Moto Mobile will behave when you connect to it. The per-network UI gives a false impression of isolation.

**Recommended fix:** Daemon should apply the saved per-network profile from config when switching networks, not keep a global `_current_profile`.

---

### BUG-005 — Flush() Does Not Check If Table Exists
**Severity:** Low | **Confidence:** Confirmed

**Evidence:**
```python
# nft_manager.py:flush()
cmd = use_sudo + ["nft", "delete", "table", "inet", TABLE_NAME]
res = subprocess.run(cmd, ...)
self.active_profile = "off"  # set to off even if command failed
return True, "Successfully flushed..."  # always returns success message
# Return code of res is NOT checked
```
If the table doesn't exist (e.g., after a reboot), `nft delete table` returns error code 1, but `flush()` returns `(True, "Successfully flushed")`. This is misleading but not dangerous — the end state (no table = no rules) is correct.

---

### BUG-006 — Notifications Never Reach Desktop from systemd Daemon
**Severity:** Medium | **Confidence:** Confirmed

**Evidence:**
```python
# daemon.py:L78
if os.environ.get("DISPLAY") and ...
    subprocess.run(["notify-send", ...])
```
```ini
# systemd/data-guardian.service
Environment="XDG_RUNTIME_DIR=/run/user/1000"
# DISPLAY is NOT set in the service file
```
The daemon running under systemd has no `DISPLAY` environment variable. `os.environ.get("DISPLAY")` returns `None` → all notifications silently dropped. The 80% warning, quota exceeded alert, and hotspot connect/disconnect notifications never appear.

**Recommended fix:** Add `Environment="DISPLAY=:0"` to the service file (valid for X11 session which you confirmed). Or use `DBUS_SESSION_BUS_ADDRESS` and `libnotify` via dbus.

---

### BUG-007 — ALHN-449C Mistakenly Treated as Hotspot
**Severity:** High | **Confidence:** Confirmed (from daemon logs)

**Evidence (from journalctl):**
```
[+] Hotspot activated: ALHN-449C (a054f084...) on wlp0s20f3
[-] Hotspot deactivated.
[+] Hotspot activated: ALHN-449C ...
[-] Hotspot deactivated.
[+] Hotspot activated: ALHN-449C ...
```
ALHN-449C is your **home Wi-Fi router**, not the mobile hotspot. The daemon is repeatedly flagging and unflagging it as the designated hotspot. This means:
1. Usage is being counted against the "metered" quota while you're on unmetered home Wi-Fi
2. The firewall (if it ever worked) would try to apply restrictions on your home network

**Root cause:** The designated hotspot UUID is `590f9bbc...` (Moto Mobile), but ALHN-449C UUID `a054f084...` appears in config or the match is by name hitting a partial match.

**Immediate action needed:** Run `data-guardian status` to confirm the designated UUID. Check `~/.config/linux-data-guardian/config.yaml` to verify the stored UUID is `590f9bbc-21bc-4b5c-9137-4b2912b21c94` and not accidentally set to ALHN-449C.

---

## 4. Missing Features and Incomplete Integrations

| Feature | Status | Detail |
|---|---|---|
| Monthly quota enforcement | **Completely missing** | `monthly_limit_mb` in config, never read by daemon |
| Strict mode cgroup management | **Missing** | No cgroup creation, no PID assignment |
| Per-network profile auto-apply | **Missing** | Daemon ignores per-network profile on network switch |
| nft privilege mechanism | **Missing** | No polkit rule, no sudoers entry |
| Firewall: whitelist by process name | **Missing** | config/whitelist.yaml never applied to nft rules |
| Inbound rule blocking | **Not present** | All rules are OUTPUT chain only — inbound is not filtered (acceptable for this use case) |
| 95% quota warning threshold | **Missing** | Only 80% is implemented |
| Reconnect after sleep/wake | **Not tested** | poll_check() is polling-based; should survive but not verified |
| Uninstall script completeness | **Not verified** | uninstall.sh not audited |

---

## 5. Unplanned Additions

| Addition | Assessment |
|---|---|
| Per-network profile (config.yaml) | ✅ **Keep** — valuable, partially working |
| Statistics tab with matplotlib | ✅ **Keep** — real data, good UX |
| CLI tab with inline output | ✅ **Keep** — genuinely useful |
| `network_daily_usage` DB table | ✅ **Keep** — correct architecture |
| Schema migration system | ✅ **Keep** — but fix test (BUG-002) |
| X11 window tracker | ⚠️ **Keep but clarify** — tracking works but isn't connected to firewall enforcement |
| IPC 8192-byte recv limit | ⚠️ **Watch** — if status response grows large, truncation could corrupt JSON |

---

## 6. Testing Results

**Command:** `python3 -m pytest tests/ -v --tb=short`  
**Environment:** `/home/jagdish/Desktop/System Files/Linux Data Guardian/`, Python 3.12.3  
**Result: 21 passed, 0 failed, 0 skipped** in 0.44s

### What these tests actually prove:
| Test | What it proves | What it does NOT prove |
|---|---|---|
| `test_firewall_rules.py` | Ruleset *strings* are generated correctly | Rules can be applied to real nftables |
| `test_database.py` | DB schema, sessions, quota math | Schema version=1 assertion is wrong (BUG-002) |
| `test_approvals.py` | Gate logic, shim creation, download detection | APT hook works when run as root |
| `test_byte_counter.py` | `/proc/net/dev` parsing, delta math | Accounting works end-to-end in daemon |
| `test_nm_monitor.py` | UUID matching logic, poll transitions | Real NetworkManager API works |

### Tests that must be added (not run):
- Firewall application with actual nft (requires root/polkit)
- APT hook behavior as root user
- Daemon end-to-end: connect hotspot → rules applied → quota exceeded → block
- Monthly quota enforcement
- Sleep/wake reconnection behavior
- Per-network profile auto-apply on network switch

---

## 7. Decisions Required from You

### DECISION-01 — Firewall Privilege Mechanism
**Problem:** Daemon runs as user `jagdish` and cannot apply nft rules without root.  
**Option A (Recommended):** Create a polkit rule that allows `jagdish` to run `/usr/sbin/nft` without password, scoped to specific arguments. Clean, modern Linux approach.  
**Option B:** Add a targeted sudoers entry: `jagdish ALL=(root) NOPASSWD: /usr/sbin/nft`. Simpler but slightly broader.  
**Option C:** Run daemon as root (reverts the previous fix — brings back the `/root/.config` problem unless XDG env is set).  
**Consequence:** Without this decision, all firewall features remain permanently non-functional.  

### DECISION-02 — Strict Mode Implementation Approach
**Problem:** Real foreground-only enforcement via cgroups v2 is complex (20+ lines of cgroup management code, must track PIDs).  
**Option A (Recommended):** Implement cgroup v2 correctly — create `data_guardian/{foreground,whitelist}`, assign foreground PID on X11 window change. Full enforcement but complex.  
**Option B:** Replace with simpler user-space approach: block all new connections in Strict except DNS, accept established — same as quota exceeded but with whitelist. Less precise but actually enforceable.  
**Option C:** Mark Strict mode as "Coming Soon" in UI, disable the radio button, explain it's planned.

### DECISION-03 — APT Hook User Context
**Problem:** Hook runs as root, reads wrong config.  
**Option A (Recommended):** Change hook to `su jagdish -c "..."` with `HOME=/home/jagdish` set explicitly.  
**Option B:** Hardcode the config path in the hook (bypass `Path.home()`).  
**Option C:** Remove APT hook; rely only on pip/npm/cargo shims.

---

## 8. Limitations of This Audit

- **Firewall:** Verified by static code inspection + live `sudo -n nft` test. Actual rule application was NOT tested (would require root).
- **APT hook:** Verified by tracing code + reading `/root/.config` would be empty. Not tested with live `apt install` as root.
- **Hotspot BUG-007:** Confirmed from journalctl logs. Root cause not fully traced yet.
- **Sleep/wake behavior:** Not tested.
- **Uninstall script:** Not audited.
- All 21 unit tests ran and passed. No integration tests ran.

> **No implementation or optimization has been started. This document is audit-only.**
