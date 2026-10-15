# Linux Data Guardian — Phase 1 Implementation Roadmap

**Based on:** audit.md (commit `7caf94d`, 2026-10-15)  
**Goal:** Make agreed features work correctly, remove critical bugs, verify behavior.

> ⛔ **Phase 2 (Optimization) is BLOCKED until user explicitly approves Phase 1 completion.**

---

## Pre-Phase: User Decisions Required

Before any implementation step can begin, you must decide on 3 architecture questions.
These are listed in `audit.md` Section 7. Summary:

| ID | Decision | Blocking Steps |
|---|---|---|
| DECISION-01 | How to grant nft privileges (polkit vs sudoers vs root) | STEP-01 |
| DECISION-02 | Strict mode implementation (cgroups vs simplified vs defer) | STEP-04 |
| DECISION-03 | APT hook user context fix approach | STEP-03 |

**BLOCKED: USER DECISION REQUIRED on DECISION-01, 02, 03 before implementation.**

---

## Phase 1 Steps (Dependency Order)

---

### STEP-01 — Fix Firewall Privilege (BLOCKED: DECISION-01)
**Finding:** SEC-001  
**Objective:** Allow daemon to actually apply nft rules without running as root.

**Proposed approach (after decision):**
- **If polkit:** Create `/etc/polkit-1/rules.d/50-data-guardian-nft.rules` allowing `jagdish` to run `/usr/sbin/nft` with specific argument patterns.
- **If sudoers:** Add `jagdish ALL=(root) NOPASSWD: /usr/sbin/nft` to `/etc/sudoers.d/data-guardian`.
- Update `nft_manager.can_apply_rules()` to verify the chosen mechanism works.
- Add error checking: if `apply_profile()` returns `(False, msg)`, log the error visibly.

**Files:** `firewall/nft_manager.py`, `systemd/data-guardian.service`, new polkit/sudoers file  
**Manual verification:** After fix, run `data-guardian profile balanced` → verify `sudo nft list table inet data_guardian` shows the rules.  
**Rollback:** Remove polkit/sudoers file, rules disappear instantly.

---

### STEP-02 — Fix BUG-007: ALHN-449C Wrongly Detected as Hotspot
**Finding:** BUG-007 (Critical immediate action)  
**Objective:** Stop daemon from counting home Wi-Fi as metered hotspot.

**No decision needed.** This is a configuration or code bug.

**Action:**
1. Read `~/.config/linux-data-guardian/config.yaml` — verify `designated_uuid` is `590f9bbc-21bc-4b5c-9137-4b2912b21c94`.
2. If wrong UUID is stored, fix it with `data-guardian set-hotspot 'Moto Mobile'`.
3. If UUID is correct but still matches ALHN-449C, trace `nm_monitor.poll_check()` — the designated_name partial match may be the bug. Add exact-match-only for UUID when UUID is set.

**Files:** `network/nm_monitor.py` (if code bug), `~/.config/linux-data-guardian/config.yaml`  
**Test:** Start daemon, check `data-guardian status` — "Metered Status: NO" when on ALHN-449C.  
**Acceptance:** Journalctl shows no "Hotspot activated: ALHN-449C" entries.

---

### STEP-03 — Fix APT Hook User Context (BLOCKED: DECISION-03)
**Finding:** BUG-001  
**Objective:** APT approval gate actually reads jagdish's config when apt runs as root.

**Proposed approach (recommended: Option A):**
```
# Change /etc/apt/apt.conf.d/99data-guardian to:
APT::Acquire::Pre-Invoke {
    "HOME=/home/jagdish XDG_CONFIG_HOME=/home/jagdish/.config PYTHONPATH='...' /usr/bin/python3 ...";
};
```
This forces `Path.home()` → `/home/jagdish` even when running as root.

**Files:** `config/apt-hook-template.conf`, `/etc/apt/apt.conf.d/99data-guardian` (requires sudo to update)  
**Test:** On hotspot, run `sudo apt install --dry-run curl`. Zenity prompt must appear.  
**Acceptance:** Gate records an approval event in the DB viewable in dashboard.

---

### STEP-04 — Fix Per-Network Accounting in Daemon (No Decision Required)
**Finding:** BUG-003  
**Objective:** Live usage is tracked per-network in `network_daily_usage`.

**Change:** 3-line fix in `daemon.py`:
```python
# daemon.py:L149 — add connection_uuid and connection_name
usage = self.db.record_usage_delta(
    delta_rx=delta_rx,
    delta_tx=delta_tx,
    quota_limit_bytes=daily_limit_bytes,
    connection_uuid=active_conn.uuid,       # ADD THIS
    connection_name=active_conn.name,       # ADD THIS
)
```

**Files:** `src/data_guardian/daemon.py`  
**Test:** Connect to any Wi-Fi for 5 minutes, check Statistics tab — should show that network's usage growing.  
**Acceptance:** `network_daily_usage` table has a row with today's date and correct UUID.

---

### STEP-05 — Fix Per-Network Profile Auto-Apply (Depends on STEP-01)
**Finding:** BUG-004  
**Objective:** When daemon detects hotspot connection, apply that network's saved profile.

**Change in `daemon.py:_on_hotspot_activated()`:**
```python
def _on_hotspot_activated(self, conn: ConnectionInfo) -> None:
    # Load per-network saved profile instead of global profile
    saved_profile = self.config_mgr.get_network_profile(conn.uuid)
    self._current_profile = saved_profile
    ...
    if self._current_profile != "off":
        self.firewall.apply_profile(self._current_profile, ...)
```

**Files:** `src/data_guardian/daemon.py`  
**Test:** Save Moto Mobile → Strict, ALHN-449C → Off. Connect to each. Verify profile auto-applies.

---

### STEP-06 — Fix Daemon Notifications (No Decision Required)
**Finding:** BUG-006  
**Objective:** 80% warning, quota exceeded, and hotspot connect alerts actually reach the desktop.

**Change:** Add `DISPLAY=:0` and `DBUS_SESSION_BUS_ADDRESS` to service file.
```ini
Environment="DISPLAY=:0"
Environment="DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus"
```
Also pass these when calling `notify-send` via subprocess.

**Files:** `systemd/data-guardian.service` (user must run sudo to update)  
**Test:** Manually trigger `data-guardian allow 1` then wait for quota to be reached — notification should appear.  
**Acceptance:** Desktop notification pops up on hotspot connect.

---

### STEP-07 — Implement Monthly Quota Enforcement (No Decision Required)
**Finding:** Missing feature  
**Objective:** Respect `monthly_limit_mb` from config.

**Change:** In `daemon.py:_iteration()`, after daily quota check, add monthly sum query:
```python
monthly_used = self.db.get_monthly_usage()  # new DB method
if monthly_used >= self.config_mgr.monthly_limit_mb * 1024 * 1024:
    # block / notify
```
Add `get_monthly_usage()` to `database.py` (sum of `daily_usage.rx_bytes + tx_bytes` for current month).

**Files:** `daemon.py`, `database.py`

---

### STEP-08 — Fix flush() Return Value Correctness (Low Priority)
**Finding:** BUG-005  
**Objective:** `flush()` should not claim success when nft command fails.

**Change (2 lines in `nft_manager.py`):**
```python
if res.returncode != 0 and "No such file" not in res.stderr:
    return False, f"nft delete failed: {res.stderr.strip()}"
```

**Files:** `firewall/nft_manager.py`

---

### STEP-09 — Fix Schema Version Test (Low Priority)
**Finding:** BUG-002  
**Objective:** Test correctly verifies v2 schema migration.

**Change in `tests/test_database.py`:**
```python
cursor.execute("SELECT MAX(version) FROM schema_version;")
self.assertEqual(cursor.fetchone()[0], 2)  # was 1
```
Also add: `test_network_daily_usage_tracking()`, `test_per_network_history()`.

**Files:** `tests/test_database.py`

---

### STEP-10 — Strict Mode Implementation (BLOCKED: DECISION-02)
**Finding:** SEC-002  
**Objective:** Strict mode actually restricts to foreground app.

**(If Option A chosen — full cgroups):**
- Create `/sys/fs/cgroup/data_guardian/foreground` on daemon start (needs root or polkit)
- On each X11 window change, move foreground PID + children into `foreground` cgroup
- Move whitelisted processes into `whitelist` cgroup at startup
- The existing nft rules then work correctly

**(If Option B chosen — simplified block-all-new):**
- Remove cgroup references from `rulesets.py` strict profile
- Replace with `ct state new drop` after DNS/DHCP (same as quota exceeded but clearer label)
- Update UI description to match actual behavior

**Files:** `firewall/rulesets.py`, `window_tracker/x11_tracker.py`, `daemon.py`

---

### STEP-11 — Add Missing Integration Tests
**Objective:** Tests that prove actual behavior, not just string matching.

Tests to add (safe, no real firewall):
- `test_daemon_passes_uuid_to_db()` — mock DB, verify `record_usage_delta` called with UUID
- `test_per_network_profile_applied_on_hotspot_connect()` — mock firewall, verify profile loaded from config
- `test_monthly_quota_calculation()` — DB method with known data
- `test_apt_hook_reads_correct_user_config()` — mock HOME env, verify correct config path

**Files:** `tests/test_daemon.py` (new), `tests/test_integration.py` (new)

---

## Phase 2 — Optimization (BLOCKED until Phase 1 approved)

Candidate work for Phase 2 (do not start):
- Replace polling loop with event-driven NetworkManager D-Bus signals
- Database retention policy (prune records older than 90 days)
- Dashboard startup latency (lazy-load matplotlib)
- Reduce `_notify()` subprocess overhead (use `gi.repository.Notify` directly)
- IPC receive buffer size (currently 8192 bytes — could truncate large status JSON)
- Package the project as a proper pip-installable package with `setup.py`

---

## Summary Status

| Step | Priority | Blocking | Status |
|---|---|---|---|
| STEP-02 | 🔴 Immediate | Nothing | Ready to fix now |
| STEP-04 | 🔴 High | Nothing | Ready to fix now |
| STEP-06 | 🟠 High | Nothing | Ready to fix now |
| STEP-01 | 🔴 Critical | DECISION-01 | BLOCKED |
| STEP-03 | 🔴 High | DECISION-03 | BLOCKED |
| STEP-05 | 🟠 High | STEP-01 | BLOCKED |
| STEP-07 | 🟡 Medium | Nothing | Ready |
| STEP-08 | 🟢 Low | Nothing | Ready |
| STEP-09 | 🟢 Low | Nothing | Ready |
| STEP-10 | 🔴 Critical | DECISION-02 | BLOCKED |
| STEP-11 | 🟡 Medium | STEP-01 | After STEP-01 |

> ⛔ **No implementation has been started. This is a planning document only.**
