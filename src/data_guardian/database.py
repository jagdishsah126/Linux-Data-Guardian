"""SQLite database storage and queries for Linux Data Guardian."""

from __future__ import annotations
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional

CURRENT_SCHEMA_VERSION = 2


class Database:
    """Manages SQLite connection, schema migrations, and persistent usage records."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._migrate()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def _init_db(self) -> None:
        """Create tables and indexes if they do not exist."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Schema versioning
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
            """)

            # Network connection sessions
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    connection_uuid TEXT,
                    connection_name TEXT,
                    interface_name TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    rx_bytes INTEGER DEFAULT 0,
                    tx_bytes INTEGER DEFAULT 0,
                    profile TEXT DEFAULT 'balanced'
                );
            """)

            # Daily aggregated network usage (combined all networks, legacy)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_usage (
                    date TEXT PRIMARY KEY,
                    rx_bytes INTEGER DEFAULT 0,
                    tx_bytes INTEGER DEFAULT 0,
                    quota_limit_bytes INTEGER DEFAULT 0,
                    warning_fired INTEGER DEFAULT 0,
                    quota_exceeded INTEGER DEFAULT 0,
                    temporary_allowance_bytes INTEGER DEFAULT 0,
                    updated_at TEXT
                );
            """)

            # Per-network daily usage (v2: keyed by date + connection_uuid)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS network_daily_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    connection_uuid TEXT NOT NULL,
                    connection_name TEXT NOT NULL,
                    rx_bytes INTEGER DEFAULT 0,
                    tx_bytes INTEGER DEFAULT 0,
                    updated_at TEXT,
                    UNIQUE(date, connection_uuid)
                );
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_net_usage_date
                ON network_daily_usage(date);
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_net_usage_uuid
                ON network_daily_usage(connection_uuid);
            """)

            # Package and dependency approval requests & audit log
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

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_uuid ON sessions(connection_uuid);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_time ON sessions(started_at);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_approval_time ON approval_events(timestamp);")

            # Check and record version
            cursor.execute("SELECT MAX(version) FROM schema_version;")
            row = cursor.fetchone()
            if not row or row[0] is None:
                cursor.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, datetime('now'));",
                    (1,)
                )
            conn.commit()

    def _migrate(self) -> None:
        """Apply schema migrations for version upgrades."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(version) FROM schema_version;")
            row = cursor.fetchone()
            current_version = row[0] if row and row[0] is not None else 1

            if current_version < 2:
                # v2: Migrate today's combined daily_usage into network_daily_usage
                # for ALHN-449C (the only network used so far, detected from sessions)
                cursor.execute("""
                    SELECT connection_uuid, connection_name,
                           SUM(rx_bytes) as rx, SUM(tx_bytes) as tx
                    FROM sessions
                    WHERE date(started_at) = date('now')
                    GROUP BY connection_uuid, connection_name;
                """)
                session_rows = cursor.fetchall()

                cursor.execute("SELECT * FROM daily_usage WHERE date = date('now');")
                today_row = cursor.fetchone()

                if today_row and session_rows:
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
                        """, (sr["connection_uuid"], sr["connection_name"],
                              sr["rx"], sr["tx"]))

                cursor.execute(
                    "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (2, datetime('now'));"
                )
                conn.commit()

    # --- Session Management ---

    def start_session(
        self,
        connection_uuid: str,
        connection_name: str,
        interface_name: str,
        profile: str = "balanced"
    ) -> int:
        """Records a new network session and returns its session ID."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO sessions (connection_uuid, connection_name, interface_name, started_at, profile)
                VALUES (?, ?, ?, ?, ?);
            """, (connection_uuid, connection_name, interface_name, now, profile))
            conn.commit()
            return cursor.lastrowid or 0

    def update_session(self, session_id: int, delta_rx: int, delta_tx: int) -> None:
        """Increment rx and tx bytes on an active session."""
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE sessions
                SET rx_bytes = rx_bytes + ?,
                    tx_bytes = tx_bytes + ?
                WHERE id = ?;
            """, (delta_rx, delta_tx, session_id))
            conn.commit()

    def end_session(self, session_id: int) -> None:
        """Mark a session as ended."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?;", (now, session_id))
            conn.commit()

    # --- Daily Usage & Quotas ---

    def record_usage_delta(
        self,
        delta_rx: int,
        delta_tx: int,
        quota_limit_bytes: int = 0,
        target_date: Optional[str] = None,
        connection_uuid: str = "",
        connection_name: str = ""
    ) -> Dict[str, Any]:
        """Add delta usage to the specified date (default today) and check quota status."""
        today_str = target_date or date.today().isoformat()
        now = datetime.now().isoformat()

        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Upsert combined daily row (legacy + still used for quota tracking)
            cursor.execute("""
                INSERT INTO daily_usage (date, rx_bytes, tx_bytes, quota_limit_bytes, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    rx_bytes = rx_bytes + excluded.rx_bytes,
                    tx_bytes = tx_bytes + excluded.tx_bytes,
                    quota_limit_bytes = CASE WHEN excluded.quota_limit_bytes > 0 THEN excluded.quota_limit_bytes ELSE daily_usage.quota_limit_bytes END,
                    updated_at = excluded.updated_at;
            """, (today_str, delta_rx, delta_tx, quota_limit_bytes, now))

            # Upsert per-network daily row
            if connection_uuid and connection_name:
                cursor.execute("""
                    INSERT INTO network_daily_usage
                        (date, connection_uuid, connection_name, rx_bytes, tx_bytes, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date, connection_uuid) DO UPDATE SET
                        rx_bytes = rx_bytes + excluded.rx_bytes,
                        tx_bytes = tx_bytes + excluded.tx_bytes,
                        connection_name = excluded.connection_name,
                        updated_at = excluded.updated_at;
                """, (today_str, connection_uuid, connection_name, delta_rx, delta_tx, now))

            # Fetch updated combined row
            cursor.execute("SELECT * FROM daily_usage WHERE date = ?;", (today_str,))
            row = cursor.fetchone()
            conn.commit()

            if not row:
                return {}

            total_bytes = row["rx_bytes"] + row["tx_bytes"]
            effective_limit = row["quota_limit_bytes"] + row["temporary_allowance_bytes"]
            exceeded = (effective_limit > 0) and (total_bytes >= effective_limit)

            if exceeded and not row["quota_exceeded"]:
                with self._get_connection() as conn2:
                    conn2.execute("UPDATE daily_usage SET quota_exceeded = 1 WHERE date = ?;", (today_str,))
                    conn2.commit()

            return {
                "date": row["date"],
                "rx_bytes": row["rx_bytes"],
                "tx_bytes": row["tx_bytes"],
                "total_bytes": total_bytes,
                "quota_limit_bytes": row["quota_limit_bytes"],
                "temporary_allowance_bytes": row["temporary_allowance_bytes"],
                "effective_limit_bytes": effective_limit,
                "quota_exceeded": bool(exceeded),
                "warning_fired": bool(row["warning_fired"]),
            }

    def get_today_usage(self, target_date: Optional[str] = None) -> Dict[str, Any]:
        """Fetch usage for today or specified date (combined all networks)."""
        today_str = target_date or date.today().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM daily_usage WHERE date = ?;", (today_str,))
            row = cursor.fetchone()
            if not row:
                return {
                    "date": today_str,
                    "rx_bytes": 0,
                    "tx_bytes": 0,
                    "total_bytes": 0,
                    "quota_limit_bytes": 0,
                    "temporary_allowance_bytes": 0,
                    "effective_limit_bytes": 0,
                    "quota_exceeded": False,
                    "warning_fired": False,
                }
            total = row["rx_bytes"] + row["tx_bytes"]
            effective_limit = row["quota_limit_bytes"] + row["temporary_allowance_bytes"]
            return {
                "date": row["date"],
                "rx_bytes": row["rx_bytes"],
                "tx_bytes": row["tx_bytes"],
                "total_bytes": total,
                "quota_limit_bytes": row["quota_limit_bytes"],
                "temporary_allowance_bytes": row["temporary_allowance_bytes"],
                "effective_limit_bytes": effective_limit,
                "quota_exceeded": bool(row["quota_exceeded"]),
                "warning_fired": bool(row["warning_fired"]),
            }

    def get_network_today_usage(
        self, connection_uuid: str, target_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """Fetch today's usage for a specific network UUID."""
        today_str = target_date or date.today().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM network_daily_usage WHERE date = ? AND connection_uuid = ?;",
                (today_str, connection_uuid)
            )
            row = cursor.fetchone()
            if not row:
                return {"date": today_str, "rx_bytes": 0, "tx_bytes": 0, "total_bytes": 0}
            total = row["rx_bytes"] + row["tx_bytes"]
            return {
                "date": row["date"],
                "connection_uuid": row["connection_uuid"],
                "connection_name": row["connection_name"],
                "rx_bytes": row["rx_bytes"],
                "tx_bytes": row["tx_bytes"],
                "total_bytes": total,
            }

    def get_network_history(
        self, connection_uuid: str, days: int = 60
    ) -> List[Dict[str, Any]]:
        """Fetch per-network usage for the last N days, one row per day."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT date, connection_name,
                       rx_bytes, tx_bytes, (rx_bytes + tx_bytes) AS total_bytes
                FROM network_daily_usage
                WHERE connection_uuid = ?
                  AND date >= date('now', ? || ' days')
                ORDER BY date ASC;
            """, (connection_uuid, f"-{days}"))
            return [dict(r) for r in cursor.fetchall()]

    def get_all_networks_history(self, days: int = 60) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch per-network usage history for all known networks for the last N days.
        Returns dict keyed by connection_uuid → list of {date, rx_bytes, tx_bytes, total_bytes}.
        Also returns a special key '__all__' for combined totals per day.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT date, connection_uuid, connection_name,
                       rx_bytes, tx_bytes, (rx_bytes + tx_bytes) AS total_bytes
                FROM network_daily_usage
                WHERE date >= date('now', ? || ' days')
                ORDER BY date ASC;
            """, (f"-{days}",))
            rows = [dict(r) for r in cursor.fetchall()]

        result: Dict[str, List[Dict[str, Any]]] = {}
        combined: Dict[str, Dict[str, Any]] = {}

        for r in rows:
            uuid = r["connection_uuid"]
            if uuid not in result:
                result[uuid] = []
            result[uuid].append(r)

            d = r["date"]
            if d not in combined:
                combined[d] = {"date": d, "rx_bytes": 0, "tx_bytes": 0, "total_bytes": 0}
            combined[d]["rx_bytes"] += r["rx_bytes"]
            combined[d]["tx_bytes"] += r["tx_bytes"]
            combined[d]["total_bytes"] += r["total_bytes"]

        result["__all__"] = sorted(combined.values(), key=lambda x: x["date"])
        return result

    def get_known_networks(self) -> List[Dict[str, Any]]:
        """Return list of all networks we have ever tracked data for."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT connection_uuid,
                       connection_name,
                       SUM(rx_bytes) as total_rx,
                       SUM(tx_bytes) as total_tx,
                       MAX(date) as last_seen
                FROM network_daily_usage
                GROUP BY connection_uuid
                ORDER BY last_seen DESC;
            """)
            return [dict(r) for r in cursor.fetchall()]

    def grant_temporary_allowance(self, allowance_bytes: int, target_date: Optional[str] = None) -> None:
        """Grant temporary extra megabytes for today's quota."""
        today_str = target_date or date.today().isoformat()
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE daily_usage
                SET temporary_allowance_bytes = temporary_allowance_bytes + ?,
                    quota_exceeded = 0
                WHERE date = ?;
            """, (allowance_bytes, today_str))
            conn.commit()

    def set_warning_fired(self, target_date: Optional[str] = None) -> None:
        """Mark warning notification as sent for today to prevent repeated alerts."""
        today_str = target_date or date.today().isoformat()
        with self._get_connection() as conn:
            conn.execute("UPDATE daily_usage SET warning_fired = 1 WHERE date = ?;", (today_str,))
            conn.commit()

    # --- Approval Events Audit Log ---

    def record_approval_event(
        self,
        tool: str,
        packages: str,
        estimated_bytes: int,
        decision: str
    ) -> int:
        """Logs an approval or denial event."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO approval_events (timestamp, tool, packages, estimated_bytes, decision, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?);
            """, (now, tool, packages, estimated_bytes, decision, now))
            conn.commit()
            return cursor.lastrowid or 0

    def get_recent_approvals(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Retrieve recent approval and denial events."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM approval_events
                ORDER BY id DESC
                LIMIT ?;
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]
