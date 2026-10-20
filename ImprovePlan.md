# Linux Data Guardian — Verified Audit & Bug-Free Improvement Plan

**Status:** implementation complete; 124 tests passing. The bug registry in §2
is the *pre-fix* record and is kept as the audit trail. Remaining privileged
install and deploy steps are listed in §9. See `report.md` for verified results.
**Scope:** every tracked file in the build workspace, every claim in `README.md`, `audit.md`, `plan.md`, `step.md`, `report.md`, and the live installed system.
**Method:** static reading of all 4,672 LOC + live runtime probing (daemon, IPC, DB, helper, nft, UI, systemd, installer) — not test-suite-only.

---

## 0. Executive verdict

The project is **not** "Production Ready & Verified Active" as `report.md` claims. The core idea works and several hard problems were genuinely solved (unprivileged daemon, scoped nft table, NOPASSWD helper with argument validation, per-network profile persistence, 26 passing tests). But there are **5 release-blocking bugs**, **3 advertised features that do not exist**, and a **dangerous installer regression** that re-creates the root-level privilege-escalation path that was deliberately removed.

| Area | Verdict |
|---|---|
| Traffic accounting (`/proc/net/dev`) | ✅ works, byte-accurate |
| Hotspot detection (libnm) | ✅ works, but `nmcli -t` parsing breaks on SSIDs containing `:` |
| nftables enforcement | ⚠️ works, but rules **accumulate** on every apply; interface fallback can silently disable protection |
| Quota engine | ❌ false-positive block on empty DB; temporary allowance is a no-op before first traffic; monthly-only quota never enforced; unmetered traffic counts toward quota |
| Strict mode | ❌ does not restrict ports, does not whitelist tools, does not track the foreground window; it is byte-for-byte balanced |
| Whitelist feature | ❌ loaded, never enforced |
| APT window | ❌ never expires; may be unreachable due to duplicate-rule ordering |
| `data-guardian doctor` | ❌ exists in code, not wired to CLI |
| Installer | ❌ does not install helper/sudoers/icon; **re-installs the removed root Python APT hook** |
| Uninstaller | ❌ fails to remove shims (missing `PYTHONPATH`); leaves config/DB |
| UI | ⚠️ approvals log frame is built but never added to the window; tray click callback signature is wrong |
| Docs | ❌ at least 14 material claims are false (matrix in §4) |

**Definition of done for this plan:** every P0/P1 item closed, every "false" row in §4 either implemented or reworded, `nft -c` validation green for all profiles, 40+ tests including the new regressions, and a 24-hour soak on the real hotspot.

---

## 1. Evidence: what was actually run

```
pytest -q                                   -> 26 passed in 0.91s
systemctl status data-guardian              -> active (running) since 13:34
python3 -m src.data_guardian.main status    -> IPC get_status OK
sudo -n /usr/local/libexec/data-guardian-helper --status        -> ok
sudo -n /usr/local/libexec/data-guardian-helper --apply-strict  -> ok
nft -c on rulesets.build_nft_ruleset("strict")  -> FAILS (cgroupv2 path missing)
nft -c on rulesets.build_nft_ruleset("balanced")-> ok
nft list table inet data_guardian (after 2 applies) -> duplicate rules present
```

Runtime facts captured:

* daemon RSS 43.5 MiB (earlier 28.9 MiB) — **exceeds the "<35 MB" claim**.
* tray RSS 85.8 MiB, dashboard RSS 110.5 MiB (measured with GTK running) — the "<35 MB" claim is false for the product as a whole.
* DB file mode `0644` although `report.md` claims `0600`.
* Sessions 15 and 16 both have `ended_at IS NULL` — orphan session from a daemon restart.
* `~/.local/bin/pip3` absent although `report.md` claims 4 shims installed.
* `/etc/apt/apt.conf.d/99data-guardian` absent (good) but `scripts/install.sh` would recreate it.
* deploy workspace: `git ls-files -v` shows `S` (skip-worktree) on `audit.md`, `plan.md`, `step.md`, and all `tests/*`, which are physically missing → the deploy repo is in a corrupted state that will break `git checkout`/CI.

---

## 2. Bug registry (P0 → P3)

Each entry: **symptom → evidence → root cause → fix algorithm**.

### P0 — release blockers

#### BUG-01 · Empty-DB quota false positive blocks all traffic on first run / new day

**Evidence:** on a DB with no row for today, `get_today_usage()` returns `effective_limit_bytes = 0`; `daemon.__init__` computes `total_bytes (0) >= effective_limit_bytes (0)` → `True` → prints `[!] Quota limit already exceeded on startup.` and sets `_is_quota_blocked = True`. Reproduced with a mocked daemon. This is what happens every midnight and on every fresh install.

**Root cause:** the startup check compares against a sentinel (`0`) instead of the configured limit, and does not distinguish "no data" from "limit reached".

**Fix:**

```
function startup_quota_state(config, db):
    daily_limit   = config.daily_limit_mb * MB
    monthly_limit = config.monthly_limit_mb * MB
    today         = db.quota_snapshot(date=today)      # metered-only, see §3.1
    month         = db.monthly_metered_total(cycle)

    daily_hit   = daily_limit   > 0 and today.metered_bytes >= today.effective_limit
    monthly_hit = monthly_limit > 0 and month.total_bytes   >= monthly_limit

    return {
        blocked: daily_hit or monthly_hit,
        reason:  monthly_hit ? "monthly" : (daily_hit ? "daily" : null),
    }
```

Rules: `effective_limit` must be `quota_limit + allowance` **only when a quota row exists**; `0` means "unlimited", never "exhausted".

**Regression test:** `test_startup_with_empty_db_is_not_blocked`.

---

#### BUG-02 · `grant_temporary_allowance()` is a no-op before the first byte of the day

**Evidence:** `UPDATE daily_usage ... WHERE date = ?` matches 0 rows on an empty day; verified `temporary_allowance_bytes` stays `0` after granting. The dashboard "+100 MB" button therefore silently does nothing if pressed before any traffic is recorded (e.g. right after midnight, or after a restart on a fresh day).

**Root cause:** update-only SQL against a row that is created lazily by `record_usage_delta`.

**Fix:** upsert the quota-state row (see §3.1). Do **not** rely on `daily_usage` for quota state.

```
function grant_allowance(db, mb):
    assert 0 < mb <= 10_000
    db.upsert_quota_state(date=today, allowance_delta=mb*MB, quota_exceeded=0)
    return db.quota_snapshot(today)
```

**Regression test:** `test_grant_allowance_on_empty_day_persists`.

---

#### BUG-03 · Installer re-introduces the root Python APT hook (privilege escalation)

**Evidence:** `scripts/install.sh` lines 41–49 still write `/etc/apt/apt.conf.d/99data-guardian` with `PYTHONPATH='${PROJECT_ROOT}' /usr/bin/python3 -m src.data_guardian.approvals.gate ...`. APT runs hooks as **root** and imports user-writable code. This is exactly SEC-002 from `audit.md`, which `report.md` claims was fixed.

**Root cause:** SEC-002 was mitigated by deleting the installed file, not by deleting the installer code.

**Fix:** remove the APT hook block entirely. Replace with a **root-owned, non-importing** check if APT gating is still desired:

```
Option A (recommended): firewall-only APT gating
  - keep `skuid 42 ct state new drop` in balanced
  - timed window implemented in the daemon (see BUG-05)
Option B (later, optional): compiled/shell hook installed 0755 root:root
  - may only call /usr/local/libexec/data-guardian-helper
  - never sets PYTHONPATH, never imports user code
```

**Acceptance:** `grep -r "PYTHONPATH" scripts/` returns nothing; `install.sh --system` never writes to `/etc/apt/apt.conf.d`.

---

#### BUG-04 · nftables rules accumulate on every apply (duplicate + stale rules)

**Evidence:** the helper emits `table inet data_guardian { ... }` without deleting the existing table. nft appends a second chain body to the existing chain. Reproduced in `unshare -Urn`: after two applies, `oif "lo" accept` appears twice, and rules for the previous interface remain.

Consequences:
1. rule bloat and duplicate counters over hours of profile switching;
2. stale `oif <old-iface>` rules keep dropping traffic on interfaces you left;
3. the APT window accept rule can be appended **after** the `skuid 42 drop` rule and is therefore unreachable (see BUG-05);
4. "flush" is the only way out, so a failed apply can leave a half-merged chain.

**Root cause:** non-idempotent table creation.

**Fix (atomic, idempotent):**

```
RULESET="
destroy table inet data_guardian      # rc=0 even if absent (verified)
table inet data_guardian {
  chain output { type filter hook output priority 0; policy accept; ... }
}"
echo "$RULESET" | nft -c -f -   || exit 1
echo "$RULESET" | nft -f -      || exit 1
```

`destroy table` + `table` in one `nft -f` transaction is atomic. Add a helper self-test that applies twice and asserts rule count is unchanged.

**Regression test:** `test_helper_apply_is_idempotent` (counts lines containing `oif "lo"` after 2 applies → must be 1).

---

#### BUG-05 · APT window never closes and may never open

**Evidence:** `--allow-apt-window` appends `skuid 42 accept` with no expiry and no timer anywhere in the daemon. Dashboard tooltip promises "temporary 15-minute window". Because of BUG-04, the accept is appended after the balanced `drop`, so new APT connections are still dropped.

**Fix:** two parts.

1. **Rule ordering** (in the generated ruleset, before the generic drop):

```
oif "$IFACE" skuid 42 ct state new accept    # only while window active
...
oif "$IFACE" skuid 42 ct state new drop
```

2. **Expiry in the daemon** (single source of truth):

```
self._apt_window_until: float | None = None

on allow_apt_window(mb=15):
    if not is_hotspot: return error("not on hotspot")
    self._apt_window_until = monotonic() + 15*60
    reapply_profile(with_apt_window=True)
    notify("APT window open for 15 minutes")

each iteration:
    if self._apt_window_until and monotonic() >= self._apt_window_until:
        self._apt_window_until = None
        reapply_profile(with_apt_window=False)
        notify("APT window closed")
```

3. Status exposes `apt_window_remaining_seconds`; dashboard shows a countdown and a "Close now" button.

**Regression test:** `test_apt_window_expires_and_reapplies`.

---

### P1 — correctness / security / data integrity

#### BUG-06 · Unmetered traffic is counted against the metered quota

**Evidence:** `daily_usage` aggregates **all** interfaces, and `record_usage_delta` compares `total_bytes` against the hotspot limit. Time spent on home Wi-Fi therefore consumes the hotspot budget. Conversely `quota_limit_bytes` is sticky (only overwritten when `> 0`), so an unmetered session can even flip `quota_exceeded = 1`.

**Fix:** quota is computed from **metered** rows only (schema v3 in §3.1). `daily_usage` remains a stats table for the chart.

#### BUG-07 · Monthly quota is never enforced when the daily limit is unlimited

**Evidence:** the monthly check lives inside `if is_hotspot and usage and usage.get("quota_limit_bytes", 0) > 0`. With `daily_limit_mb = 0`, the monthly limit is silently ignored.

**Fix:** evaluate daily and monthly independently in a single `QuotaEngine.evaluate()` (§3.2).

#### BUG-08 · Quota block state is not applied when `action_on_exhaustion == "warn_only"`, yet status reports blocked

**Evidence:** `_is_quota_blocked = True` is set unconditionally; firewall is applied only for `block_unapproved`. UI therefore shows "blocked" while traffic flows.

**Fix:** model `QuotaVerdict { level: ok|warning|exceeded, enforced: bool, reason }`; set `is_quota_blocked` only when `enforced`.

#### BUG-09 · Interface fallback silently disables protection

**Evidence:** `NftablesManager.apply_profile` falls back to the hardcoded `"wlp0s20f3"` when `interface` is empty. On any machine with a different interface name, nft rejects the rule (`interface does not exist`), the code flushes, and the user believes they are protected.

**Fix:** refuse to apply with an empty interface (`return False, "no interface"`), surface `firewall_error` in status, and show a red banner in tray/dashboard. Remove the hardcoded fallback.

#### BUG-10 · `flush()` reports success on failure

**Evidence:** `flush()` returns `True` unless an exception is raised; `nft` non-zero exit codes are ignored. A failed rollback is reported as success.

**Fix:** check `returncode`, log stderr, return `(ok, msg)`, and never claim "safe rollback" without verification.

#### BUG-11 · `pip3` shim gates read-only commands

**Evidence:** `ApprovalGate.is_download_action("pip3", ["list"])` → `True`, because `pip3` is missing from `DOWNLOAD_ACTIONS` and unknown tools default to gating. `pip3 list`, `pip3 --version`, `pip3 show` all prompt/deny.

**Fix:**

```
DOWNLOAD_ACTIONS = {
  "apt": [...], "apt-get": [...], "pip": [...], "pip3": [...],
  "npm": [...], "npx": ["install","i","add","create","dlx"],
  "cargo": [...], "yarn": [...], "pnpm": [...],
}
READ_ONLY = {"list","show","freeze","check","--version","-V","info","search","view","config"}
```

Also: skip flags *with values* correctly (`pip3 -r req.txt install` vs `pip3 install -r req.txt`), and treat `python -m pip install` by inspecting `argv` for `-m pip`.

**Regression test:** parameterized table of (tool, args, expected).

#### BUG-12 · No single-instance guard → split-brain daemons and socket theft

**Evidence:** `IpcServer.start()` unlinks an existing socket unconditionally; a second daemon unlinks the first's socket and binds its own. The first keeps running with no IPC, still enforcing rules.

**Fix:** `flock` a lock file (`$XDG_RUNTIME_DIR/data-guardian.lock`) with `LOCK_EX|LOCK_NB` before binding; exit 0 with a clear message if already held. Bind to a temp name then `os.replace` for atomic socket publication.

#### BUG-13 · IPC threads race the main loop (state, firewall, libnm)

**Evidence:** `_handle_ipc_request` mutates `_current_profile`, `_is_quota_blocked`, and calls `firewall.apply_profile()` from client threads while the main loop does the same; `self.monitor` (libnm) and `_pending_notifications` are touched without locks.

**Fix:** command/response architecture.

```
main loop owns: monitor, firewall, db writes, profile state
IPC thread: validates request -> enqueues Command(fn, args, Event) -> waits with timeout
main loop: drains queue at top of each tick and after sleep (use Event.wait(interval))
snapshot: self._status_snapshot rebuilt by main loop; get_status returns it lock-free
```

Shorten `poll_interval` wakeups with `threading.Event` so commands are processed in <100 ms and SIGTERM is immediate.

#### BUG-14 · Notifications are lost/duplicated across clients

**Evidence:** `get_status` clears `_pending_notifications`; tray, dashboard overview, and dashboard network panel all poll independently → first client consumes, others never see; if the response fails to send, the notification is gone. `_notify` *also* calls `notify-send` directly when `DISPLAY` is set → duplicates when running manually.

**Fix:** monotonic notification log with cursors.

```
notifications(id INTEGER PK AUTOINCREMENT, ts, title, message, urgency)
get_status(since_id) -> {notifications:[...], last_notification_id}
client stores cursor; no destructive read
retention: DELETE WHERE ts < now-24h (in maintenance tick)
_notify(): DB insert + wake clients; never call notify-send from the daemon
```

Exactly one UI component (tray) delivers desktop notifications, using `notify-send -r <id>` replacement semantics.

#### BUG-15 · Orphan sessions on restart / hotspot switch

**Evidence:** DB shows two sessions with `ended_at IS NULL` (15 and 16). `_on_hotspot_activated` starts a new session without closing `_active_session_id`.

**Fix:** on activation, close any open session first; on startup, `UPDATE sessions SET ended_at = COALESCE(ended_at, last_update) WHERE ended_at IS NULL`.

#### BUG-16 · Changing the designated hotspot while connected leaves stale firewall rules

**Evidence:** `set_hotspot_uuid` updates `monitor.designated_uuid` but does not re-evaluate `_is_hotspot_active`. If you designate a different network, the old rules stay applied and the new hotspot is unprotected until a transition that never happens.

**Fix:** after changing the designation, force `monitor.invalidate()` and re-run activation/deactivation logic in the same tick (main loop owns it).

#### BUG-17 · DB and config file permissions leak usage history

**Evidence:** DB is `0644` while `report.md` claims `0600`; directory `0775`. Any local user can read browsing/usage history.

**Fix:** `os.umask(0o077)` before creating DB/config; `chmod 0600` on DB, `-wal`, `-shm`, and config; `0700` on data dir. Add a startup self-heal for existing installs.

#### BUG-18 · `uninstall.sh` fails to remove shims (missing `PYTHONPATH`)

**Evidence:** it runs `python3 -m src.data_guardian.main remove-shims ... || true` from an arbitrary CWD without `PYTHONPATH`, so the import fails and the error is swallowed; `~/.local/bin/pip`, `npm`, `cargo` survive.

**Fix:** `cd "$PROJECT_ROOT"` and export `PYTHONPATH`, or call `shims.remove_shims()` directly via a small `python3 -c` with the absolute path. Never `|| true` on a step that matters — collect failures and report at the end.

#### BUG-19 · `install.sh` run with `sudo` writes user files into `/root`

**Evidence:** `HOME` is `/root` under sudo, so the wrapper, shims, and autostart land in `/root/.local` and `/root/.config`; the real user gets nothing. Same problem in `uninstall.sh` and `emergency-disable.sh` (wrong DB path).

**Fix:** resolve the invoking user:

```
TARGET_USER=${SUDO_USER:-$USER}
TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
run_as_user() { sudo -u "$TARGET_USER" env HOME="$TARGET_HOME" "$@"; }
```

Refuse to run the user phase as root without `SUDO_USER` unless `--system --user-home` is given.

#### BUG-20 · Installer does not install the helper, sudoers, icon, or desktop entry

**Evidence:** `install.sh --system` installs only the APT hook and the systemd unit. The helper, `/etc/sudoers.d/linux-data-guardian`, the icon, and `~/.local/share/applications/*.desktop` were installed manually and are not reproducible.

**Fix:** add idempotent steps with validation:

```
install -m 0755 scripts/data-guardian-helper /usr/local/libexec/
sed "s/@USER@/$TARGET_USER/" config/sudoers-data-guardian > /etc/sudoers.d/linux-data-guardian
chmod 0440; visudo -cf /etc/sudoers.d/linux-data-guardian
install -m 0644 assets/icon.png /usr/share/icons/hicolor/512x512/apps/linux-data-guardian.png
install -m 0644 systemd/data-guardian.desktop "$TARGET_HOME/.local/share/applications/"
```

Template the unit (`User=`, `HOME=`, `XDG_RUNTIME_DIR=/run/user/%U`) instead of shipping `jagdish` paths.

#### BUG-21 · `rulesets.py` is dead code and its strict profile fails `nft -c`

**Evidence:** nothing imports `build_nft_ruleset`; `nft -c` fails on `socket cgroupv2 level 2 "data_guardian/foreground"` (path does not exist). `tests/test_firewall_rules.py` tests this dead module, giving false confidence.

**Fix:** delete `rulesets.py` and its tests; move rule generation into the helper (single source of truth) and test the helper's output with `nft -c` in a user namespace.

#### BUG-22 · Strict mode is not strict (docs vs reality)

**Evidence:** helper `--apply-strict` is identical to balanced (`skuid 42 drop` only). It does not restrict ports, does not use the whitelist, and does not consult the foreground window.

**Fix (decision D2):** implement a real, honest strict profile now:

```
chain output {
  oif "lo" accept
  ct state established,related accept
  ct state invalid drop
  udp dport {53,67,68} accept
  tcp dport {53} accept
  # explicit allowlist (config, defaults below)
  tcp dport {80,443,22} accept
  oif "$IFACE" skuid 42 ct state new drop
  oif "$IFACE" ct state new drop          # everything else on the metered link
}
```

and **reword the docs**: "Strict = web/SSH/DNS only; background updaters blocked" — not "foreground-app-only". A cgroup-v2 supervisor is a separate, larger project (§6, D2-B) and must not be advertised before it exists.

#### BUG-23 · Whitelist is loaded but never enforced

**Evidence:** `ConfigManager.whitelist` has zero consumers.

**Fix:** either enforce it (feed `allowed_ports_tcp/udp` and an optional `skuid`/`cgroup` set into the strict ruleset) or delete `whitelist.yaml`, `_load_whitelist`, and the docs claims. Recommendation: enforce **ports** now (safe, testable); defer per-process enforcement.

#### BUG-24 · `data-guardian doctor` does not exist

**Evidence:** `diagnostics.py` defines a full suite; `main.py` has no `doctor` subcommand. `report.md` claims it shipped.

**Fix:** add `doctor [--json] [--fix]`, exit non-zero on failures, and include: python deps, nft present, helper installed+executable, sudoers NOPASSWD, systemd unit active, IPC ping, DB writable + schema version, firewall table state, interface resolvable, DISPLAY/XAUTHORITY for window tracking.

#### BUG-25 · Active-window tracking never works under systemd

**Evidence:** `get_status` returns `active_app: "unknown"` with `DISPLAY=:0.0`; the unit exports neither `DISPLAY` nor `XAUTHORITY`.

**Fix:** either (a) discover the session bus/display from logind (`loginctl show-user`), or (b) move window tracking into the user-session tray process and push the app name over IPC. Recommendation: (b) — no X auth in a system service, no polling in the daemon. Until then, remove the "Focused App" field or label it "unavailable".

#### BUG-26 · Tray click callback signature is wrong

**Evidence:** `_on_icon_clicked(self, widget=None)` is connected to `XApp.StatusIcon::activate`, which passes `(icon, button, time)` → `TypeError`, dashboard does not open.

**Fix:** `def _on_icon_clicked(self, *args):` and connect `button-press-event`/`activate` per backend; add a headless smoke test that invokes the callback with the backend's real arity.

#### BUG-27 · Approvals log frame is built but never shown

**Evidence:** `dashboard.py` creates `log_frame`, `log_box`, `scrolled`, `self.log_list`, then returns without `outer.pack_start(log_frame, ...)`. The "Recent Download Approvals" panel is invisible.

**Fix:** pack it (after the profile frame, before the CLI tab) and add a UI test that walks the widget tree and asserts the frame is a descendant of the window.

#### BUG-28 · Dashboard writes YAML from a 3-second refresh

**Evidence:** `_refresh_network_panel` calls `set_network_profile()` on every refresh when the displayed profile differs from the daemon's; each call rewrites the whole config file.

**Fix:** never persist from a refresh path. Persist only on explicit user action; compare and update widgets only.

#### BUG-29 · "This Network Only" radio actually changes the global default and the active network

**Evidence:** dashboard sends `set_profile`, which sets `active_profile` globally and writes the **active** connection's profile — even when the user selected a different row in the network combo.

**Fix:** add IPC `set_network_profile {uuid, profile}`; keep `set_profile` for the active network only. Dashboard sends the uuid of the selected row.

#### BUG-30 · `nmcli -t` parsing breaks on SSIDs containing `:`

**Evidence:** `split(":")` on escaped output; `nmcli -t` escapes `:` as `\:`.

**Fix:** use `nmcli -t -f NAME,UUID,TYPE,DEVICE --escape yes` and parse escapes, or better `nmcli -g NAME,UUID,TYPE,DEVICE` per connection, or use libnm exclusively.

#### BUG-31 · `emergency-disable.sh` unlinks the live daemon socket

**Evidence:** script `rm -f`s the socket; the daemon keeps a bound-but-unlinked socket, so the tray can no longer reach it until restart. The daemon may also re-apply rules on the next network transition.

**Fix:** ask the daemon to bypass via IPC (`emergency_bypass`), and only fall back to a direct flush if IPC is unavailable. Never unlink a socket you do not own.

#### BUG-32 · `pip3` shim missing / shims break if the project moves

**Evidence:** `~/.local/bin/pip3` absent; shims embed an absolute project path and a failing import exits 1, which blocks the tool with no explanation.

**Fix:** shims should resolve the project dir from a small config file written at install time, and on import failure print a clear message and (configurable) fail **open** for read-only commands, fail **closed** only for download commands.

---

### P2 — robustness / performance / UX

| ID | Issue | Fix |
|---|---|---|
| BUG-33 | `List` used in `daemon.py` annotation without import | add `from typing import List` or use `list[...]` |
| BUG-34 | `stop()` runs twice (signal handler + `finally`) → duplicate flush | make `stop()` idempotent with a flag |
| BUG-35 | IPC `get_status` does DB + monthly SUM + config reads on every call, 3× per 3 s per client | serve `_status_snapshot`; recompute once per tick |
| BUG-36 | `send_ipc_command` 2 s timeout while `set_profile` performs sudo (up to 5 s) → UI reports failure though the command succeeded | enqueue and return `accepted`; poll result via snapshot |
| BUG-37 | UI calls IPC synchronously on the GTK main thread → freezes | run IPC in a worker thread, marshal results with `GLib.idle_add` |
| BUG-38 | `open_apt_window` accepts any interface, no hotspot check | validate + require hotspot |
| BUG-39 | `grant_allowance` accepts negative/huge MB | clamp `0 < mb <= 10000`, return error |
| BUG-40 | `set_hotspot_uuid` accepts arbitrary strings | validate UUID v4 shape and existence in saved connections |
| BUG-41 | Monthly total depends on the currently designated UUID → changing it resets the month | persist `metered=1` on the row at write time (§3.1) |
| BUG-42 | `history_retention_days` config unused → DB/WAL grows forever | maintenance tick: delete rows older than retention, `PRAGMA wal_checkpoint(TRUNCATE)` |
| BUG-43 | 4 MB WAL file observed | `wal_autocheckpoint=256`, checkpoint on clean shutdown |
| BUG-44 | Byte counter loses up to one poll interval of traffic after activation | reset counter **before** applying rules, sample immediately, and use `time.monotonic()` deltas |
| BUG-45 | `get_network_history(days=60)` includes 61 dates | `date > date('now', ?)` |
| BUG-46 | Chart plots zero-lines for networks absent in range | filter series with all-zero data |
| BUG-47 | `matplotlib` imported at dashboard module load (~1–2 s, ~40 MB) | import inside the Statistics tab builder |
| BUG-48 | Stray `Gtk.Separator(...).show()` never added to a container | remove |
| BUG-49 | Two IPC polls per 3 s in dashboard (overview + network panel) | one timer, one snapshot, fan out to panels |
| BUG-50 | Tray has no APT-window item or countdown | add |
| BUG-51 | Dashboard banner shows quota fraction for non-metered networks | show "unmetered — no quota" |
| BUG-52 | No "protection failed" indicator anywhere | `firewall_error` in status + red banner + one notification |
| BUG-53 | CLI `status` lacks monthly data and JSON output | add `--json`, monthly fields, `apt_window_remaining` |
| BUG-54 | Config ignores `XDG_CONFIG_HOME` / `XDG_DATA_HOME` | honor them, fall back to `~/.config`, `~/.local/share` |
| BUG-55 | No `__main__.py` | add `python3 -m src.data_guardian` |
| BUG-56 | `ConfigManager` prints warnings to stdout | use `logging` |
| BUG-57 | No dependency declaration | add `pyproject.toml` with runtime deps + pytest/ruff config |
| BUG-58 | No CI | add GitHub Actions (cloud builds are free and avoid metered downloads locally) |
| BUG-59 | Deploy repo has skip-worktree'd missing files | see §7 |
| BUG-60 | `assets/wifi.png` is byte-identical to `icon.png` | delete |

---

## 3. Target architecture (algorithms)

### 3.1 Data model v3 — separate *statistics* from *quota*

The current schema conflates "bytes seen on any interface" with "metered bytes that consume the budget". Split them.

```sql
-- statistics only (all interfaces)
CREATE TABLE daily_usage (
  date TEXT PRIMARY KEY, rx_bytes INTEGER NOT NULL DEFAULT 0,
  tx_bytes INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);

-- authoritative per-network stats; metered flag is frozen at write time
CREATE TABLE network_daily_usage (
  date TEXT NOT NULL, connection_uuid TEXT NOT NULL, connection_name TEXT NOT NULL,
  rx_bytes INTEGER NOT NULL DEFAULT 0, tx_bytes INTEGER NOT NULL DEFAULT 0,
  metered INTEGER NOT NULL DEFAULT 0,          -- 1 iff designated hotspot at write time
  updated_at TEXT NOT NULL,
  PRIMARY KEY (date, connection_uuid)
);

-- quota state, independent of traffic rows
CREATE TABLE quota_state (
  date TEXT PRIMARY KEY,
  daily_limit_bytes INTEGER NOT NULL DEFAULT 0,
  allowance_bytes INTEGER NOT NULL DEFAULT 0,
  warning_fired INTEGER NOT NULL DEFAULT 0,
  exceeded INTEGER NOT NULL DEFAULT 0,
  blocked_reason TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, title TEXT NOT NULL, message TEXT NOT NULL,
  urgency TEXT NOT NULL DEFAULT 'normal'
);
```

**Why `metered` at write time:** changing the designated hotspot must not retroactively move last week's bytes into (or out of) the budget. It also makes the monthly query a single indexed `SUM`.

**Migration v3:**

```
1. add column metered to network_daily_usage (default 0)
2. UPDATE network_daily_usage SET metered = 1
     WHERE connection_uuid = (current designated uuid)
       AND date >= start_of_current_billing_cycle
3. create quota_state; seed today's row from daily_usage
   (daily_limit_bytes = last non-zero quota_limit_bytes, allowance = temporary_allowance_bytes)
4. create notifications
5. drop quota columns from daily_usage only after a backup is written
   (keep them one release for rollback)
```

### 3.2 Quota engine — one pure function, two callers

```python
@dataclass(frozen=True)
class QuotaVerdict:
    daily_used: int; daily_limit: int; daily_pct: float
    monthly_used: int; monthly_limit: int; monthly_pct: float
    level: Literal["ok", "warning", "exceeded"]
    reason: Literal["daily", "monthly"] | None
    enforce: bool

def evaluate(db, cfg, today, *, now_monotonic) -> QuotaVerdict:
    st  = db.quota_state(today)                       # upsert-if-missing, never returns None
    day = db.metered_total(today)                     # SUM(network_daily_usage) WHERE metered=1
    mon = db.metered_total_cycle(cfg.billing_cycle_day, today)
    dlim = cfg.daily_limit_mb * MB
    mlim = cfg.monthly_limit_mb * MB

    d_eff = st.daily_limit_bytes + st.allowance_bytes if st.daily_limit_bytes else 0
    d_hit = dlim > 0 and d_eff > 0 and day >= d_eff
    m_hit = mlim > 0 and mon >= mlim

    reason = "monthly" if m_hit else ("daily" if d_hit else None)
    pct    = max(day/d_eff if d_eff else 0, mon/mlim if mlim else 0)
    warn   = cfg.warning_threshold_percent / 100
    level  = "exceeded" if reason else ("warning" if pct >= warn else "ok")
    return QuotaVerdict(..., reason=reason, level=level,
                        enforce=bool(reason) and cfg.action_on_exhaustion == "block_unapproved")
```

Rules:

* `0` limit = unlimited, never "exceeded".
* Warning fires once per threshold per day (`quota_state.warning_fired`), including a new 95 % threshold.
* `enforce` is the **only** input to `is_quota_blocked` and firewall re-application.
* Every write path (`record_usage_delta`, `grant_allowance`, startup, profile change) calls `evaluate()` — no duplicated arithmetic.

### 3.3 Firewall application — single writer, idempotent, verified

```
def apply(profile, iface, *, quota_exceeded, apt_window) -> Result:
    if not iface: return Err("no interface")           # never guess
    ruleset = render(profile, iface, quota_exceeded, apt_window)
    rc, err = run(["nft", "-c", "-f", "-"], input=ruleset)
    if rc != 0: return Err(f"validation failed: {err}")
    rc, err = run(["nft", "-f", "-"], input=ruleset)
    if rc != 0: return Err(f"apply failed: {err}")
    return Ok(ruleset)
```

* `render()` emits `destroy table` first (BUG-04).
* Only the main loop calls `apply()`; IPC enqueues (BUG-13).
* On failure: record `firewall_error`, notify once, show red banner; never claim success.
* On shutdown: flush, then verify the table is gone (`nft list table` rc != 0).

### 3.4 Notification bus

```
daemon:  notify() -> INSERT INTO notifications
clients: get_status(since_id) -> rows with id > since_id; store cursor in memory
tray:    the only component that calls notify-send (with -r id)
cleanup: DELETE FROM notifications WHERE ts < now - 24h
```

No destructive reads, no duplicates, no lost messages, survives UI restarts.

### 3.5 Privilege model (keep it boring)

* Daemon: **user** process, no root.
* One root surface: `/usr/local/libexec/data-guardian-helper`, 0755 root:root, strict argument validation, `nft` only.
* Sudoers: exactly that path, NOPASSWD, no wildcards needed because the helper validates.
* No root Python. No `PYTHONPATH` in `/etc`. No `eval`. Interface regex `^[A-Za-z0-9_.-]{1,15}$`, action ∈ {flush,status,apply-balanced,apply-strict,apply-blocked,allow-apt}, max 2 args, `umask 077`, `set -euo pipefail`.
* Installer validates with `visudo -cf` and `nft -c` before enabling the service.

---

## 4. Documentation truth pass

Every claim below was checked against the running system. **F = false, P = partially true, T = true.**

| # | Claim (file) | Reality | Action |
|---|---|---|---|
| 1 | "Production Ready & Verified Active" (`report.md`) | F — 5 P0 bugs | rewrite after fixes; add a verification log with commands + dates |
| 2 | "Strict mode routes only the active window" (`README`, `report`) | F — strict == balanced | implement port-strict, reword |
| 3 | "Whitelisted tools (opencode, git, ssh) work in strict" (`README`) | F — whitelist unused | enforce ports / delete claim |
| 4 | "Blocks telemetry and silences packagekitd, unattended-upgrades, apt-daily" (`README`) | F — only UID 42 is dropped | implement timer masking (needs sudo) or delete claim |
| 5 | "15-minute APT window" (`dashboard`, `README`) | F — permanent, possibly unreachable | BUG-05 |
| 6 | "Prompts show command and download scope/size" (`README`, `plan`) | F — size always 0 | add argv display; drop "size" or implement `apt-get --print-uris` estimate |
| 7 | "`data-guardian doctor` health suite" (`report`) | F — not wired | BUG-24 |
| 8 | "60-day history, native Cairo chart" (`report`) | P — matplotlib, 61 dates | fix wording + off-by-one |
| 9 | "< 35 MB RAM" (`README`) | F — daemon 44.5 MB, tray 87.8 MB, dashboard 113 MB | measure and publish real numbers, or optimize (lazy matplotlib, drop AppIndicator duplication) |
| 10 | "Flushes in 5 ms" (`README`) | unverified | measure p95 and publish, or delete |
| 11 | "Config 0644, DB 0600" (`report`) | P — config yes, DB 0644 | BUG-17 |
| 12 | "4 CLI shims installed" (`report`) | F — pip3 missing | BUG-32 + verify in installer |
| 13 | "Installer installs helper, sudoers, service" (`README`) | F — only service + APT hook | BUG-20 |
| 14 | "Uninstall removes shims, configs, autostart" (`README`) | F — shims survive, config/DB remain | BUG-18 + decide DB policy |
| 15 | "D-Bus signals, zero polling" (`plan`) | F — 5 s polling | reword or implement NM signals (Phase 2) |
| 16 | "Root daemon / cgroup supervisor / dialogs.py" (`plan`) | F — user daemon, no cgroups, no dialogs.py | mark as roadmap, not shipped |
| 17 | "Daily + monthly budgets with 80 % alerts" | P — daily only; monthly silent | add monthly warning |
| 18 | "Never touches ufw" | T — separate table | keep |
| 19 | "Byte-exact from /proc/net/dev" | T | keep |
| 20 | "All 26 tests pass" | T but misleading — tests cover dead code | replace with meaningful tests (§5) |

Deliverable: after fixes, `report.md` becomes a **dated verification log** (command → observed output), not a marketing summary. `plan.md` gets `[shipped] / [deferred] / [not implemented]` markers. `audit.md`/`step.md` get a status header pointing at this file.

---

## 5. Test plan — from "26 green" to "26 meaningful + 20 regressions"

### 5.1 New regression tests (must fail before the fix, pass after)

| Test | Asserts |
|---|---|
| `test_startup_empty_db_not_blocked` | BUG-01 |
| `test_startup_over_limit_blocked` | positive control |
| `test_grant_allowance_empty_day` | BUG-02 |
| `test_allowance_unblocks_and_reapplies` | BUG-02/05 |
| `test_apt_window_expires` | BUG-05 (fake monotonic clock) |
| `test_helper_apply_is_idempotent` | BUG-04 (userns, rule count) |
| `test_helper_rejects_injection` | `;`, `"`, `$(...)`, newline, 16-char iface |
| `test_helper_apply_requires_iface` | BUG-09 |
| `test_flush_reports_failure` | BUG-10 (stub `nft` exit 1) |
| `test_metered_only_counts_toward_quota` | BUG-06 |
| `test_monthly_enforced_with_zero_daily` | BUG-07 |
| `test_warn_only_does_not_report_blocked` | BUG-08 |
| `test_pip3_readonly_not_gated` (param table) | BUG-11 |
| `test_single_instance_second_daemon_exits` | BUG-12 |
| `test_ipc_set_profile_does_not_race` | BUG-13 (concurrent calls, no duplicate rules) |
| `test_notifications_cursor_no_loss` | BUG-14 (two clients) |
| `test_session_closed_on_restart` | BUG-15 |
| `test_designation_change_reevaluates` | BUG-16 |
| `test_db_permissions_0600` | BUG-17 |
| `test_uninstall_removes_shims` | BUG-18 |
| `test_installer_no_apt_hook` | BUG-03 (`grep` the generated files) |
| `test_dashboard_widget_tree_contains_approvals` | BUG-27 |
| `test_tray_callback_arity` | BUG-26 |
| `test_nmcli_parsing_with_colon_ssid` | BUG-30 |
| `test_config_honors_xdg` | BUG-54 |

### 5.2 Ruleset validation matrix (run in `unshare -Urn`)

For each profile ∈ {balanced, strict, blocked} × quota ∈ {false, true} × apt_window ∈ {false, true}:

1. `nft -c -f -` must exit 0.
2. Apply twice → rule count identical (idempotence).
3. Apply profile A then B → no `oif` rule for A's interface remains.
4. Blocked → `ping`/`curl` fail, DNS resolves, loopback works.
5. Strict → 443/22 allowed, 8080 dropped, UID 42 dropped.
6. Flush → table absent.

### 5.3 Chaos/soak

* `kill -9` the daemon while blocked → rules persist; restart must reconcile (apply or flush per policy) and close the orphan session.
* SIGTERM latency < 1 s (Event-based sleep).
* 24 h on the real hotspot: no rule growth (`nft list | wc -l` stable), RSS stable, WAL bounded, no duplicate notifications.
* 1000 rapid `set_profile` calls → no error, rule count stable.

### 5.4 CI

`.github/workflows/ci.yml` on `ubuntu-24.04`: install `python3-gi gir1.2-gtk-3.0 python3-yaml nftables network-manager`, run `pytest -q`, run the userns ruleset matrix, `ruff check`, `python -m compileall`. Runs in the cloud — no metered data.

---

## 6. Dead code & file cleanup

| Path | Why | Action |
|---|---|---|
| `src/data_guardian/firewall/rulesets.py` | imported nowhere; broken strict | delete |
| `tests/test_firewall_rules.py` | tests dead code | delete, replace with helper tests |
| `assets/wifi.png` | md5-identical to `icon.png` | delete |
| `Database.get_network_history` | 0 refs | delete |
| `Database.get_all_networks_history` | 0 refs (chart uses another path) | delete or use |
| `Database.get_network_today_usage` | 0 refs | delete or use in UI |
| `format_cli_report` (`diagnostics.py`) | 0 refs | delete |
| `generate_apt_config` (`apt_hook.py`) | 0 refs; dangerous | delete file |
| `X11WindowTracker.get_process_family_pids` | 0 refs | delete or implement whitelist |
| `enforce_metered_flag` config | unused | delete or implement |
| `timeout_action` config | unused by gate | implement or delete |
| `.pytest_cache/`, `__pycache__/`, `*.pyc` | junk | gitignore + clean |
| `~/.local/share/linux-data-guardian/usage.db` | stray 0-byte file (real DB is `guardian.db`) | investigate creator, delete |
| `emergency-disable.sh` DB logging | guarded by `command -v sqlite3`, which is not installed → silently skipped | use the Python API instead of the `sqlite3` CLI |
| `assets/` icon duplicates | — | keep one |
| stale `.git/REBASE_HEAD`, `.git/ORIG_HEAD` | leftover | inspect, then remove |

Add to `.gitignore`: `.pytest_cache/`, `__pycache__/`, `*.pyc`, `dist/`, `*.log`, `.ruff_cache/`, `.mypy_cache/`.

---

## 7. Git & deployment strategy (fix the corrupted deploy repo)

**Current state:** two independent clones of the same remote. The deploy clone has `skip-worktree` set on `audit.md`, `plan.md`, `step.md`, and all `tests/*`, which are physically absent. `git status` hides this, so a later `git checkout`/`pull` will fail or silently diverge. Deploy also has 3 uncommitted modified files.

**Recommended model — one source of truth:**

```
build repo  = source of truth (full tree, tests, docs, CI)
deploy dir  = runtime artifact, produced by scripts/deploy.sh (no nested .git)
```

`scripts/deploy.sh`:

```
rsync -a --delete \
  --include 'src/***' --include 'scripts/***' --include 'config/***' \
  --include 'systemd/***' --include 'assets/***' \
  --exclude '*' "$BUILD/" "$DEPLOY/"
write VERSION (git rev-parse --short HEAD)
```

Then "push to the implementation area" is one deterministic command, and the deployment can never diverge. **This requires deleting the deploy `.git` directory** (it is a duplicate history with the same remote). Ask the user before deleting; the safe alternative is `git worktree add`, which keeps history but also ships tests/docs.

**Commit/push sequence (after fixes):**

```
git add -A
git commit -m "fix: close P0/P1 audit findings (quota, nft idempotence, installer, IPC)"
git push origin main
./scripts/deploy.sh
```

---

## 8. Phased roadmap

### Phase 0 — safety net (no behavior change)
1. Commit the 3 uncommitted files (profile persistence + notifications) so nothing is lost.
2. Back up the live DB (`~/.local/share/linux-data-guardian/guardian.db` + `-wal` + `-shm`) to `/tmp`.
3. Add `pyproject.toml`, `.gitignore` updates, CI skeleton.
4. Delete dead code listed in §6.

**Exit:** clean tree, backup exists, tests still 26 green.

### Phase 1 — stop the bleeding (P0)
1. BUG-01/02/03/04/05 + BUG-09/10.
2. Schema v3 + `QuotaEngine` (§3.1/3.2).
3. Helper idempotence + timed APT window.
4. Installer rewrite: remove APT hook, install helper/sudoers/icon/desktop, `visudo -cf`, `nft -c`, `SUDO_USER` handling.
5. Regression tests for every item.

**Exit:** empty DB → not blocked; +100 MB on empty day persists; two applies → identical rule count; APT window closes after 15 min; `install.sh --system` produces a working install from scratch on a clean VM.

### Phase 2 — correctness & concurrency (P1)
1. BUG-11..BUG-32 (shim classification, single instance, IPC command queue + snapshot, notification bus, orphan sessions, designation re-evaluation, permissions, uninstaller, strict profile, doctor, tray/dashboard fixes, nmcli parsing).
2. UI: worker-thread IPC, single poll timer, approvals frame packed, countdown, red failure banner.

**Exit:** 45+ tests, no GTK freeze, no duplicate/lost notifications, strict actually restricts.

### Phase 3 — docs & polish (P2)
1. Rewrite `report.md` as a verification log; mark `plan.md` statuses; add `docs/` and a CHANGELOG.
2. Real resource measurements; update or delete the 35 MB/5 ms claims.
3. Optional: NM D-Bus signals instead of polling; cgroup supervisor spike (not advertised until done); packaging (`pipx`/`.deb`).

**Exit:** every claim in every `.md` is T or explicitly marked "not implemented / roadmap".

### Phase 4 — deploy
1. `scripts/deploy.sh` sync to `/home/jagdish/Desktop/System Files/Linux Data Guardian/`.
2. Reinstall helper/sudoers/unit/icon; `systemctl daemon-reload && restart`.
3. Live smoke: status, doctor, profile switch, quota block/unblock, APT window, uninstall dry-run.
4. `git push`.

---

## 9. Actions that need `sudo` (you run these, I will not)

| Step | Command | Purpose | Rollback |
|---|---|---|---|
| 1 | `sudo ./scripts/install.sh --system` | install/refresh helper, sudoers rule, systemd unit, icon; restart the service | restore `/tmp/opencode/ldg-backup/`, then `systemctl restart data-guardian.service` |
| 2 | `data-guardian doctor` (no sudo) | confirm helper capabilities, cgroups, daemon, permissions | — |
| 3 | `sudo rm -f /etc/apt/apt.conf.d/99data-guardian` (only if present) | remove the legacy LPE hook installed by older versions | none needed |

`install.sh --system` validates the generated sudoers file with `visudo -cf`
before installing it. Do not mask APT timers: the current design blocks APT
traffic at the firewall instead of disabling system services.

Deleting the deploy `.git` (for the rsync model) needs no sudo, but I will ask
before removing it.

---

## 10. Risks & rollback

| Risk | Mitigation |
|---|---|
| Schema migration loses history | backup DB+WAL before migration; migration runs in one transaction; keep old quota columns for one release |
| New strict profile blocks needed traffic | defaults include 80/443/22/53/67/68; one-click Off/Bypass; `nft -c` before apply; 15 s auto-rollback if the user does not confirm connectivity (optional) |
| Helper change locks out enforcement | keep `--status` and `--flush` working in every version; installer validates before restart |
| Deploy repo deletion loses uncommitted work | commit/push first; `cp -a` backup; only then remove `.git` |
| Notification bus growth | 24 h retention + index on `id` |
| IPC command queue deadlock | bounded queue, 2 s timeout, return `busy` |

---

## 11. Acceptance checklist ("bug-free" gate)

- [ ] No P0/P1 open; every registry item has a linked test.
- [ ] `pytest -q` ≥ 45 passing, including all 25 regression tests; no test touches the real `~/.config` or DB.
- [ ] Ruleset matrix green in `unshare -Urn`; two applies → identical rule count.
- [ ] Fresh install on a clean VM works with only the documented commands.
- [ ] `install.sh` never writes to `/etc/apt/apt.conf.d`; `grep -r PYTHONPATH scripts/` empty.
- [ ] `data-guardian doctor` exits 0 on a healthy install and non-zero with a clear reason when the helper/sudoers/unit is missing.
- [ ] Empty DB → not blocked; +100 MB on an empty day persists; monthly-only quota enforced; unmetered traffic does not consume quota.
- [ ] APT window opens, is reachable, and closes after 15 minutes without a restart.
- [ ] Tray click opens the dashboard; approvals panel visible; no GTK main-thread IPC calls.
- [ ] Two clients receive every notification exactly once; no lost messages.
- [ ] DB/config 0600/0700; WAL bounded; no orphan sessions after restart.
- [ ] Every `.md` claim is T or explicitly marked roadmap; `report.md` is a dated command/output log.
- [ ] 24 h hotspot soak: stable RSS, stable rule count, no duplicate rules, no crash.

---

## Appendix A — verification commands used

```bash
pytest -q
systemctl status data-guardian --no-pager
python3 -m src.data_guardian.main status
sudo -n /usr/local/libexec/data-guardian-helper --status
sudo -n /usr/local/libexec/data-guardian-helper --apply-strict wlan0
unshare -Urn nft -c -f <(python3 -c 'from src.data_guardian.firewall.rulesets import build_nft_ruleset as b; print(b("strict"))')
python3 - <<'PY'   # empty-DB false positive
from src.data_guardian.database import Database
from src.data_guardian.daemon import DataGuardianDaemon
...
PY
python3 - <<'PY'   # pip3 misclassification
from src.data_guardian.approvals.gate import ApprovalGate
print(ApprovalGate().is_download_action("pip3", ["list"]))
PY
git -C "/home/jagdish/Desktop/System Files/Linux Data Guardian" ls-files -v | grep '^S '
ps -o rss=,pcpu= -p <daemon|tray|dashboard>
stat -c '%a %n' ~/.local/share/linux-data-guardian/guardian.db
```

## Appendix B — decision log (DECIDED)

| ID | Decision | Chosen |
|---|---|---|
| D1 | Quota accounting model | schema v3 with frozen `metered` flag |
| D2 | Strict mode | **real per-process enforcement: foreground app + `opencode` + `antigravity` CLI**, via cgroup-v2 socket matching (see §12) |
| D3 | APT gating | firewall UID 42 + daemon-timed window; no root Python hook |
| D4 | Updater silencing claim | implement or delete — pending |
| D5 | Deployment model | **rsync runtime artifact; delete deploy `.git` after backup** |
| D6 | Uninstall data policy | keep DB by default, add `--purge` |
| D7 | Window tracking | move to tray process over IPC (needed for D2 too) |

---

## 12. Strict mode via cgroup v2 socket matching (D2)

Goal: on the metered link, allow **only** the focused (foreground) application, the
`opencode` CLI, and the `antigravity` CLI; drop every other new outbound connection.

Mechanism: nftables matches the **socket's owning cgroup**:

```
ct state established,related accept          # do not kill in-flight transfers
socket cgroupv2 level 2 "data_guardian/foreground" ct state new accept
socket cgroupv2 level 2 "data_guardian/whitelist"  ct state new accept
udp dport {53,67,68} accept; tcp dport 53 accept
oif "$IFACE" ct state new drop
```

Two cgroups are required, `foreground` and `whitelist`, under a top-level
`data_guardian/` cgroup. Membership is set by the privileged helper (root), which
alone can move arbitrary processes:

```
helper --cgroup-init                     # mkdir + delegate, idempotent
helper --cgroup-set foreground <pid>...  # move a process tree
helper --cgroup-clear foreground
helper --apply-strict <iface> [--cgroups]
```

Daemon behaviour (only while on the hotspot and profile == strict):

```
on focus change (tray reports app_id/pid over IPC):
    pids = descendant_pids(focused_pid)          # /proc/*/stat walk
    helper --cgroup-set foreground pids...
on app_id leaving focus: helper --cgroup-clear foreground
on start of opencode/antigravity (shim hook): helper --cgroup-set whitelist pid
```

Feasibility gate (must pass before this ships): in `unshare -Urn` + a VM check —
(1) `nft -c` accepts the rules; (2) a process moved into `foreground` can reach
443 while an unmoved process is dropped; (3) moving a browser tree twice does not
leak rules; (4) focus-change churn ≤ 50 ms p95.

**Fallback if the gate fails:** strict degrades to "web/SSH/DNS only + UID 42 drop"
(§2 BUG-22), and the docs are worded accordingly. Never ship the per-process claim
without passing the gate.
