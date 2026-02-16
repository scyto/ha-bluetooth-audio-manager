"""Auto-reconnection service with exponential backoff.

Monitors D-Bus PropertiesChanged signals for Connected=false events
and attempts to reconnect paired devices automatically.
"""

import asyncio
import logging
import random
from typing import TYPE_CHECKING

from dbus_next.errors import DBusError

if TYPE_CHECKING:
    from .manager import BluetoothAudioManager

logger = logging.getLogger(__name__)


class ReconnectService:
    """Manages automatic reconnection of disconnected Bluetooth audio devices."""

    QUICK_RETRY_DELAY = 10  # seconds — fast first attempt for transient glitches

    def __init__(self, manager: "BluetoothAudioManager"):
        self._manager = manager
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = False

    async def start(self) -> None:
        """Start monitoring for disconnections."""
        self._running = True
        logger.info("Reconnect service started")

    async def stop(self) -> None:
        """Stop all reconnection attempts."""
        self._running = False
        for address, task in self._tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._tasks.clear()
        logger.info("Reconnect service stopped")

    def handle_disconnect(self, address: str) -> None:
        """Called when a device disconnects. Schedules reconnection."""
        if not self._running:
            return
        if not self._manager.config.auto_reconnect:
            return

        # Check if device is in our persistent store with auto_connect
        device_info = self._manager.store.get_device(address)
        if not device_info or not device_info.get("auto_connect", True):
            logger.debug("Skipping reconnect for %s (not auto-connect)", address)
            return

        if address in self._tasks and not self._tasks[address].done():
            logger.debug("Already reconnecting to %s", address)
            return

        self._tasks[address] = asyncio.create_task(self._reconnect_loop(address))

    def cancel_reconnect(self, address: str) -> None:
        """Cancel any pending reconnection for a device."""
        task = self._tasks.pop(address, None)
        if task and not task.done():
            task.cancel()

    async def reconnect_all(self) -> None:
        """Attempt to reconnect all auto-connect devices (called on startup)."""
        devices = self._manager.store.auto_connect_devices
        if not devices:
            return

        logger.info("Attempting to reconnect %d stored device(s)...", len(devices))
        for device_info in devices:
            address = device_info["address"]
            self._tasks[address] = asyncio.create_task(
                self._reconnect_loop(address)
            )

    async def _reconnect_loop(self, address: str) -> None:
        """Attempt reconnection with exponential backoff and jitter."""
        # Check if already connected (e.g. device persisted across app restart)
        device = self._manager.managed_devices.get(address)
        if device:
            try:
                if await device.is_connected():
                    logger.info("Device %s already connected, skipping reconnect", address)
                    self._tasks.pop(address, None)
                    return
            except Exception as e:
                logger.debug("is_connected check failed for %s: %s", address, e)

        # Quick first attempt — handles transient glitches (e.g. AVRCP bugs)
        logger.info(
            "Quick reconnect to %s in %ds...", address, self.QUICK_RETRY_DELAY
        )
        self._manager._broadcast_status(
            f"Quick reconnect to {address} in {self.QUICK_RETRY_DELAY}s..."
        )
        await asyncio.sleep(self.QUICK_RETRY_DELAY)

        if not self._running:
            return

        try:
            success = await self._manager.connect_device(address)
            if success:
                logger.info("Quick reconnect to %s succeeded", address)
                self._manager._broadcast_status(f"Reconnected to {address}")
                self._tasks.pop(address, None)
                return
        except (DBusError, asyncio.TimeoutError, OSError) as e:
            logger.info(
                "Quick reconnect to %s failed: %s — falling back to backoff",
                address, e,
            )

        interval = self._manager.config.reconnect_interval_seconds
        max_backoff = self._manager.config.reconnect_max_backoff_seconds
        attempt = 0

        while self._running:
            wait = min(interval * (2 ** attempt), max_backoff)
            jitter = random.uniform(0, wait * 0.1)
            total_wait = wait + jitter

            logger.debug(
                "Reconnect to %s: attempt %d in %.1fs",
                address, attempt + 1, total_wait,
            )
            self._manager._broadcast_status(
                f"Reconnecting to {address} in {int(total_wait)}s "
                f"(attempt {attempt + 1})..."
            )
            await asyncio.sleep(total_wait)

            if not self._running:
                return

            try:
                success = await self._manager.connect_device(address)
                if success:
                    logger.info(
                        "Reconnected to %s after %d attempt(s)", address, attempt + 1
                    )
                    self._manager._broadcast_status(
                        f"Reconnected to {address}"
                    )
                    self._tasks.pop(address, None)
                    return
            except (DBusError, asyncio.TimeoutError, OSError) as e:
                logger.warning(
                    "Reconnect attempt %d for %s failed: %s",
                    attempt + 1, address, e,
                )
                self._manager._broadcast_status(
                    f"Reconnect attempt {attempt + 1} for {address} failed"
                )

            attempt += 1
