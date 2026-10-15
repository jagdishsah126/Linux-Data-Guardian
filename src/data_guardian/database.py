"""SQLite database storage and queries for Linux Data Guardian."""

from __future__ import annotations
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional

CURRENT_SCHEMA_VERSION = 1


class Database:
    """Manages SQLite connection, schema migrations, and persistent usage records."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

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

            # Daily aggregated network usage
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

            # Indexes for quick dashboard lookups
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_uuid ON sessions(connection_uuid);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_approval_tool ON approval_events(tool);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_approval_time ON approval_events(timestamp);")

            # Check and record version
            cursor.execute("SELECT MAX(version) FROM schema_version;")
            row = cursor.fetchone()
            if not row or row[0] is None:
                cursor.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, datetime('now'));",
                    (CURRENT_SCHEMA_VERSION,)
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
        target_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """Add delta usage to the specified date (default today) and check quota status."""
        today_str = target_date or date.today().isoformat()
        now = datetime.now().isoformat()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Upsert daily row
            cursor.execute("""
                INSERT INTO daily_usage (date, rx_bytes, tx_bytes, quota_limit_bytes, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    rx_bytes = rx_bytes + excluded.rx_bytes,
                    tx_bytes = tx_bytes + excluded.tx_bytes,
                    quota_limit_bytes = CASE WHEN excluded.quota_limit_bytes > 0 THEN excluded.quota_limit_bytes ELSE daily_usage.quota_limit_bytes END,
                    updated_at = excluded.updated_at;
            """, (today_str, delta_rx, delta_tx, quota_limit_bytes, now))

            # Fetch updated row
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
        """Fetch usage for today or specified date."""
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
