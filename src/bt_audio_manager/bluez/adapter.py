"""BlueZ Adapter1 D-Bus wrapper with coexistence-safe discovery."""

import asyncio
import logging
import os

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError

from .constants import (
    A2DP_SOURCE_UUID,
    ADAPTER_INTERFACE,
    AVRCP_CONTROLLER_UUID,
    AVRCP_TARGET_UUID,
    BLUEZ_SERVICE,
    COD_MAJOR_AUDIO,
    DEFAULT_ADAPTER_PATH,
    DEVICE_INTERFACE,
    LE_AUDIO_UUIDS,
    OBJECT_MANAGER_INTERFACE,
    PROPERTIES_INTERFACE,
    SINK_UUIDS,
    cod_major_class,
    cod_major_label,
)

logger = logging.getLogger(__name__)

_AVRCP_ONLY = frozenset({AVRCP_TARGET_UUID, AVRCP_CONTROLLER_UUID})
_SOURCE_UUIDS = frozenset({A2DP_SOURCE_UUID})


def _classify_rejection(uuids: set[str]) -> str:
    """Return a human-readable reason why a device was not surfaced."""
    if uuids.intersection(LE_AUDIO_UUIDS):
        return "LE Audio device, not yet supported"
    if uuids.intersection(_SOURCE_UUIDS) and not uuids.intersection(SINK_UUIDS):
        return "audio source only (e.g. phone), not a speaker"
    if uuids and uuids.issubset(_AVRCP_ONLY):
        return "AVRCP remote control only, no audio playback"
    if not uuids:
        return "no UUIDs advertised (incomplete SDP)"
    return "no audio sink profile"


class AdapterNotPoweredError(Exception):
    """Raised when the Bluetooth adapter is not powered on."""


class BluezAdapter:
    """Wraps org.bluez.Adapter1 for A2DP device discovery.

    COEXISTENCE: BlueZ reference-counts StartDiscovery/StopDiscovery per
    D-Bus client.  Our discovery session is independent of HA's passive
    BLE scanning.
    """

    def __init__(self, bus: MessageBus, adapter_path: str = DEFAULT_ADAPTER_PATH):
        self._bus = bus
        self._adapter_path = adapter_path
        self._adapter_iface = None
        self._properties_iface = None
        self._discovering = False
        # Tracks addresses already logged during this scan session,
        # so each device is logged at INFO only once per scan.
        self._logged_cache: set[str] = set()

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
                "Enable Bluetooth in HAOS settings — this app does not "
                "modify adapter power state."
            )

        address = await self._properties_iface.call_get(ADAPTER_INTERFACE, "Address")
        logger.info("Adapter %s initialized at %s", address.value, self._adapter_path)

    async def start_discovery(self) -> None:
        """Start unfiltered discovery on all transports (BR/EDR + BLE).

        No UUID filter is set so BlueZ reports ALL nearby devices.  The
        app-level filter in get_audio_devices() then decides which to
        surface, logging every rejection at INFO so support can see
        exactly what was discovered and why it was excluded.

        BlueZ reference-counts discovery per D-Bus client, so our session
        does not interfere with HA's passive BLE scanning.
        """
        await self._adapter_iface.call_set_discovery_filter(
            {
                "Transport": Variant("s", "auto"),
            }
        )
        self._logged_cache.clear()
        await self._adapter_iface.call_start_discovery()
        self._discovering = True
        logger.info("Device discovery started (all transports, no UUID filter)")

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
        logger.info("Device discovery stopped")

    async def get_audio_devices(self) -> list[dict]:
        """Enumerate discovered devices that can receive/play audio.

        Uses ObjectManager to list all /org/bluez/hci0/dev_* objects
        and filters for those with a sink-capable profile (A2DP Sink,
        HFP, or HSP).  Devices that only advertise A2DP Source (e.g.
        phones) are excluded since this add-on manages speakers.
        """
        introspection = await self._bus.introspect(BLUEZ_SERVICE, "/")
        proxy = self._bus.get_proxy_object(BLUEZ_SERVICE, "/", introspection)
        obj_manager = proxy.get_interface(OBJECT_MANAGER_INTERFACE)
        objects = await obj_manager.call_get_managed_objects()

        devices = []
        skipped = 0
        audio_class_no_uuid = 0
        for path, interfaces in objects.items():
            if DEVICE_INTERFACE not in interfaces:
                continue

            props = interfaces[DEVICE_INTERFACE]
            uuids_variant = props.get("UUIDs")
            uuids = set(uuids_variant.value) if uuids_variant else set()

            # Read Class of Device for diagnostics
            class_variant = props.get("Class")
            cod_raw = class_variant.value if class_variant else 0

            if not uuids.intersection(SINK_UUIDS):
                skipped += 1
                if not uuids and cod_major_class(cod_raw) == COD_MAJOR_AUDIO:
                    audio_class_no_uuid += 1
                addr_v = props.get("Address")
                addr = addr_v.value if addr_v else "??:??"
                name_v = props.get("Name")
                name = name_v.value if name_v else "unknown"
                # Log each rejection once per scan session at INFO
                if addr not in self._logged_cache:
                    self._logged_cache.add(addr)
                    reason = _classify_rejection(uuids)
                    cod_str = (
                        f"0x{cod_raw:06X}({cod_major_label(cod_raw)})"
                        if cod_raw else "(none)"
                    )
                    logger.info(
                        "Skipping device %s (%s) — %s. UUIDs: %s CoD: %s",
                        name, addr, reason,
                        sorted(uuids) if uuids else "(none)",
                        cod_str,
                    )
                continue

            address_variant = props.get("Address")
            name_variant = props.get("Name")
            paired_variant = props.get("Paired")
            connected_variant = props.get("Connected")
            rssi_variant = props.get("RSSI")

            paired = paired_variant.value if paired_variant else False
            connected = connected_variant.value if connected_variant else False
            rssi = rssi_variant.value if rssi_variant else None

            # Log accepted devices once per scan so the full picture is visible
            addr = address_variant.value if address_variant else "??:??"
            name = name_variant.value if name_variant else "unknown"
            if addr not in self._logged_cache:
                self._logged_cache.add(addr)
                matched = sorted(uuids.intersection(SINK_UUIDS))
                cod_str = (
                    f"0x{cod_raw:06X}({cod_major_label(cod_raw)})"
                    if cod_raw else "(none)"
                )
                if connected:
                    state = "connected"
                elif paired:
                    state = "paired (offline)"
                else:
                    state = "unpaired"
                logger.info(
                    "Accepted device %s (%s) [%s] — matched %s. CoD: %s",
                    name, addr, state, matched, cod_str,
                )

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

        if not self._discovering:
            parts = [
                f"{len(objects)} BlueZ objects scanned",
                f"{skipped - audio_class_no_uuid} unsupported skipped",
            ]
            if audio_class_no_uuid:
                parts.append(
                    f"{audio_class_no_uuid} audio-class device(s) with no UUIDs skipped"
                )
            parts.append(f"{len(devices)} supported audio devices matched")
            logger.info("get_audio_devices: %s", ", ".join(parts))
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
                if os.path.isfile(prod_file):
                    with open(prod_file) as f:
                        prod = f.read().strip()
                    if not prod:
                        continue
                    # Include manufacturer only if non-empty
                    if os.path.isfile(mfr_file):
                        with open(mfr_file) as f:
                            mfr = f.read().strip()
                        if mfr:
                            return f"{mfr} {prod}"
                    return prod
        except OSError:
            pass
        return None

    @staticmethod
    def _read_sysfs_usb_id(hci_name: str) -> str | None:
        """Read the real USB vendor:product ID from sysfs for a BT adapter.

        Walks the same path as _read_sysfs_hw_info but reads idVendor and
        idProduct files, which are always present for USB devices (unlike
        manufacturer/product string descriptors).

        Returns e.g. "2357:0604" (lowercase) or None for non-USB adapters.
        """
        base = f"/sys/class/bluetooth/{hci_name}"
        if not os.path.exists(base):
            return None
        try:
            device_path = os.path.realpath(os.path.join(base, "device"))
            for path in [device_path, os.path.dirname(device_path)]:
                vid_file = os.path.join(path, "idVendor")
                pid_file = os.path.join(path, "idProduct")
                if os.path.isfile(vid_file) and os.path.isfile(pid_file):
                    with open(vid_file) as f:
                        vid = f.read().strip().lower()
                    with open(pid_file) as f:
                        pid = f.read().strip().lower()
                    if vid and pid:
                        return f"{vid}:{pid}"
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

            # Read real USB vendor:product ID from sysfs
            usb_id = BluezAdapter._read_sysfs_usb_id(hci_name)

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
                "usb_id": usb_id or "",
                "powered": bool(_val("Powered")),
                "discovering": bool(_val("Discovering")),
            })

        # Sort by path so hci0 comes first
        adapters.sort(key=lambda a: a["path"])
        return adapters
