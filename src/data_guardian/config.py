"""Configuration management for Linux Data Guardian."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List

import yaml

log = logging.getLogger(__name__)


def _xdg_dir(env_var: str, fallback: Path) -> Path:
    raw = os.environ.get(env_var)
    if raw:
        return Path(raw).expanduser()
    return fallback


def default_config_dir() -> Path:
    return _xdg_dir("XDG_CONFIG_HOME", Path.home() / ".config") / "linux-data-guardian"


def default_data_dir() -> Path:
    return _xdg_dir("XDG_DATA_HOME", Path.home() / ".local" / "share") / "linux-data-guardian"


# Kept for backwards compatibility; prefer the functions above in new code.
DEFAULT_CONFIG_DIR = default_config_dir()
DEFAULT_DATA_DIR = default_data_dir()
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.yaml"
DEFAULT_WHITELIST_FILE = DEFAULT_CONFIG_DIR / "whitelist.yaml"

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BUILTIN_CONFIG_FILE = PROJECT_ROOT / "config" / "config.default.yaml"
BUILTIN_WHITELIST_FILE = PROJECT_ROOT / "config" / "whitelist.yaml"


def _int_config(value: Any, default: int, minimum: int | None = None,
                maximum: int | None = None) -> int:
    """Best-effort integer config parsing; a broken value never crashes a poll."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def ensure_directories() -> None:
    """Ensure runtime configuration and data storage directories exist (0700)."""
    for directory in (default_config_dir(), default_data_dir()):
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass


def _secure_write(path: Path, text: str) -> None:
    """Write a file with 0600 permissions atomically."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class ConfigManager:
    """Loads, validates, and manages Linux Data Guardian configuration."""

    def __init__(self, config_path: Path | None = None, whitelist_path: Path | None = None) -> None:
        self.config_path = Path(config_path) if config_path else default_config_dir() / "config.yaml"
        self.whitelist_path = Path(whitelist_path) if whitelist_path else default_config_dir() / "whitelist.yaml"
        ensure_directories()
        self.config: Dict[str, Any] = self._load_config()
        self.whitelist: Dict[str, Any] = self._load_whitelist()

    def _load_config(self) -> Dict[str, Any]:
        base_config: Dict[str, Any] = {}
        if BUILTIN_CONFIG_FILE.exists():
            with open(BUILTIN_CONFIG_FILE, "r", encoding="utf-8") as f:
                base_config = yaml.safe_load(f) or {}

        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    user_config = yaml.safe_load(f) or {}
                self._deep_merge(base_config, user_config)
                os.chmod(self.config_path, 0o600)
            except Exception as exc:  # noqa: BLE001 - config must never crash startup
                log.warning("Failed to parse user config at %s: %s", self.config_path, exc)

        return base_config

    def _load_whitelist(self) -> Dict[str, Any]:
        base_whitelist: Dict[str, Any] = {}
        if BUILTIN_WHITELIST_FILE.exists():
            with open(BUILTIN_WHITELIST_FILE, "r", encoding="utf-8") as f:
                base_whitelist = yaml.safe_load(f) or {}

        if self.whitelist_path.exists():
            try:
                with open(self.whitelist_path, "r", encoding="utf-8") as f:
                    user_whitelist = yaml.safe_load(f) or {}
                self._deep_merge(base_whitelist, user_whitelist)
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to parse user whitelist at %s: %s", self.whitelist_path, exc)

        return base_whitelist

    def _deep_merge(self, base: Dict[str, Any], override: Dict[str, Any]) -> None:
        for key, val in override.items():
            if isinstance(val, dict) and key in base and isinstance(base[key], dict):
                self._deep_merge(base[key], val)
            else:
                base[key] = val

    def save_user_config(self) -> None:
        ensure_directories()
        _secure_write(self.config_path, yaml.safe_dump(self.config, default_flow_style=False))

    # --- hotspot -------------------------------------------------------------

    @property
    def designated_hotspot_uuid(self) -> str:
        return str(self.config.get("hotspot", {}).get("designated_uuid", "") or "")

    @designated_hotspot_uuid.setter
    def designated_hotspot_uuid(self, value: str) -> None:
        self.config.setdefault("hotspot", {})["designated_uuid"] = value
        self.save_user_config()

    @property
    def designated_hotspot_name(self) -> str:
        return str(self.config.get("hotspot", {}).get("designated_name", "") or "")

    @designated_hotspot_name.setter
    def designated_hotspot_name(self, value: str) -> None:
        self.config.setdefault("hotspot", {})["designated_name"] = value
        self.save_user_config()

    def set_hotspot(self, uuid: str, name: str = "") -> None:
        """Set the designated hotspot UUID and (optionally) its display name in one write."""
        hotspot = self.config.setdefault("hotspot", {})
        hotspot["designated_uuid"] = uuid
        if name:
            hotspot["designated_name"] = name
            self.config.setdefault("network_names", {})[uuid] = name
        self.save_user_config()

    # --- profiles -------------------------------------------------------------

    @property
    def active_profile(self) -> str:
        profile = str(self.config.get("app", {}).get("default_profile", "balanced"))
        return profile if profile in ("balanced", "strict", "off") else "balanced"

    @active_profile.setter
    def active_profile(self, profile: str) -> None:
        self._validate_profile(profile)
        self.config.setdefault("app", {})["default_profile"] = profile
        self.save_user_config()

    @staticmethod
    def _validate_profile(profile: str) -> None:
        if profile not in ("balanced", "strict", "off"):
            raise ValueError(f"Invalid profile: {profile!r}. Must be 'balanced', 'strict', or 'off'.")

    def get_network_profile(self, connection_uuid: str) -> str:
        """Saved profile for a network UUID, falling back to the global default."""
        profiles = self.config.get("network_profiles", {})
        return str(profiles.get(connection_uuid, self.active_profile))

    def set_network_profile(self, connection_uuid: str, profile: str) -> None:
        self._validate_profile(profile)
        self.config.setdefault("network_profiles", {})[connection_uuid] = profile
        self.save_user_config()

    def get_network_name(self, connection_uuid: str) -> str:
        names = self.config.get("network_names", {})
        return str(names.get(connection_uuid, connection_uuid[:8] + "..."))

    # --- quotas ---------------------------------------------------------------

    @property
    def daily_limit_mb(self) -> int:
        return _int_config(
            self.config.get("quotas", {}).get("daily_limit_mb", 1000), 1000, minimum=0
        )

    @property
    def monthly_limit_mb(self) -> int:
        return _int_config(
            self.config.get("quotas", {}).get("monthly_limit_mb", 25000), 25000, minimum=0
        )

    @property
    def billing_cycle_day(self) -> int:
        return _int_config(
            self.config.get("quotas", {}).get("billing_cycle_day", 1), 1,
            minimum=1, maximum=28,
        )

    @property
    def warning_thresholds(self) -> List[int]:
        """Warn-once-per-day thresholds, e.g. [80, 95]."""
        raw = self.config.get("quotas", {}).get("warning_thresholds")
        if isinstance(raw, list) and raw:
            values = set()
            for item in raw:
                value = _int_config(item, 0)
                if 0 < value < 100:
                    values.add(value)
            if values:
                return sorted(values)
        # Backwards compatibility with the single-value key.
        single = _int_config(
            self.config.get("quotas", {}).get("warning_threshold_percent", 80), 80,
            minimum=1, maximum=99,
        )
        return [single]

    @property
    def action_on_exhaustion(self) -> str:
        return str(self.config.get("quotas", {}).get("action_on_exhaustion", "block_unapproved"))

    @property
    def enforce_quota(self) -> bool:
        """Whether exceeding the quota should actually block traffic."""
        return self.action_on_exhaustion in ("block_unapproved", "block_all")

    @property
    def max_allowance_mb(self) -> int:
        return _int_config(
            self.config.get("quotas", {}).get("max_allowance_mb", 10000), 10000, minimum=1
        )

    # --- misc -----------------------------------------------------------------

    @property
    def poll_interval_seconds(self) -> int:
        return _int_config(
            self.config.get("app", {}).get("poll_interval_seconds", 5), 5, minimum=1
        )

    @property
    def history_retention_days(self) -> int:
        return _int_config(
            self.config.get("storage", {}).get("history_retention_days", 90), 90, minimum=0
        )

    @property
    def app_enabled(self) -> bool:
        return bool(self.config.get("app", {}).get("enabled", True))

    @property
    def strict_allowed_ports_tcp(self) -> List[int]:
        """Extra TCP ports permitted in strict mode (from the whitelist config)."""
        raw = self.whitelist.get("strict_allowed_ports_tcp")
        if isinstance(raw, list) and raw:
            ports = set()
            for port in raw:
                try:
                    value = int(port)
                except (TypeError, ValueError):
                    continue
                if 1 <= value <= 65535:
                    ports.add(value)
            if ports:
                return sorted(ports)
        return [80, 443, 22]

    @property
    def strict_whitelist_processes(self) -> List[str]:
        """Process names allowed to open new connections in strict mode."""
        raw = self.whitelist.get("strict_whitelist_processes")
        if isinstance(raw, list) and raw:
            return [str(p) for p in raw]
        return ["opencode", "antigravity"]

    @property
    def db_path(self) -> Path:
        raw = self.config.get("storage", {}).get("db_path", "")
        if raw:
            return Path(os.path.expanduser(raw))
        return default_data_dir() / "guardian.db"
