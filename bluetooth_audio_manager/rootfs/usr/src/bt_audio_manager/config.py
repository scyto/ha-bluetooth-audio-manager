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
    keep_alive_enabled: bool = False
    keep_alive_method: str = "infrasound"
    scan_duration_seconds: int = 15

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
                keep_alive_enabled=data.get("keep_alive_enabled", False),
                keep_alive_method=data.get("keep_alive_method", "infrasound"),
                scan_duration_seconds=data.get("scan_duration_seconds", 15),
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.error("Failed to parse options: %s, using defaults", e)
            return cls()
