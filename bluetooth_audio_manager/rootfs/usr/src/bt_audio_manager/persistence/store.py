"""JSON-backed persistent store for paired device information.

Data is stored in /data/paired_devices.json which persists across
container restarts, add-on updates, and is included in HA backups.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_STORE_PATH = "/data/paired_devices.json"


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

    @property
    def devices(self) -> list[dict]:
        """All stored devices."""
        return list(self._devices)

    @property
    def auto_connect_devices(self) -> list[dict]:
        """Devices marked for auto-connect."""
        return [d for d in self._devices if d.get("auto_connect", True)]
