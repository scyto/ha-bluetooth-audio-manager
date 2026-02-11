"""Configuration loader for the Bluetooth Audio Manager add-on.

Reads user options from /data/options.json which is injected by
the HA Supervisor based on the schema defined in config.yaml.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

OPTIONS_PATH = "/data/options.json"


@dataclass
class AppConfig:
    """Application configuration loaded from HA add-on options."""

    log_level: str = "info"
    auto_reconnect: bool = True
    reconnect_interval_seconds: int = 30
    reconnect_max_backoff_seconds: int = 300
    scan_duration_seconds: int = 15
    bt_adapter: str = "auto"
    block_hfp: bool = True

    @property
    def adapter_path(self) -> str:
        """Resolve the bt_adapter setting to a BlueZ D-Bus adapter path.

        "auto" → "/org/bluez/hci0" (default first adapter).
        "hci1" → "/org/bluez/hci1", etc.
        """
        if self.bt_adapter == "auto":
            return "/org/bluez/hci0"
        name = self.bt_adapter
        if name.startswith("/org/bluez/"):
            return name
        return f"/org/bluez/{name}"

    @classmethod
    def load(cls) -> "AppConfig":
        """Load configuration from the HA options file."""
        path = Path(OPTIONS_PATH)
        if not path.exists():
            logger.warning("Options file not found at %s, using defaults", OPTIONS_PATH)
            return cls()

        try:
            data = json.loads(path.read_text())
            return cls(
                log_level=data.get("log_level", "info"),
                auto_reconnect=data.get("auto_reconnect", True),
                reconnect_interval_seconds=data.get("reconnect_interval_seconds", 30),
                reconnect_max_backoff_seconds=data.get("reconnect_max_backoff_seconds", 300),
                scan_duration_seconds=data.get("scan_duration_seconds", 15),
                bt_adapter=data.get("bt_adapter", "auto"),
                block_hfp=data.get("block_hfp", True),
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.error("Failed to parse options: %s, using defaults", e)
            return cls()
