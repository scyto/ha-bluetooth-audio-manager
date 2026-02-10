"""BlueZ Device1 D-Bus wrapper for individual Bluetooth device management."""

import asyncio
import logging
from typing import Callable

from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError

from .constants import (
    BLUEZ_SERVICE,
    DEVICE_INTERFACE,
    PROPERTIES_INTERFACE,
)

logger = logging.getLogger(__name__)


def address_to_path(address: str, adapter_path: str = "/org/bluez/hci0") -> str:
    """Convert a MAC address to a BlueZ D-Bus object path."""
    return f"{adapter_path}/dev_{address.replace(':', '_')}"


class BluezDevice:
    """Wraps org.bluez.Device1 for pairing, connecting, and monitoring a device."""

    def __init__(self, bus: MessageBus, address: str, adapter_path: str = "/org/bluez/hci0"):
        self._bus = bus
        self._address = address
        self._path = address_to_path(address, adapter_path)
        self._device_iface = None
        self._properties_iface = None
        self._disconnect_callbacks: list[Callable] = []
        self._connect_callbacks: list[Callable] = []
        self._properties_changed_unsub = None

    async def initialize(self) -> None:
        """Connect to the device's D-Bus interfaces and start monitoring."""
        introspection = await self._bus.introspect(BLUEZ_SERVICE, self._path)
        proxy = self._bus.get_proxy_object(BLUEZ_SERVICE, self._path, introspection)
        self._device_iface = proxy.get_interface(DEVICE_INTERFACE)
        self._properties_iface = proxy.get_interface(PROPERTIES_INTERFACE)

        self._properties_iface.on_properties_changed(self._on_properties_changed)
        logger.debug("Device %s initialized at %s", self._address, self._path)

    def _on_properties_changed(
        self, interface_name: str, changed: dict, invalidated: list
    ) -> None:
        """Handle D-Bus PropertiesChanged signals."""
        if interface_name != DEVICE_INTERFACE:
            return

        if "Connected" in changed:
            connected = changed["Connected"].value
            if not connected:
                logger.info("Device %s disconnected", self._address)
                for cb in self._disconnect_callbacks:
                    cb(self._address)
            else:
                logger.info("Device %s connected", self._address)
                for cb in self._connect_callbacks:
                    cb(self._address)

    def on_disconnected(self, callback: Callable[[str], None]) -> None:
        """Register a callback for when this device disconnects."""
        self._disconnect_callbacks.append(callback)

    def on_connected(self, callback: Callable[[str], None]) -> None:
        """Register a callback for when this device connects."""
        self._connect_callbacks.append(callback)

    async def pair(self) -> None:
        """Initiate pairing with the device."""
        if await self.is_paired():
            logger.debug("Device %s already paired", self._address)
            return

        logger.info("Pairing with %s...", self._address)
        try:
            await self._device_iface.call_pair()
            logger.info("Paired with %s", self._address)
        except DBusError as e:
            if "AlreadyExists" in str(e):
                logger.debug("Device %s already paired (race)", self._address)
            else:
                raise

    async def set_trusted(self, trusted: bool = True) -> None:
        """Set the device as trusted (allows BlueZ auto-reconnect)."""
        from dbus_next import Variant

        await self._properties_iface.call_set(
            DEVICE_INTERFACE, "Trusted", Variant("b", trusted)
        )
        logger.info("Device %s trusted=%s", self._address, trusted)

    async def connect(self) -> None:
        """Connect to the device (all profiles including A2DP)."""
        logger.info("Connecting to %s...", self._address)
        await self._device_iface.call_connect()
        logger.info("Connected to %s", self._address)

    async def disconnect(self) -> None:
        """Disconnect from the device."""
        logger.info("Disconnecting from %s...", self._address)
        try:
            await self._device_iface.call_disconnect()
        except DBusError as e:
            logger.debug("Disconnect from %s failed: %s", self._address, e)

    async def wait_for_services(self, timeout: float = 10.0) -> bool:
        """Wait for ServicesResolved to become True after connecting."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            resolved = await self._properties_iface.call_get(
                DEVICE_INTERFACE, "ServicesResolved"
            )
            if resolved.value:
                return True
            await asyncio.sleep(0.5)
        logger.warning("Services not resolved for %s within %ss", self._address, timeout)
        return False

    async def is_paired(self) -> bool:
        """Check if the device is paired."""
        result = await self._properties_iface.call_get(DEVICE_INTERFACE, "Paired")
        return result.value

    async def is_connected(self) -> bool:
        """Check if the device is connected."""
        result = await self._properties_iface.call_get(DEVICE_INTERFACE, "Connected")
        return result.value

    async def get_name(self) -> str:
        """Get the device's friendly name."""
        try:
            result = await self._properties_iface.call_get(DEVICE_INTERFACE, "Name")
            return result.value
        except DBusError:
            return "Unknown Device"

    async def get_properties(self) -> dict:
        """Get all Device1 properties."""
        result = await self._properties_iface.call_get_all(DEVICE_INTERFACE)
        return {k: v.value for k, v in result.items()}

    @property
    def address(self) -> str:
        return self._address

    @property
    def path(self) -> str:
        return self._path
