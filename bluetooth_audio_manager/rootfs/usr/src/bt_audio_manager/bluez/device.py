"""BlueZ Device1 D-Bus wrapper for individual Bluetooth device management."""

import asyncio
import logging
import time
from typing import Callable
from xml.etree import ElementTree as ET

from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError

from .constants import (
    BLUEZ_SERVICE,
    DEFAULT_ADAPTER_PATH,
    DEVICE_INTERFACE,
    PROPERTIES_INTERFACE,
)

logger = logging.getLogger(__name__)

MEDIA_PLAYER_INTERFACE = "org.bluez.MediaPlayer1"


def address_to_path(address: str, adapter_path: str = DEFAULT_ADAPTER_PATH) -> str:
    """Convert a MAC address to a BlueZ D-Bus object path."""
    return f"{adapter_path}/dev_{address.replace(':', '_')}"


class BluezDevice:
    """Wraps org.bluez.Device1 for pairing, connecting, and monitoring a device."""

    def __init__(self, bus: MessageBus, address: str, adapter_path: str = DEFAULT_ADAPTER_PATH):
        self._bus = bus
        self._address = address
        self._path = address_to_path(address, adapter_path)
        self._device_iface = None
        self._properties_iface = None
        self._disconnect_callbacks: list[Callable] = []
        self._connect_callbacks: list[Callable] = []
        self._avrcp_callbacks: list[Callable] = []
        self._player_path: str | None = None
        self._properties_changed_unsub = None
        self._avrcp_last_search: float = 0.0  # monotonic timestamp of last failed search
        self._avrcp_cooldown: float = 60.0  # seconds to wait before searching again

    async def initialize(self) -> None:
        """Connect to the device's D-Bus interfaces and start monitoring."""
        introspection = await self._bus.introspect(BLUEZ_SERVICE, self._path)
        proxy = self._bus.get_proxy_object(BLUEZ_SERVICE, self._path, introspection)
        self._device_iface = proxy.get_interface(DEVICE_INTERFACE)
        self._properties_iface = proxy.get_interface(PROPERTIES_INTERFACE)

        self._properties_iface.on_properties_changed(self._on_properties_changed)
        logger.debug("Device %s initialized at %s", self._address, self._path)

    def cleanup(self) -> None:
        """Remove D-Bus signal subscriptions and clear callbacks.

        Call this before discarding a BluezDevice to prevent leaked subscriptions.
        """
        if self._properties_iface:
            self._properties_iface.off_properties_changed(self._on_properties_changed)
        self._disconnect_callbacks.clear()
        self._connect_callbacks.clear()
        self._avrcp_callbacks.clear()
        self._player_path = None
        self._avrcp_last_search = 0.0
        logger.debug("Device %s cleaned up", self._address)

    def reset_avrcp_watch(self) -> None:
        """Clear AVRCP subscription state so watch_media_player() will re-search."""
        self._player_path = None
        self._avrcp_last_search = 0.0

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

    def on_avrcp_event(self, callback: Callable[[str, str, object], None]) -> None:
        """Register a callback for AVRCP MediaPlayer1 property changes.

        Callback signature: callback(address, property_name, value)
        """
        self._avrcp_callbacks.append(callback)

    async def watch_media_player(self, retries: int = 3, delay: float = 2.0) -> bool:
        """Introspect for MediaPlayer1 child nodes and subscribe to signals.

        BlueZ may take a moment to create the player node after A2DP connects,
        so we retry a few times with a delay.

        Returns True if a media player was found and subscribed.
        """
        if self._player_path:
            logger.debug("AVRCP already watching %s", self._player_path)
            return True

        # Cooldown: skip if we searched recently and found nothing
        elapsed = time.monotonic() - self._avrcp_last_search
        if self._avrcp_last_search > 0 and elapsed < self._avrcp_cooldown:
            logger.debug(
                "AVRCP search for %s on cooldown (%.0fs remaining)",
                self._address, self._avrcp_cooldown - elapsed,
            )
            return False

        for attempt in range(retries):
            try:
                introspection = await self._bus.introspect(BLUEZ_SERVICE, self._path)
                xml_data = introspection.tostring()
                root = ET.fromstring(xml_data)

                player_nodes = [
                    n.get("name") for n in root.findall("node")
                    if n.get("name", "").startswith("player")
                ]

                if not player_nodes:
                    if attempt < retries - 1:
                        logger.debug(
                            "No AVRCP player for %s yet (attempt %d/%d), retrying in %.0fs...",
                            self._address, attempt + 1, retries, delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    logger.debug(
                        "No AVRCP player on %s after %d attempts "
                        "(normal for speakers — button events use registered MPRIS player)",
                        self._address, retries,
                    )
                    self._avrcp_last_search = time.monotonic()
                    return False

                # Use the first player
                player_name = player_nodes[0]
                self._player_path = f"{self._path}/{player_name}"
                logger.info(
                    "AVRCP player found for %s: %s", self._address, self._player_path
                )

                # Subscribe to PropertiesChanged on the player
                player_introspection = await self._bus.introspect(
                    BLUEZ_SERVICE, self._player_path
                )
                player_proxy = self._bus.get_proxy_object(
                    BLUEZ_SERVICE, self._player_path, player_introspection
                )
                player_props = player_proxy.get_interface(PROPERTIES_INTERFACE)
                player_props.on_properties_changed(self._on_media_player_changed)

                # Read initial state
                try:
                    all_props = await player_props.call_get_all(MEDIA_PLAYER_INTERFACE)
                    for prop_name, variant in all_props.items():
                        val = variant.value
                        logger.info("AVRCP %s initial: %s = %s", self._address, prop_name, val)
                        for cb in self._avrcp_callbacks:
                            cb(self._address, prop_name, val)
                except DBusError as e:
                    logger.debug("Could not read initial AVRCP state: %s", e)

                return True
            except DBusError as e:
                if attempt < retries - 1:
                    logger.debug(
                        "AVRCP introspect failed for %s (attempt %d/%d): %s, retrying...",
                        self._address, attempt + 1, retries, e,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.debug("AVRCP introspect failed for %s after %d attempts: %s", self._address, retries, e)
                    self._avrcp_last_search = time.monotonic()
                    return False
        self._avrcp_last_search = time.monotonic()
        return False

    def _on_media_player_changed(
        self, interface_name: str, changed: dict, invalidated: list
    ) -> None:
        """Handle AVRCP MediaPlayer1 PropertiesChanged signals."""
        if interface_name != MEDIA_PLAYER_INTERFACE:
            return

        for prop_name, variant in changed.items():
            val = variant.value
            # Flatten Track dict values from Variant
            if prop_name == "Track" and isinstance(val, dict):
                val = {k: (v.value if hasattr(v, "value") else v) for k, v in val.items()}
            logger.info("AVRCP %s: %s = %s", self._address, prop_name, val)
            for cb in self._avrcp_callbacks:
                cb(self._address, prop_name, val)

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

    async def connect_profile(self, uuid: str) -> None:
        """Connect a specific Bluetooth profile by UUID.

        Useful to explicitly activate A2DP when the device is connected
        but the audio transport was not established.
        """
        logger.info("ConnectProfile %s on %s...", uuid, self._address)
        await self._device_iface.call_connect_profile(uuid)
        logger.info("ConnectProfile %s on %s succeeded", uuid, self._address)

    async def disconnect_profile(self, uuid: str) -> None:
        """Disconnect a specific Bluetooth profile by UUID.

        Tears down a single profile (e.g. A2DP) without dropping the
        entire device connection.
        """
        logger.info("DisconnectProfile %s on %s...", uuid, self._address)
        await self._device_iface.call_disconnect_profile(uuid)
        logger.info("DisconnectProfile %s on %s succeeded", uuid, self._address)

    async def get_uuids(self) -> list[str]:
        """Get the list of service UUIDs advertised by the device."""
        try:
            result = await self._properties_iface.call_get(DEVICE_INTERFACE, "UUIDs")
            return list(result.value) if result.value else []
        except DBusError:
            return []

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
