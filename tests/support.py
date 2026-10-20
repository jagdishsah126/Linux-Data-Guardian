"""Shared test fixtures: every test is isolated from the real user config/DB."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from src.data_guardian.config import ConfigManager
from src.data_guardian.database import Database

MB = 1024 * 1024


class IsolatedTestCase(unittest.TestCase):
    """Provides a temp config + DB and never touches ~/.config or ~/.local/share."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.tmp_root = root
        self.config_path = root / "config.yaml"
        self.whitelist_path = root / "whitelist.yaml"
        self.db_path = root / "guardian.db"

        self.config = ConfigManager(self.config_path, self.whitelist_path)
        self.config.config.setdefault("storage", {})["db_path"] = str(self.db_path)
        self.config.config.setdefault("quotas", {})["daily_limit_mb"] = 1000
        self.config.config["quotas"]["monthly_limit_mb"] = 25000
        self.config.save_user_config()

        self.db = Database(self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # helpers -----------------------------------------------------------------

    def record(self, mb: float, *, metered: bool, uuid: str = "hot-uuid",
               name: str = "Hotspot", limit_mb: int = 1000) -> dict:
        return self.db.record_usage_delta(
            delta_rx=int(mb * MB),
            delta_tx=0,
            metered=metered,
            daily_limit_bytes=limit_mb * MB,
            connection_uuid=uuid,
            connection_name=name,
        )


def env_without_display() -> dict:
    env = dict(os.environ)
    env.pop("DISPLAY", None)
    env.pop("XAUTHORITY", None)
    return env
