"""Configuration management for Linux Data Guardian."""

from __future__ import annotations
import os
from pathlib import Path
from typing import Any, Dict
import yaml

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "linux-data-guardian"
DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "linux-data-guardian"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.yaml"
DEFAULT_WHITELIST_FILE = DEFAULT_CONFIG_DIR / "whitelist.yaml"

# Project root fallback configuration
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BUILTIN_CONFIG_FILE = PROJECT_ROOT / "config" / "config.default.yaml"
BUILTIN_WHITELIST_FILE = PROJECT_ROOT / "config" / "whitelist.yaml"


def ensure_directories() -> None:
    """Ensure runtime configuration and data storage directories exist."""
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)


class ConfigManager:
    """Loads, validates, and manages Linux Data Guardian configuration."""

    def __init__(self, config_path: Path | None = None, whitelist_path: Path | None = None) -> None:
        self.config_path = config_path or DEFAULT_CONFIG_FILE
        self.whitelist_path = whitelist_path or DEFAULT_WHITELIST_FILE
        ensure_directories()
        self.config: Dict[str, Any] = self._load_config()
        self.whitelist: Dict[str, Any] = self._load_whitelist()

    def _load_config(self) -> Dict[str, Any]:
        """Load user config or fallback to built-in template."""
        base_config: Dict[str, Any] = {}
        if BUILTIN_CONFIG_FILE.exists():
            with open(BUILTIN_CONFIG_FILE, "r", encoding="utf-8") as f:
                base_config = yaml.safe_load(f) or {}

        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    user_config = yaml.safe_load(f) or {}
                # Deep merge user config over base config
                self._deep_merge(base_config, user_config)
            except Exception as e:
                print(f"[!] Warning: Failed to parse user config at {self.config_path}: {e}")

        return base_config

    def _load_whitelist(self) -> Dict[str, Any]:
        """Load whitelist config or fallback to built-in template."""
        base_whitelist: Dict[str, Any] = {}
        if BUILTIN_WHITELIST_FILE.exists():
            with open(BUILTIN_WHITELIST_FILE, "r", encoding="utf-8") as f:
                base_whitelist = yaml.safe_load(f) or {}

        if self.whitelist_path.exists():
            try:
                with open(self.whitelist_path, "r", encoding="utf-8") as f:
                    user_whitelist = yaml.safe_load(f) or {}
                self._deep_merge(base_whitelist, user_whitelist)
            except Exception as e:
                print(f"[!] Warning: Failed to parse user whitelist at {self.whitelist_path}: {e}")

        return base_whitelist

    def _deep_merge(self, base: Dict[str, Any], override: Dict[str, Any]) -> None:
        """Recursively merges override dictionary into base dictionary."""
        for key, val in override.items():
            if isinstance(val, dict) and key in base and isinstance(base[key], dict):
                self._deep_merge(base[key], val)
            else:
                base[key] = val

    def save_user_config(self) -> None:
        """Persist current configuration to user's config file."""
        ensure_directories()
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.config, f, default_flow_style=False)

    @property
    def designated_hotspot_uuid(self) -> str:
        return str(self.config.get("hotspot", {}).get("designated_uuid", "") or "")

    @designated_hotspot_uuid.setter
    def designated_hotspot_uuid(self, value: str) -> None:
        if "hotspot" not in self.config:
            self.config["hotspot"] = {}
        self.config["hotspot"]["designated_uuid"] = value
        self.save_user_config()

    @property
    def active_profile(self) -> str:
        return str(self.config.get("app", {}).get("default_profile", "balanced"))

    @active_profile.setter
    def active_profile(self, profile: str) -> None:
        if profile not in ("balanced", "strict", "off"):
            raise ValueError(f"Invalid profile: {profile}. Must be 'balanced', 'strict', or 'off'.")
        if "app" not in self.config:
            self.config["app"] = {}
        self.config["app"]["default_profile"] = profile
        self.save_user_config()

    def get_network_profile(self, connection_uuid: str) -> str:
        """Get the saved profile mode for a specific network UUID.
        Defaults to 'balanced' for hotspot, 'off' for regular networks if not configured.
        """
        profiles = self.config.get("network_profiles", {})
        return str(profiles.get(connection_uuid, "balanced"))

    def set_network_profile(self, connection_uuid: str, profile: str) -> None:
        """Persist a profile mode for a specific network UUID."""
        if profile not in ("balanced", "strict", "off"):
            raise ValueError(f"Invalid profile: {profile}")
        if "network_profiles" not in self.config:
            self.config["network_profiles"] = {}
        self.config["network_profiles"][connection_uuid] = profile
        self.save_user_config()

    def get_network_name(self, connection_uuid: str) -> str:
        """Return the saved display name for a network UUID if we have it."""
        names = self.config.get("network_names", {})
        return str(names.get(connection_uuid, connection_uuid[:8] + "..."))

    @property
    def daily_limit_mb(self) -> int:
        return int(self.config.get("quotas", {}).get("daily_limit_mb", 1000))

    @property
    def monthly_limit_mb(self) -> int:
        return int(self.config.get("quotas", {}).get("monthly_limit_mb", 25000))

    @property
    def db_path(self) -> Path:
        raw = self.config.get("storage", {}).get("db_path", "~/.local/share/linux-data-guardian/guardian.db")
        return Path(os.path.expanduser(raw))
