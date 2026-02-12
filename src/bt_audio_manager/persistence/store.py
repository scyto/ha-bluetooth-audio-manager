"""JSON-backed persistent store for paired device information.

Data is stored in /data/paired_devices.json which persists across
container restarts, app updates, and is included in HA backups.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_STORE_PATH = "/data/paired_devices.json"

# Per-device settings with their default values.
# Existing device records without these keys get defaults automatically.
DEFAULT_DEVICE_SETTINGS = {
    "keep_alive_enabled": False,
    "keep_alive_method": "infrasound",
}


class PersistenceStore:
    """Manages persistent storage of paired Bluetooth audio devices."""

    def __init__(self, path: str = DEFAULT_STORE_PATH):
        self._path = Path(path)
        self._devices: list[dict] = []

    async def load(self) -> None:
        """Load paired devices from disk."""
        if not self._path.exists():
            self._devices = []
            logger.info("No existing paired devices store found")
            return

        try:
            data = json.loads(self._path.read_text())
            self._devices = data.get("devices", [])
            logger.info("Loaded %d paired device(s) from store", len(self._devices))
        except (json.JSONDecodeError, KeyError) as e:
            logger.error("Failed to parse paired devices store: %s", e)
            self._devices = []

    async def save(self) -> None:
        """Write current device list to disk."""
        data = {"devices": self._devices}
        self._path.write_text(json.dumps(data, indent=2))
        logger.debug("Saved %d device(s) to store", len(self._devices))

    async def add_device(
        self, address: str, name: str, auto_connect: bool = True
    ) -> None:
        """Add or update a paired device."""
        existing = self._find_device(address)
        if existing is not None:
            existing["name"] = name
            existing["auto_connect"] = auto_connect
        else:
            self._devices.append(
                {
                    "address": address,
                    "name": name,
                    "auto_connect": auto_connect,
                    "paired_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        await self.save()
        logger.info("Stored device %s (%s)", address, name)

    async def remove_device(self, address: str) -> None:
        """Remove a device from the store."""
        self._devices = [d for d in self._devices if d["address"] != address]
        await self.save()
        logger.info("Removed device %s from store", address)

    def get_device(self, address: str) -> dict | None:
        """Get a device by address."""
        return self._find_device(address)

    def _find_device(self, address: str) -> dict | None:
        for d in self._devices:
            if d["address"] == address:
                return d
        return None

    async def update_device_settings(self, address: str, settings: dict) -> dict | None:
        """Update settings fields on a device record. Returns updated device or None."""
        device = self._find_device(address)
        if device is None:
            return None
        for key in DEFAULT_DEVICE_SETTINGS:
            if key in settings:
                device[key] = settings[key]
        await self.save()
        logger.info("Updated settings for %s: %s", address,
                     {k: device.get(k) for k in DEFAULT_DEVICE_SETTINGS})
        return device

    def get_device_settings(self, address: str) -> dict:
        """Get settings for a device, filling in defaults for missing keys."""
        device = self._find_device(address)
        if device is None:
            return dict(DEFAULT_DEVICE_SETTINGS)
        return {k: device.get(k, v) for k, v in DEFAULT_DEVICE_SETTINGS.items()}

    @property
    def devices(self) -> list[dict]:
        """All stored devices."""
        return list(self._devices)

    @property
    def auto_connect_devices(self) -> list[dict]:
        """Devices marked for auto-connect."""
        return [d for d in self._devices if d.get("auto_connect", True)]
