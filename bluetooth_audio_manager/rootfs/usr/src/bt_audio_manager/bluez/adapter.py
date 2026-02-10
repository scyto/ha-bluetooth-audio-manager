"""BlueZ Adapter1 D-Bus wrapper with coexistence-safe discovery."""

import asyncio
import logging

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError

from .constants import (
    A2DP_SINK_UUID,
    A2DP_SOURCE_UUID,
    ADAPTER_INTERFACE,
    AUDIO_UUIDS,
    BLUEZ_SERVICE,
    DEFAULT_ADAPTER_PATH,
    DEVICE_INTERFACE,
    OBJECT_MANAGER_INTERFACE,
    PROPERTIES_INTERFACE,
)

logger = logging.getLogger(__name__)


class AdapterNotPoweredError(Exception):
    """Raised when the Bluetooth adapter is not powered on."""


class BluezAdapter:
    """Wraps org.bluez.Adapter1 for A2DP device discovery.

    COEXISTENCE: Uses SetDiscoveryFilter(Transport="bredr") to restrict
    scanning to Classic Bluetooth only. HA's passive BLE scanning uses
    LE transport and is completely unaffected.
    """

    def __init__(self, bus: MessageBus, adapter_path: str = DEFAULT_ADAPTER_PATH):
        self._bus = bus
        self._adapter_path = adapter_path
        self._adapter_iface = None
        self._properties_iface = None
        self._discovering = False

    async def initialize(self) -> None:
        """Connect to the adapter's D-Bus interfaces.

        Verifies the adapter is powered but NEVER modifies adapter state
        (Powered, Discoverable, Pairable) — those are managed by HAOS.
        """
        introspection = await self._bus.introspect(BLUEZ_SERVICE, self._adapter_path)
        proxy = self._bus.get_proxy_object(
            BLUEZ_SERVICE, self._adapter_path, introspection
        )
        self._adapter_iface = proxy.get_interface(ADAPTER_INTERFACE)
        self._properties_iface = proxy.get_interface(PROPERTIES_INTERFACE)

        powered = await self._properties_iface.call_get(ADAPTER_INTERFACE, "Powered")
        if not powered.value:
            raise AdapterNotPoweredError(
                "Bluetooth adapter is not powered. "
                "Enable Bluetooth in HAOS settings — this add-on does not "
                "modify adapter power state."
            )

        address = await self._properties_iface.call_get(ADAPTER_INTERFACE, "Address")
        logger.info("Adapter %s initialized at %s", address.value, self._adapter_path)

    async def start_discovery(self) -> None:
        """Start A2DP-filtered discovery on Classic Bluetooth only.

        Sets a discovery filter BEFORE starting discovery. BlueZ merges
        filters from multiple D-Bus clients, so our filter narrows only
        our own view without affecting HA's passive BLE scanning.
        """
        await self._adapter_iface.call_set_discovery_filter(
            {
                "UUIDs": Variant("as", [A2DP_SINK_UUID, A2DP_SOURCE_UUID]),
                "Transport": Variant("s", "bredr"),
            }
        )
        await self._adapter_iface.call_start_discovery()
        self._discovering = True
        logger.info("A2DP device discovery started (Transport=bredr)")

    async def stop_discovery(self) -> None:
        """Stop our discovery session.

        BlueZ reference-counts StartDiscovery/StopDiscovery per D-Bus client.
        Our stop only affects our session, not HA's passive LE scanning.
        """
        if not self._discovering:
            return
        try:
            await self._adapter_iface.call_stop_discovery()
        except DBusError as e:
            if "No discovery started" not in str(e):
                raise
        finally:
            self._discovering = False
        logger.info("A2DP device discovery stopped")

    async def get_audio_devices(self) -> list[dict]:
        """Enumerate discovered devices that have audio UUIDs.

        Uses ObjectManager to list all /org/bluez/hci0/dev_* objects
        and filters for those with A2DP capabilities.
        """
        introspection = await self._bus.introspect(BLUEZ_SERVICE, "/")
        proxy = self._bus.get_proxy_object(BLUEZ_SERVICE, "/", introspection)
        obj_manager = proxy.get_interface(OBJECT_MANAGER_INTERFACE)
        objects = await obj_manager.call_get_managed_objects()

        devices = []
        for path, interfaces in objects.items():
            if DEVICE_INTERFACE not in interfaces:
                continue

            props = interfaces[DEVICE_INTERFACE]
            uuids_variant = props.get("UUIDs")
            uuids = set(uuids_variant.value) if uuids_variant else set()

            if not uuids.intersection(AUDIO_UUIDS):
                continue

            address_variant = props.get("Address")
            name_variant = props.get("Name")
            paired_variant = props.get("Paired")
            connected_variant = props.get("Connected")
            rssi_variant = props.get("RSSI")

            # Detect active bearers (BR/EDR vs LE)
            bearers = []
            for iface_name in interfaces:
                if not iface_name.startswith("org.bluez.Bearer."):
                    continue
                bearer_props = interfaces[iface_name]
                conn_var = bearer_props.get("Connected")
                if conn_var and (conn_var.value if hasattr(conn_var, "value") else conn_var):
                    # e.g. "org.bluez.Bearer.BREDR1" → "BR/EDR"
                    short = iface_name.rsplit(".", 1)[-1]  # "BREDR1", "LE1"
                    if "BREDR" in short:
                        bearers.append("BR/EDR")
                    elif "LE" in short:
                        bearers.append("LE")
                    else:
                        bearers.append(short)

            # Check for MediaTransport1 at sub-paths (e.g. .../fd0)
            has_transport = False
            dev_fragment = path + "/"
            for obj_path in objects:
                if obj_path.startswith(dev_fragment) and "org.bluez.MediaTransport1" in objects[obj_path]:
                    has_transport = True
                    break

            devices.append(
                {
                    "path": path,
                    "address": address_variant.value if address_variant else "unknown",
                    "name": name_variant.value if name_variant else "Unknown Device",
                    "paired": paired_variant.value if paired_variant else False,
                    "connected": connected_variant.value if connected_variant else False,
                    "rssi": rssi_variant.value if rssi_variant else None,
                    "uuids": list(uuids),
                    "bearers": bearers,
                    "has_transport": has_transport,
                }
            )

        return devices

    async def remove_device(self, device_path: str) -> None:
        """Remove a device from the adapter (unpair)."""
        try:
            await self._adapter_iface.call_remove_device(device_path)
            logger.info("Removed device %s", device_path)
        except DBusError as e:
            logger.warning("Failed to remove device %s: %s", device_path, e)

    async def discover_for_duration(self, seconds: int) -> list[dict]:
        """Run discovery for a fixed duration and return found audio devices."""
        await self.start_discovery()
        await asyncio.sleep(seconds)
        await self.stop_discovery()
        return await self.get_audio_devices()

    @property
    def adapter_path(self) -> str:
        return self._adapter_path
