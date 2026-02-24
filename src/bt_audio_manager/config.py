"""Configuration loader for the Bluetooth Audio Manager app.

Reads user options from /data/options.json which is injected by
the HA Supervisor based on the schema defined in config.yaml.

Runtime settings (auto_reconnect, reconnect intervals, scan duration,
bt_adapter) are stored in /config/settings.json (addon_config, survives
reinstall) and managed via the app's web UI.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

OPTIONS_PATH = "/data/options.json"
SETTINGS_PATH = "/config/settings.json"
_LEGACY_SETTINGS_PATH = "/data/settings.json"

# Keys that live in settings.json (managed via app UI)
_SETTINGS_KEYS = {
    "bt_adapter",
    "auto_reconnect",
    "reconnect_interval_seconds",
    "reconnect_max_backoff_seconds",
    "scan_duration_seconds",
}


@dataclass
class AppConfig:
    """Application configuration loaded from HA app options + settings."""

    # From options.json (HAOS config page — requires restart)
    log_level: str = "info"

    # From settings.json (app UI)
    bt_adapter: str = "auto"
    auto_reconnect: bool = True
    reconnect_interval_seconds: int = 30
    reconnect_max_backoff_seconds: int = 300
    scan_duration_seconds: int = 30

    @property
    def bt_adapter_is_mac(self) -> bool:
        """True when bt_adapter stores a MAC address (new format)."""
        return ":" in self.bt_adapter

    @property
    def bt_adapter_is_legacy_hci(self) -> bool:
        """True when bt_adapter stores a legacy HCI name like 'hci1'."""
        return self.bt_adapter != "auto" and ":" not in self.bt_adapter

    @property
    def runtime_settings(self) -> dict:
        """Return current runtime settings as a dict."""
        return {
            "auto_reconnect": self.auto_reconnect,
            "reconnect_interval_seconds": self.reconnect_interval_seconds,
            "reconnect_max_backoff_seconds": self.reconnect_max_backoff_seconds,
            "scan_duration_seconds": self.scan_duration_seconds,
        }

    def save_settings(self) -> None:
        """Write all settings (including bt_adapter) to /data/settings.json."""
        path = Path(SETTINGS_PATH)
        data = {
            "bt_adapter": self.bt_adapter,
            **self.runtime_settings,
        }
        path.write_text(json.dumps(data, indent=2))
        logger.info("Settings saved to %s", SETTINGS_PATH)

    @classmethod
    def load(cls) -> "AppConfig":
        """Load configuration from HA options + settings files."""
        config = cls()

        # 1. Load log_level from options.json (only setting left on HAOS page)
        opts_path = Path(OPTIONS_PATH)
        if opts_path.exists():
            try:
                data = json.loads(opts_path.read_text())
                config.log_level = data.get("log_level", "info")
            except (json.JSONDecodeError, KeyError) as e:
                logger.error("Failed to parse options: %s, using defaults", e)

        # 1b. Migrate settings from /data/ to /config/ (one-time)
        settings_path = Path(SETTINGS_PATH)
        legacy_settings = Path(_LEGACY_SETTINGS_PATH)
        if not settings_path.exists() and legacy_settings.exists():
            logger.info("Migrating settings from %s to %s", legacy_settings, settings_path)
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(legacy_settings.read_text())

        # 2. Load settings from settings.json
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text())
                config.bt_adapter = settings.get("bt_adapter", "auto")
                config.auto_reconnect = settings.get("auto_reconnect", True)
                config.reconnect_interval_seconds = settings.get("reconnect_interval_seconds", 30)
                config.reconnect_max_backoff_seconds = settings.get("reconnect_max_backoff_seconds", 300)
                config.scan_duration_seconds = settings.get("scan_duration_seconds", 30)
                logger.info("Loaded settings from %s", SETTINGS_PATH)
                return config
            except (json.JSONDecodeError, KeyError) as e:
                logger.error("Failed to parse settings: %s, trying migration", e)

        # 3. Migration: settings.json doesn't exist — check options.json
        #    for legacy keys (user upgrading from older version)
        if opts_path.exists():
            try:
                data = json.loads(opts_path.read_text())
                migrated = False
                for key in _SETTINGS_KEYS:
                    if key in data:
                        setattr(config, key, data[key])
                        migrated = True
                if migrated:
                    config.save_settings()
                    logger.info("Migrated settings from options.json → settings.json")
                    return config
            except (json.JSONDecodeError, KeyError):
                pass

        # 4. No settings found — save defaults so the file exists
        config.save_settings()
        return config
