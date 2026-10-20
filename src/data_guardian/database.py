"""SQLite database storage and queries for Linux Data Guardian.

Schema v3 separates three concerns that were previously conflated:

* ``daily_usage``        — combined statistics (all interfaces), chart data only.
* ``network_daily_usage``— per-network statistics; the ``metered`` flag is frozen
                           at write time so that changing the designated hotspot
                           never re-labels historical bytes.
* ``quota_state``        — the authoritative quota bookkeeping for a day
                           (limit, temporary allowance, fired warnings, exceeded).

Quota decisions must never be derived from statistics tables.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

CURRENT_SCHEMA_VERSION = 3


class Database:
    """Manages the SQLite connection, schema migrations, and persistent records."""

    def __init__(self, db_path: Path | str, designated_uuid: str = "") -> None:
        self.db_path = Path(db_path)
        self.designated_uuid = designated_uuid or ""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._migrate()
        self._harden_permissions()

    # --- connection / permissions -------------------------------------------

    def _harden_permissions(self) -> None:
        """Usage history is private: data dir 0700, database files 0600."""
        try:
            os.chmod(self.db_path.parent, 0o700)
        except OSError:
            pass
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                try:
                    os.chmod(candidate, 0o600)
                except OSError:
                    pass
        # The WAL/SHM files are created lazily by SQLite; force a connection so
        # they exist now and can be tightened.
        try:
            with self._get_connection() as conn:
                conn.execute("PRAGMA user_version;")
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(f"{self.db_path}{suffix}")
                if candidate.exists():
                    os.chmod(candidate, 0o600)
        except OSError:
            pass

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA wal_autocheckpoint = 256;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    # --- schema --------------------------------------------------------------

    def _init_db(self) -> None:
        """Create tables and indexes if they do not exist."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    connection_uuid TEXT,
                    connection_name TEXT,
                    interface_name TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    last_update TEXT,
                    rx_bytes INTEGER DEFAULT 0,
                    tx_bytes INTEGER DEFAULT 0,
                    profile TEXT DEFAULT 'balanced'
                );
            """)

            # Statistics only — never used for quota decisions.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_usage (
                    date TEXT PRIMARY KEY,
                    rx_bytes INTEGER DEFAULT 0,
                    tx_bytes INTEGER DEFAULT 0,
                    updated_at TEXT
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS network_daily_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    connection_uuid TEXT NOT NULL,
                    connection_name TEXT NOT NULL,
                    rx_bytes INTEGER DEFAULT 0,
                    tx_bytes INTEGER DEFAULT 0,
                    metered INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT,
                    UNIQUE(date, connection_uuid)
                );
            """)

            # Authoritative quota bookkeeping.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS quota_state (
                    date TEXT PRIMARY KEY,
                    daily_limit_bytes INTEGER NOT NULL DEFAULT 0,
                    allowance_bytes INTEGER NOT NULL DEFAULT 0,
                    warnings_fired TEXT NOT NULL DEFAULT '',
                    exceeded INTEGER NOT NULL DEFAULT 0,
                    blocked_reason TEXT,
                    updated_at TEXT NOT NULL
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    urgency TEXT NOT NULL DEFAULT 'normal'
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS approval_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    packages TEXT,
                    estimated_bytes INTEGER DEFAULT 0,
                    decision TEXT NOT NULL,
                    resolved_at TEXT
                );
            """)

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_net_usage_date ON network_daily_usage(date);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_net_usage_uuid ON network_daily_usage(connection_uuid);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_uuid ON sessions(connection_uuid);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_time ON sessions(started_at);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_approval_time ON approval_events(timestamp);")

            cursor.execute("SELECT MAX(version) FROM schema_version;")
            row = cursor.fetchone()
            if not row or row[0] is None:
                cursor.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, datetime('now'));",
                    (1,),
                )
            conn.commit()

    def _columns(self, cursor: sqlite3.Cursor, table: str) -> set:
        cursor.execute(f"PRAGMA table_info({table});")
        return {r["name"] for r in cursor.fetchall()}

    def _migrate(self) -> None:
        """Apply schema migrations for version upgrades."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(version) FROM schema_version;")
            row = cursor.fetchone()
            current = row[0] if row and row[0] is not None else 1

            if current < 2:
                cursor.execute("""
                    SELECT connection_uuid, connection_name,
                           SUM(rx_bytes) AS rx, SUM(tx_bytes) AS tx
                    FROM sessions
                    WHERE date(started_at) = date('now')
                    GROUP BY connection_uuid, connection_name;
                """)
                session_rows = cursor.fetchall()
                if session_rows:
                    for sr in session_rows:
                        if not sr["connection_uuid"]:
                            continue
                        cursor.execute("""
                            INSERT INTO network_daily_usage
                                (date, connection_uuid, connection_name, rx_bytes, tx_bytes, updated_at)
                            VALUES (date('now'), ?, ?, ?, ?, datetime('now'))
                            ON CONFLICT(date, connection_uuid) DO UPDATE SET
                                rx_bytes = excluded.rx_bytes,
                                tx_bytes = excluded.tx_bytes,
                                updated_at = excluded.updated_at;
                        """, (sr["connection_uuid"], sr["connection_name"], sr["rx"], sr["tx"]))
                cursor.execute(
                    "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (2, datetime('now'));"
                )
                conn.commit()
                current = 2

            if current < 3:
                # 1. network_daily_usage.metered ---------------------------------
                if "metered" not in self._columns(cursor, "network_daily_usage"):
                    cursor.execute("ALTER TABLE network_daily_usage ADD COLUMN metered INTEGER NOT NULL DEFAULT 0;")
                if self.designated_uuid:
                    cursor.execute(
                        "UPDATE network_daily_usage SET metered = 1 WHERE connection_uuid = ?;",
                        (self.designated_uuid,),
                    )

                # 2. sessions.last_update ---------------------------------------
                if "last_update" not in self._columns(cursor, "sessions"):
                    cursor.execute("ALTER TABLE sessions ADD COLUMN last_update TEXT;")
                    cursor.execute("UPDATE sessions SET last_update = COALESCE(ended_at, started_at);")

                # 3. quota_state seeding from the legacy daily_usage columns ----
                legacy_cols = self._columns(cursor, "daily_usage")
                if "quota_limit_bytes" in legacy_cols:
                    cursor.execute("""
                        SELECT date, quota_limit_bytes, temporary_allowance_bytes,
                               warning_fired, quota_exceeded
                        FROM daily_usage
                        WHERE quota_limit_bytes > 0 OR temporary_allowance_bytes > 0;
                    """)
                    for r in cursor.fetchall():
                        cursor.execute("""
                            INSERT INTO quota_state
                                (date, daily_limit_bytes, allowance_bytes, warnings_fired,
                                 exceeded, blocked_reason, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                            ON CONFLICT(date) DO UPDATE SET
                                daily_limit_bytes = MAX(daily_limit_bytes, excluded.daily_limit_bytes),
                                allowance_bytes = MAX(allowance_bytes, excluded.allowance_bytes),
                                warnings_fired = excluded.warnings_fired,
                                exceeded = MAX(exceeded, excluded.exceeded);
                        """, (
                            r["date"], r["quota_limit_bytes"], r["temporary_allowance_bytes"],
                            "80" if r["warning_fired"] else "",
                            r["quota_exceeded"], "legacy" if r["quota_exceeded"] else None,
                        ))

                cursor.execute(
                    "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (3, datetime('now'));"
                )
                conn.commit()
                current = 3

            if current < CURRENT_SCHEMA_VERSION:
                cursor.execute(
                    "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, datetime('now'));",
                    (CURRENT_SCHEMA_VERSION,),
                )
                conn.commit()

            # Indexes that depend on migrated columns are created here, after the
            # ALTER TABLE statements above (they would fail on an old schema).
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_net_usage_metered "
                "ON network_daily_usage(metered, date);"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_time ON sessions(started_at);"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_approval_time ON approval_events(timestamp);"
            )
            conn.commit()

    # --- session management ---------------------------------------------------

    def start_session(
        self,
        connection_uuid: str,
        connection_name: str,
        interface_name: str,
        profile: str = "balanced",
    ) -> int:
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO sessions
                    (connection_uuid, connection_name, interface_name, started_at, last_update, profile)
                VALUES (?, ?, ?, ?, ?, ?);
            """, (connection_uuid, connection_name, interface_name, now, now, profile))
            conn.commit()
            return cursor.lastrowid or 0

    def update_session(self, session_id: int, delta_rx: int, delta_tx: int) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE sessions
                SET rx_bytes = rx_bytes + ?,
                    tx_bytes = tx_bytes + ?,
                    last_update = ?
                WHERE id = ?;
            """, (delta_rx, delta_tx, datetime.now().isoformat(), session_id))
            conn.commit()

    def end_session(self, session_id: int) -> None:
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ?, last_update = ? WHERE id = ?;",
                (now, now, session_id),
            )
            conn.commit()

    def close_orphan_sessions(self) -> int:
        """Close sessions left open by an unclean shutdown. Returns rows affected."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cur = conn.execute("""
                UPDATE sessions
                SET ended_at = COALESCE(last_update, started_at)
                WHERE ended_at IS NULL;
            """)
            conn.commit()
            return cur.rowcount or 0

    # --- statistics -----------------------------------------------------------

    def record_usage_delta(
        self,
        delta_rx: int,
        delta_tx: int,
        *,
        metered: bool = False,
        daily_limit_bytes: int = 0,
        target_date: Optional[str] = None,
        connection_uuid: str = "",
        connection_name: str = "",
    ) -> Dict[str, Any]:
        """Record a traffic delta as statistics and update quota bookkeeping.

        ``metered`` is stamped onto the per-network row at write time.
        ``daily_limit_bytes`` is only used to refresh the stored limit; it never
        makes an unlimited plan look exhausted.
        """
        day = target_date or date.today().isoformat()
        now = datetime.now().isoformat()

        with self._get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO daily_usage (date, rx_bytes, tx_bytes, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    rx_bytes = rx_bytes + excluded.rx_bytes,
                    tx_bytes = tx_bytes + excluded.tx_bytes,
                    updated_at = excluded.updated_at;
            """, (day, delta_rx, delta_tx, now))

            if connection_uuid and connection_name:
                cursor.execute("""
                    INSERT INTO network_daily_usage
                        (date, connection_uuid, connection_name, rx_bytes, tx_bytes, metered, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date, connection_uuid) DO UPDATE SET
                        rx_bytes = rx_bytes + excluded.rx_bytes,
                        tx_bytes = tx_bytes + excluded.tx_bytes,
                        connection_name = excluded.connection_name,
                        metered = MAX(network_daily_usage.metered, excluded.metered),
                        updated_at = excluded.updated_at;
                """, (day, connection_uuid, connection_name, delta_rx, delta_tx,
                      1 if metered else 0, now))

            self._ensure_quota_row(cursor, day, now)
            if daily_limit_bytes > 0:
                cursor.execute(
                    "UPDATE quota_state SET daily_limit_bytes = ?, updated_at = ? WHERE date = ?;",
                    (daily_limit_bytes, now, day),
                )
            conn.commit()

        return self.get_today_usage(day)

    def get_today_usage(self, target_date: Optional[str] = None) -> Dict[str, Any]:
        """Combined statistics for the day plus authoritative quota bookkeeping."""
        day = target_date or date.today().isoformat()
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM daily_usage WHERE date = ?;", (day,))
            row = cur.fetchone()
            rx = row["rx_bytes"] if row else 0
            tx = row["tx_bytes"] if row else 0

            cur.execute("SELECT * FROM quota_state WHERE date = ?;", (day,))
            q = cur.fetchone()

            limit = q["daily_limit_bytes"] if q else 0
            allowance = q["allowance_bytes"] if q else 0
            effective = (limit + allowance) if limit > 0 else 0
            warnings = (q["warnings_fired"].split(",") if q and q["warnings_fired"] else [])
            exceeded = bool(q["exceeded"]) if q else False

            return {
                "date": day,
                "rx_bytes": rx,
                "tx_bytes": tx,
                "total_bytes": rx + tx,
                "quota_limit_bytes": limit,
                "temporary_allowance_bytes": allowance,
                "effective_limit_bytes": effective,
                "quota_exceeded": exceeded,
                "warning_fired": bool(warnings),
                "warnings_fired": [int(w) for w in warnings if w],
                "blocked_reason": q["blocked_reason"] if q else None,
            }

    def get_network_today_usage(
        self, connection_uuid: str, target_date: Optional[str] = None
    ) -> Dict[str, Any]:
        day = target_date or date.today().isoformat()
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM network_daily_usage WHERE date = ? AND connection_uuid = ?;",
                (day, connection_uuid),
            )
            row = cur.fetchone()
            if not row:
                return {"date": day, "connection_uuid": connection_uuid,
                        "rx_bytes": 0, "tx_bytes": 0, "total_bytes": 0, "metered": False}
            return {
                "date": row["date"],
                "connection_uuid": row["connection_uuid"],
                "connection_name": row["connection_name"],
                "rx_bytes": row["rx_bytes"],
                "tx_bytes": row["tx_bytes"],
                "total_bytes": row["rx_bytes"] + row["tx_bytes"],
                "metered": bool(row["metered"]),
            }

    # --- quota bookkeeping -----------------------------------------------------

    def _ensure_quota_row(self, cursor: sqlite3.Cursor, day: str, now: str) -> None:
        cursor.execute(
            "INSERT OR IGNORE INTO quota_state (date, updated_at) VALUES (?, ?);",
            (day, now),
        )

    def get_quota_state(self, target_date: Optional[str] = None) -> Dict[str, Any]:
        day = target_date or date.today().isoformat()
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            self._ensure_quota_row(cursor, day, now)
            conn.commit()
            cursor.execute("SELECT * FROM quota_state WHERE date = ?;", (day,))
            row = cursor.fetchone()
            return dict(row) if row else {}

    def grant_temporary_allowance(
        self, allowance_bytes: int, target_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """Grant extra bytes for the day. Works even before any traffic is recorded."""
        if allowance_bytes <= 0:
            raise ValueError("allowance_bytes must be positive")
        day = target_date or date.today().isoformat()
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            self._ensure_quota_row(cursor, day, now)
            cursor.execute("""
                UPDATE quota_state
                SET allowance_bytes = allowance_bytes + ?,
                    exceeded = 0,
                    blocked_reason = NULL,
                    updated_at = ?
                WHERE date = ?;
            """, (allowance_bytes, now, day))
            conn.commit()
        return self.get_quota_state(day)

    def mark_warning_fired(self, level: int, target_date: Optional[str] = None) -> bool:
        """Record that a warning threshold fired. Returns True if newly recorded."""
        day = target_date or date.today().isoformat()
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            self._ensure_quota_row(cursor, day, now)
            cursor.execute("SELECT warnings_fired FROM quota_state WHERE date = ?;", (day,))
            row = cursor.fetchone()
            levels = [x for x in (row["warnings_fired"].split(",") if row and row["warnings_fired"] else []) if x]
            if str(level) in levels:
                return False
            levels.append(str(level))
            cursor.execute(
                "UPDATE quota_state SET warnings_fired = ?, updated_at = ? WHERE date = ?;",
                (",".join(levels), now, day),
            )
            conn.commit()
            return True

    def set_quota_exceeded(
        self, exceeded: bool, reason: Optional[str] = None, target_date: Optional[str] = None
    ) -> None:
        day = target_date or date.today().isoformat()
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            self._ensure_quota_row(cursor, day, now)
            cursor.execute(
                "UPDATE quota_state SET exceeded = ?, blocked_reason = ?, updated_at = ? WHERE date = ?;",
                (1 if exceeded else 0, reason, now, day),
            )
            conn.commit()

    def set_daily_limit(self, limit_bytes: int, target_date: Optional[str] = None) -> None:
        """Store the configured daily limit for the day (0 means unlimited)."""
        day = target_date or date.today().isoformat()
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            self._ensure_quota_row(cursor, day, now)
            cursor.execute(
                "UPDATE quota_state SET daily_limit_bytes = ?, updated_at = ? WHERE date = ?;",
                (max(0, int(limit_bytes)), now, day),
            )
            conn.commit()


    # --- metered totals (quota basis) ------------------------------------------

    def metered_total(self, target_date: Optional[str] = None) -> int:
        day = target_date or date.today().isoformat()
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT COALESCE(SUM(rx_bytes + tx_bytes), 0) AS total
                FROM network_daily_usage
                WHERE date = ? AND metered = 1;
            """, (day,))
            row = cur.fetchone()
            return int(row["total"]) if row else 0

    def metered_total_cycle(self, billing_cycle_day: int = 1,
                            target_date: Optional[str] = None) -> int:
        today = date.fromisoformat(target_date) if target_date else date.today()
        cycle_day = max(1, min(int(billing_cycle_day), 28))
        if today.day >= cycle_day:
            start = today.replace(day=cycle_day)
        else:
            start = (today.replace(day=1) - timedelta(days=1)).replace(day=cycle_day)
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT COALESCE(SUM(rx_bytes + tx_bytes), 0) AS total
                FROM network_daily_usage
                WHERE metered = 1 AND date >= ? AND date <= ?;
            """, (start.isoformat(), today.isoformat()))
            row = cur.fetchone()
            return int(row["total"]) if row else 0

    def get_monthly_usage(
        self, connection_uuid: Optional[str] = None, billing_cycle_day: int = 1
    ) -> Dict[str, Any]:
        """Metered usage for the current billing cycle (used for the monthly quota)."""
        today = date.today()
        cycle_day = max(1, min(int(billing_cycle_day), 28))
        if today.day >= cycle_day:
            start = today.replace(day=cycle_day)
        else:
            start = (today.replace(day=1) - timedelta(days=1)).replace(day=cycle_day)
        start_str, end_str = start.isoformat(), today.isoformat()

        sql = """
            SELECT COALESCE(SUM(rx_bytes), 0) AS total_rx,
                   COALESCE(SUM(tx_bytes), 0) AS total_tx,
                   COALESCE(SUM(rx_bytes + tx_bytes), 0) AS total_bytes
            FROM network_daily_usage
            WHERE metered = 1 AND date >= ? AND date <= ?
        """
        params: List[Any] = [start_str, end_str]
        if connection_uuid:
            sql += " AND connection_uuid = ?"
            params.append(connection_uuid)

        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(sql + ";", params)
            row = cur.fetchone()
            return {
                "start_date": start_str,
                "end_date": end_str,
                "connection_uuid": connection_uuid,
                "rx_bytes": int(row["total_rx"]) if row else 0,
                "tx_bytes": int(row["total_tx"]) if row else 0,
                "total_bytes": int(row["total_bytes"]) if row else 0,
            }

    # --- history / chart data --------------------------------------------------

    def get_all_networks_history(self, days: int = 60) -> Dict[str, List[Dict[str, Any]]]:
        """Per-network daily history for the last ``days`` days (inclusive of today)."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT date, connection_uuid, connection_name,
                       rx_bytes, tx_bytes, (rx_bytes + tx_bytes) AS total_bytes, metered
                FROM network_daily_usage
                WHERE date > date('now', ? || ' days')
                ORDER BY date ASC;
            """, (f"-{days}",))
            rows = [dict(r) for r in cur.fetchall()]

        result: Dict[str, List[Dict[str, Any]]] = {}
        combined: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            result.setdefault(r["connection_uuid"], []).append(r)
            d = r["date"]
            combined.setdefault(d, {"date": d, "rx_bytes": 0, "tx_bytes": 0, "total_bytes": 0})
            combined[d]["rx_bytes"] += r["rx_bytes"]
            combined[d]["tx_bytes"] += r["tx_bytes"]
            combined[d]["total_bytes"] += r["total_bytes"]
        result["__all__"] = sorted(combined.values(), key=lambda x: x["date"])
        return result

    def get_known_networks(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT connection_uuid, connection_name,
                       SUM(rx_bytes) AS total_rx, SUM(tx_bytes) AS total_tx,
                       MAX(date) AS last_seen, MAX(metered) AS metered
                FROM network_daily_usage
                GROUP BY connection_uuid
                ORDER BY last_seen DESC;
            """)
            return [dict(r) for r in cur.fetchall()]

    # --- notifications ---------------------------------------------------------

    def add_notification(self, title: str, message: str, urgency: str = "normal") -> int:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO notifications (ts, title, message, urgency) VALUES (?, ?, ?, ?);",
                (datetime.now().isoformat(), title, message, urgency),
            )
            conn.commit()
            return cur.lastrowid or 0

    def latest_notification_id(self) -> int:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COALESCE(MAX(id), 0) AS m FROM notifications;")
            row = cur.fetchone()
            return int(row["m"]) if row else 0

    def get_notifications_since(self, since_id: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM notifications WHERE id > ? ORDER BY id ASC LIMIT ?;",
                (since_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    def prune_notifications(self, keep_hours: int = 24) -> int:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM notifications WHERE ts < datetime('now', ?);",
                (f"-{keep_hours} hours",),
            )
            conn.commit()
            return cur.rowcount or 0

    # --- maintenance -----------------------------------------------------------

    def prune_history(self, retention_days: int = 90) -> int:
        """Drop statistics older than the retention window."""
        if retention_days <= 0:
            return 0
        cutoff = (date.today() - timedelta(days=retention_days)).isoformat()
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM network_daily_usage WHERE date < ?;", (cutoff,))
            removed = cur.rowcount or 0
            cur.execute("DELETE FROM daily_usage WHERE date < ?;", (cutoff,))
            cur.execute("DELETE FROM quota_state WHERE date < ?;", (cutoff,))
            cur.execute("DELETE FROM sessions WHERE ended_at IS NOT NULL AND date(started_at) < ?;", (cutoff,))
            conn.commit()
            return removed

    def checkpoint(self) -> None:
        with self._get_connection() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")

    # --- approval events -------------------------------------------------------

    def record_approval_event(
        self, tool: str, packages: str, estimated_bytes: int, decision: str
    ) -> int:
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO approval_events (timestamp, tool, packages, estimated_bytes, decision, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?);
            """, (now, tool, packages, estimated_bytes, decision, now))
            conn.commit()
            return cur.lastrowid or 0

    def get_recent_approvals(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM approval_events ORDER BY id DESC LIMIT ?;", (limit,))
            return [dict(row) for row in cur.fetchall()]
