"""BlueZ Adapter1 D-Bus wrapper with coexistence-safe discovery."""

import asyncio
import logging
import os

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

            paired = paired_variant.value if paired_variant else False
            connected = connected_variant.value if connected_variant else False
            rssi = rssi_variant.value if rssi_variant else None

            # Skip stale BlueZ cache entries: unpaired, disconnected, no recent RSSI
            if not paired and not connected and rssi is None:
                continue

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

            # Extract adapter name from path: /org/bluez/hci0/dev_XX → hci0
            adapter_name = path.split("/")[3] if len(path.split("/")) > 3 else "unknown"

            devices.append(
                {
                    "path": path,
                    "adapter": adapter_name,
                    "address": address_variant.value if address_variant else "unknown",
                    "name": name_variant.value if name_variant else "Unknown Device",
                    "paired": paired,
                    "connected": connected,
                    "rssi": rssi,
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

    @staticmethod
    async def remove_device_any_adapter(bus: MessageBus, address: str) -> bool:
        """Find and remove a device from ALL adapters that have it.

        Searches all adapters via ObjectManager for a device with the given
        MAC address and calls RemoveDevice on every owning adapter.
        Returns True if the device was removed from at least one adapter.
        """
        dev_suffix = f"/dev_{address.replace(':', '_')}"
        introspection = await bus.introspect(BLUEZ_SERVICE, "/")
        proxy = bus.get_proxy_object(BLUEZ_SERVICE, "/", introspection)
        obj_manager = proxy.get_interface(OBJECT_MANAGER_INTERFACE)
        objects = await obj_manager.call_get_managed_objects()

        removed_any = False
        for path in list(objects):
            if not path.endswith(dev_suffix):
                continue
            # Found the device — extract adapter path (e.g. /org/bluez/hci0)
            adapter_path = path[: path.rfind("/")]
            try:
                intr = await bus.introspect(BLUEZ_SERVICE, adapter_path)
                adapter_proxy = bus.get_proxy_object(BLUEZ_SERVICE, adapter_path, intr)
                adapter_iface = adapter_proxy.get_interface(ADAPTER_INTERFACE)
                await adapter_iface.call_remove_device(path)
                logger.info("Removed device %s from adapter %s", path, adapter_path)
                removed_any = True
            except DBusError as e:
                logger.warning("Failed to remove %s from %s: %s", path, adapter_path, e)
        if not removed_any:
            logger.warning("Device %s not found on any adapter", address)
        return removed_any

    async def discover_for_duration(self, seconds: int) -> list[dict]:
        """Run discovery for a fixed duration and return found audio devices."""
        await self.start_discovery()
        await asyncio.sleep(seconds)
        await self.stop_discovery()
        return await self.get_audio_devices()

    @property
    def adapter_path(self) -> str:
        return self._adapter_path

    @staticmethod
    def _read_sysfs_hw_info(hci_name: str) -> str | None:
        """Read hardware manufacturer + product from sysfs for a BT adapter.

        Walks up from /sys/class/bluetooth/hciX/device to find the USB
        (or platform) device's manufacturer and product files.
        Returns e.g. "cyber-blue(HK)Ltd CSR8510 A10" or None.
        """
        base = f"/sys/class/bluetooth/{hci_name}"
        if not os.path.exists(base):
            return None
        try:
            device_path = os.path.realpath(os.path.join(base, "device"))
            # Walk up directories to find manufacturer/product files
            # (USB devices have them one level up from the BT device)
            for path in [device_path, os.path.dirname(device_path)]:
                mfr_file = os.path.join(path, "manufacturer")
                prod_file = os.path.join(path, "product")
                if os.path.isfile(mfr_file) and os.path.isfile(prod_file):
                    mfr = open(mfr_file).read().strip()
                    prod = open(prod_file).read().strip()
                    return f"{mfr} {prod}"
                # Some devices only have product
                if os.path.isfile(prod_file):
                    return open(prod_file).read().strip()
        except OSError:
            pass
        return None

    @staticmethod
    async def list_all(bus: MessageBus) -> list[dict]:
        """Enumerate all Bluetooth adapters on the system.

        Returns a list of dicts with adapter info including path, address,
        name, powered state, hardware model, and whether discovery is
        active (indicating HA BLE scanning).
        """
        introspection = await bus.introspect(BLUEZ_SERVICE, "/")
        proxy = bus.get_proxy_object(BLUEZ_SERVICE, "/", introspection)
        obj_manager = proxy.get_interface(OBJECT_MANAGER_INTERFACE)
        objects = await obj_manager.call_get_managed_objects()

        adapters = []
        for path, interfaces in objects.items():
            if ADAPTER_INTERFACE not in interfaces:
                continue
            props = interfaces[ADAPTER_INTERFACE]

            def _val(key, _props=props):
                v = _props.get(key)
                if v is None:
                    return None
                return v.value if hasattr(v, "value") else v

            hci_name = path.rsplit("/", 1)[-1]  # e.g. "hci0"

            # Try to get hardware model from sysfs (USB manufacturer + product)
            hw_model = BluezAdapter._read_sysfs_hw_info(hci_name)

            # Fall back to BlueZ Modalias property (e.g. "usb:v0A12p0001d0678")
            modalias = _val("Modalias") or ""
            if not hw_model and modalias:
                hw_model = modalias

            adapters.append({
                "path": path,
                "name": hci_name,
                "address": _val("Address") or "unknown",
                "alias": _val("Alias") or "",
                "hw_model": hw_model or "",
                "modalias": modalias,
                "powered": bool(_val("Powered")),
                "discovering": bool(_val("Discovering")),
            })

        # Sort by path so hci0 comes first
        adapters.sort(key=lambda a: a["path"])
        return adapters
