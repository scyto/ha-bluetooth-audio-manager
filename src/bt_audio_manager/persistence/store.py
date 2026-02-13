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
    "idle_mode": "default",           # "default" | "power_save" | "keep_alive" | "auto_disconnect"
    "keep_alive_method": "infrasound",  # only used when idle_mode="keep_alive"
    "power_save_delay": 0,             # seconds before suspending (0-300)
    "auto_disconnect_minutes": 30,     # minutes before disconnect (5-60)
    "mpd_enabled": False,
    "mpd_port": None,   # Auto-assigned from pool (6600-6609); user can override
    "mpd_hw_volume": 100,  # Hardware volume % set when MPD starts (1-100)
    "avrcp_enabled": True,  # Auto-track PlaybackStatus; False = always Stopped
}

MPD_PORT_MIN = 6600
MPD_PORT_MAX = 6609


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
        settings = {k: device.get(k, v) for k, v in DEFAULT_DEVICE_SETTINGS.items()}
        # Migrate legacy keep_alive_enabled → idle_mode
        if "keep_alive_enabled" in device and "idle_mode" not in device:
            settings["idle_mode"] = "keep_alive" if device["keep_alive_enabled"] else "default"
        return settings

    # -- MPD port allocation --

    def _used_mpd_ports(self) -> dict[int, str]:
        """Return {port: address} for all devices with an assigned mpd_port."""
        result: dict[int, str] = {}
        for d in self._devices:
            port = d.get("mpd_port")
            if port is not None:
                result[port] = d["address"]
        return result

    async def allocate_mpd_port(self, address: str) -> int | None:
        """Assign the lowest available MPD port to a device.

        Returns the existing port if already assigned, or None if all 10 are taken.
        """
        device = self._find_device(address)
        if device is None:
            return None
        existing = device.get("mpd_port")
        if existing is not None:
            return existing
        used = set(self._used_mpd_ports().keys())
        for port in range(MPD_PORT_MIN, MPD_PORT_MAX + 1):
            if port not in used:
                device["mpd_port"] = port
                await self.save()
                logger.info("Allocated MPD port %d for %s", port, address)
                return port
        logger.warning("All MPD ports in use (6600-6609)")
        return None

    async def set_mpd_port(self, address: str, port: int) -> bool:
        """Set a specific MPD port for a device.

        Returns False if port is out of range or already used by another device.
        """
        if port < MPD_PORT_MIN or port > MPD_PORT_MAX:
            return False
        device = self._find_device(address)
        if device is None:
            return False
        used = self._used_mpd_ports()
        if port in used and used[port] != address:
            return False
        device["mpd_port"] = port
        await self.save()
        logger.info("Set MPD port %d for %s", port, address)
        return True

    async def release_mpd_port(self, address: str) -> None:
        """Release the MPD port assigned to a device."""
        device = self._find_device(address)
        if device is None:
            return
        old_port = device.get("mpd_port")
        device["mpd_port"] = None
        await self.save()
        if old_port is not None:
            logger.info("Released MPD port %d for %s", old_port, address)

    @property
    def devices(self) -> list[dict]:
        """All stored devices."""
        return list(self._devices)

    @property
    def auto_connect_devices(self) -> list[dict]:
        """Devices marked for auto-connect."""
        return [d for d in self._devices if d.get("auto_connect", True)]
