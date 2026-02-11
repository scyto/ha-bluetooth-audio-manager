"""Top-level orchestrator for Bluetooth audio device management.

Coordinates all sub-components: BlueZ adapter, pairing agent, device
management, PulseAudio, reconnection, keep-alive, and the web server.
"""

import asyncio
import collections
import json
import logging
import os
import time

from dbus_next.aio import MessageBus
from dbus_next import BusType, Message, MessageType
from dbus_next.errors import DBusError

from .audio.keepalive import KeepAliveService
from .audio.pulse import PulseAudioManager
from .bluez.adapter import BluezAdapter
from .bluez.agent import PairingAgent
from .bluez.device import BluezDevice
from .bluez.media_player import AVRCPMediaPlayer
from .config import AppConfig
from .persistence.store import PersistenceStore
from .reconnect import ReconnectService
from .web.events import EventBus

logger = logging.getLogger(__name__)


class BluetoothAudioManager:
    """Central orchestrator for the Bluetooth Audio Manager add-on."""

    SINK_POLL_INTERVAL = 5  # seconds between sink state polls
    MAX_RECENT_EVENTS = 50  # ring buffer size for MPRIS/AVRCP events

    def __init__(self, config: AppConfig):
        self.config = config
        self._adapter_path = config.adapter_path
        self.bus: MessageBus | None = None
        self.adapter: BluezAdapter | None = None
        self.agent: PairingAgent | None = None
        self.pulse: PulseAudioManager | None = None
        self.store: PersistenceStore | None = None
        self.reconnect_service: ReconnectService | None = None
        self.keepalive: KeepAliveService | None = None
        self.media_player: AVRCPMediaPlayer | None = None
        self.managed_devices: dict[str, BluezDevice] = {}
        self._web_server = None
        self.event_bus = EventBus()
        self._sink_poll_task: asyncio.Task | None = None
        self._volume_poll_task: asyncio.Task | None = None
        self._last_sink_snapshot: str = ""
        self._last_signaled_volume: dict[str, int] = {}  # addr → raw 0-127
        self._last_polled_volume: dict[str, int] = {}    # addr → raw 0-127
        self._device_connect_time: dict[str, float] = {}  # addr → time.time()
        self._renegotiation_count: dict[str, int] = {}  # addr → number of attempts
        self.MAX_RENEGOTIATION_ATTEMPTS = 1  # stop after this many tries per session
        self._connecting: set[str] = set()  # addrs with connection in progress
        self._suppress_reconnect: set[str] = set()  # addresses with user-initiated disconnect
        # Ring buffers so SSE clients get recent events on reconnect
        self.recent_mpris: collections.deque = collections.deque(maxlen=self.MAX_RECENT_EVENTS)
        self.recent_avrcp: collections.deque = collections.deque(maxlen=self.MAX_RECENT_EVENTS)

    async def start(self) -> None:
        """Full startup sequence."""
        # 1. Connect to system D-Bus
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        logger.info("Connected to system D-Bus")

        # Capture all D-Bus activity from BlueZ so we can diagnose
        # which signals/methods arrive for button presses, volume, etc.
        def _dbus_msg_handler(msg: Message) -> bool:
            if msg.message_type == MessageType.METHOD_CALL:
                logger.debug(
                    "D-Bus method_call: %s.%s path=%s sender=%s",
                    msg.interface, msg.member, msg.path, msg.sender,
                )
            elif (
                msg.message_type == MessageType.SIGNAL
                and msg.path
                and msg.path.startswith("/org/bluez/")
            ):
                if msg.member == "PropertiesChanged" and msg.body:
                    # body = [interface_name, changed_props, invalidated]
                    iface_name = msg.body[0] if msg.body else None
                    changed = msg.body[1] if len(msg.body) > 1 else {}
                    prop_names = list(changed.keys()) if isinstance(changed, dict) else []

                    # Suppress noisy RSSI / ManufacturerData spam to DEBUG
                    _NOISY_PROPS = {"RSSI", "ManufacturerData", "TxPower"}
                    if iface_name == "org.bluez.Device1" and set(prop_names) <= _NOISY_PROPS:
                        logger.debug(
                            "BlueZ PropertiesChanged: iface=%s props=%s path=%s",
                            iface_name, prop_names, msg.path,
                        )
                    else:
                        logger.info(
                            "BlueZ PropertiesChanged: iface=%s props=%s path=%s",
                            iface_name, prop_names, msg.path,
                        )

                    if iface_name == "org.bluez.MediaTransport1" and "Volume" in changed:
                        vol_raw = changed["Volume"].value  # 0-127 uint16
                        vol_pct = round(vol_raw / 127 * 100)
                        logger.info("AVRCP transport volume: %d%% (raw %d)", vol_pct, vol_raw)
                        # Extract device address from path like /org/bluez/hci0/dev_XX_XX_XX_XX_XX_XX/...
                        parts = msg.path.split("/")
                        addr = next((p[4:].replace("_", ":") for p in parts if p.startswith("dev_")), "")
                        self._last_signaled_volume[addr] = vol_raw
                        entry = {"address": addr, "property": "Volume", "value": f"{vol_pct}%", "ts": time.time()}
                        self.recent_avrcp.append(entry)
                        self.event_bus.emit("avrcp_event", entry)
                else:
                    # Log ALL other BlueZ signals (InterfacesAdded, etc.)
                    logger.info(
                        "BlueZ signal: %s.%s path=%s",
                        msg.interface, msg.member, msg.path,
                    )
            return False  # don't consume
        self.bus.add_message_handler(_dbus_msg_handler)

        # Subscribe to ALL BlueZ signals (broad match for diagnosis)
        for match_rule in [
            "type='signal',sender='org.bluez'",
        ]:
            await self.bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="AddMatch",
                    signature="s",
                    body=[match_rule],
                )
            )

        # 2. Initialize BlueZ adapter (using configured adapter path)
        logger.info("Using Bluetooth adapter: %s", self._adapter_path)
        self.adapter = BluezAdapter(self.bus, self._adapter_path)
        await self.adapter.initialize()

        # 3. Register pairing agent
        self.agent = PairingAgent(self.bus)
        await self.agent.register()

        # 3b. Register AVRCP media player (receives speaker button commands)
        self.media_player = AVRCPMediaPlayer(self.bus, self._on_avrcp_command, self._adapter_path)
        try:
            await self.media_player.register()
        except Exception as e:
            logger.warning("AVRCP media player registration failed: %s", e)
            self.media_player = None

        # 4. Load persistent device store
        self.store = PersistenceStore()
        await self.store.load()

        # 5. Initialize PulseAudio manager
        pulse_server = os.environ.get("PULSE_SERVER", "<unset>")
        logger.info("PULSE_SERVER=%s", pulse_server)
        self.pulse = PulseAudioManager()
        try:
            await self.pulse.connect()
            self.pulse.on_volume_change(self._on_pa_volume_change)
            await self.pulse.start_event_monitor()
        except Exception as e:
            logger.warning("PulseAudio connection failed (will retry): %s", e)
            self.pulse = None

        # 6. Register BluezDevice objects for all stored devices so UI
        #    actions (disconnect, forget) work immediately, even if the
        #    device is already connected from a previous add-on session.
        for device_info in self.store.devices:
            addr = device_info["address"]
            try:
                device = await self._get_or_create_device(addr)
                if await device.is_connected():
                    logger.info("Device %s already connected, initializing fully", addr)
                    self._renegotiation_count.pop(addr, None)
                    self._last_signaled_volume.pop(addr, None)
                    self._last_polled_volume.pop(addr, None)
                    self._device_connect_time[addr] = time.time()
                    try:
                        await device.wait_for_services(timeout=5)
                    except Exception as e:
                        logger.debug("wait_for_services for %s: %s", addr, e)
                    try:
                        await device.watch_media_player()
                    except Exception as e:
                        logger.debug("AVRCP on existing connection %s: %s", addr, e)
                    # Disconnect HFP to force AVRCP volume
                    await self._disconnect_hfp(addr)
                    await self._ensure_a2dp_transport(addr)
                    if self.pulse:
                        sink_name = await self.pulse.get_sink_for_address(addr)
                        if not sink_name:
                            logger.info("No PA sink for %s at startup, activating card profile...", addr)
                            if await self.pulse.activate_bt_card_profile(addr):
                                sink_name = await self.pulse.wait_for_bt_sink(addr, timeout=10)
                        if sink_name:
                            logger.info("PA sink for %s: %s", addr, sink_name)
                            if self.keepalive:
                                self.keepalive.set_target_sink(sink_name)
                        else:
                            logger.warning("No PA sink found for already-connected device %s", addr)
            except DBusError as e:
                logger.debug("Could not initialize stored device %s: %s", addr, e)

        # 7. Start reconnection service
        self.reconnect_service = ReconnectService(self)
        await self.reconnect_service.start()

        # 8. Reconnect stored devices that aren't already connected
        await self.reconnect_service.reconnect_all()

        # 9. Start keep-alive if enabled
        if self.config.keep_alive_enabled:
            self.keepalive = KeepAliveService(method=self.config.keep_alive_method)
            await self.keepalive.start()

        # 10. Start periodic sink state polling
        self._sink_poll_task = asyncio.create_task(self._sink_poll_loop())

        # 11. Start diagnostic volume poller (detects BlueZ AVRCP volume signal loss)
        self._volume_poll_task = asyncio.create_task(self._volume_poll_loop())

        logger.info("Bluetooth Audio Manager started successfully")

    async def shutdown(self) -> None:
        """Graceful teardown in reverse order."""
        logger.info("Shutting down Bluetooth Audio Manager...")

        # Stop sink polling
        if self._sink_poll_task and not self._sink_poll_task.done():
            self._sink_poll_task.cancel()
            try:
                await self._sink_poll_task
            except asyncio.CancelledError:
                pass

        # Stop volume diagnostic polling
        if self._volume_poll_task and not self._volume_poll_task.done():
            self._volume_poll_task.cancel()
            try:
                await self._volume_poll_task
            except asyncio.CancelledError:
                pass

        # Stop keep-alive
        if self.keepalive:
            await self.keepalive.stop()

        # Stop reconnection service
        if self.reconnect_service:
            await self.reconnect_service.stop()

        # Unregister AVRCP media player
        if self.media_player:
            await self.media_player.unregister()

        # Unregister pairing agent
        if self.agent:
            await self.agent.unregister()

        # Stop any active discovery
        if self.adapter:
            await self.adapter.stop_discovery()

        # Disconnect PulseAudio
        if self.pulse:
            await self.pulse.disconnect()

        # Disconnect D-Bus (do NOT disconnect BT devices — user may want
        # audio to persist if the add-on restarts)
        if self.bus:
            self.bus.disconnect()

        logger.info("Bluetooth Audio Manager shut down")

    # -- Device lifecycle operations --

    async def _get_or_create_device(self, address: str) -> BluezDevice:
        """Get an existing managed device or create and register a new one.

        Ensures only one BluezDevice (and one D-Bus subscription) exists per address.
        """
        device = self.managed_devices.get(address)
        if device:
            return device

        device = BluezDevice(self.bus, address, self._adapter_path)
        await device.initialize()
        device.on_disconnected(self._on_device_disconnected)
        device.on_connected(self._on_device_connected)
        device.on_avrcp_event(self._on_avrcp_event)
        self.managed_devices[address] = device
        return device

    async def scan_devices(self, duration: int | None = None) -> list[dict]:
        """Run a time-limited discovery scan for A2DP audio devices."""
        duration = duration or self.config.scan_duration_seconds
        self._broadcast_status(f"Scanning for Bluetooth audio devices ({duration}s)...")
        devices = await self.adapter.discover_for_duration(duration)
        self.event_bus.emit("status", {"message": ""})
        await self._broadcast_devices()
        return devices

    async def pair_device(self, address: str) -> dict:
        """Pair, trust, persist, and connect a Bluetooth audio device."""
        self._broadcast_status(f"Pairing with {address}...")
        device = await self._get_or_create_device(address)

        # Pair
        await device.pair()

        # Trust (enables BlueZ-level auto-reconnect)
        await device.set_trusted(True)

        # Get name for display
        name = await device.get_name()

        # Persist
        await self.store.add_device(address, name)

        logger.info("Device %s (%s) paired and stored", address, name)
        await self._broadcast_all()

        # Follow through with full connect + A2DP sink wait
        connected = await self.connect_device(address)
        return {"address": address, "name": name, "connected": connected}

    async def connect_device(self, address: str) -> bool:
        """Connect to a paired device and verify A2DP sink appears."""
        # Do NOT reset _renegotiation_count here.  If renegotiation already
        # ran (or timed out) and the user manually reconnects, resetting the
        # counter would let Check B fire again after 15 s and disconnect the
        # device the user just connected.  The counter is only cleared at
        # add-on startup for devices that are already connected.

        # If another connection attempt is already in progress, wait for it
        if address in self._connecting:
            logger.info("Connection already in progress for %s, waiting...", address)
            self._broadcast_status(f"Waiting for connection to {address}...")
            for _ in range(60):
                await asyncio.sleep(0.5)
                if address not in self._connecting:
                    break
            device = self.managed_devices.get(address)
            if device and await device.is_connected():
                if self.pulse:
                    sink = await self.pulse.get_sink_for_address(address)
                    if sink:
                        await self._broadcast_all()
                        return True
            await self._broadcast_all()
            return False

        # Cancel any pending auto-reconnect to avoid racing
        if self.reconnect_service:
            self.reconnect_service.cancel_reconnect(address)
        # Clear any disconnect suppression (user wants to connect now)
        self._suppress_reconnect.discard(address)
        self._broadcast_status(f"Connecting to {address}...")

        self._connecting.add(address)
        try:
            device = await self._get_or_create_device(address)

            # Skip redundant BlueZ connect if already connected, but still
            # wait for services and A2DP sink (e.g. after pairing auto-connect)
            already_connected = False
            try:
                already_connected = await device.is_connected()
            except Exception:
                pass

            if already_connected:
                logger.info("Device %s already connected, waiting for services/sink", address)
            else:
                await device.connect()

            self._broadcast_status(f"Waiting for services on {address}...")
            await device.wait_for_services(timeout=10)

            # Try to subscribe to AVRCP media player signals
            try:
                await device.watch_media_player()
            except Exception as e:
                logger.debug("AVRCP watch failed for %s: %s", address, e)

            # Disconnect HFP to force AVRCP volume (speakers send AT+VGS otherwise)
            await self._disconnect_hfp(address)

            # Verify PulseAudio sink appeared
            if self.pulse:
                self._broadcast_status(f"Waiting for A2DP sink for {address}...")
                sink_name = await self.pulse.wait_for_bt_sink(address, timeout=15)
                if sink_name:
                    if self.keepalive:
                        self.keepalive.set_target_sink(sink_name)
                    await self._broadcast_all()
                    return True
                logger.warning("A2DP sink for %s did not appear in PulseAudio", address)
                await self._broadcast_all()
                return False

            # PulseAudio not available — connection may still work at BlueZ level
            await self._broadcast_all()
            return await device.is_connected()
        finally:
            self._connecting.discard(address)

    async def disconnect_device(self, address: str) -> None:
        """Disconnect a device without removing it from the store."""
        self._broadcast_status(f"Disconnecting {address}...")
        # Cancel any pending reconnection
        if self.reconnect_service:
            self.reconnect_service.cancel_reconnect(address)

        # Suppress auto-reconnect for this user-initiated disconnect
        self._suppress_reconnect.add(address)

        device = self.managed_devices.get(address)
        if device:
            await device.disconnect()
        else:
            logger.warning("Disconnect: device %s not in managed_devices", address)
        self.event_bus.emit("status", {"message": ""})
        await self._broadcast_all()

    async def forget_device(self, address: str) -> None:
        """Unpair, remove from BlueZ, and delete from persistent store."""
        # Cancel reconnection
        if self.reconnect_service:
            self.reconnect_service.cancel_reconnect(address)

        # Disconnect and clean up D-Bus subscriptions
        device = self.managed_devices.pop(address, None)
        if device:
            try:
                await device.disconnect()
            except DBusError:
                pass
            device.cleanup()

        # Remove from BlueZ (search all adapters — device may be on a
        # different adapter than the one this add-on is configured to use)
        await BluezAdapter.remove_device_any_adapter(self.bus, address)

        # Remove from persistent store
        await self.store.remove_device(address)
        logger.info("Device %s forgotten", address)
        await self._broadcast_all()

    async def get_all_devices(self) -> list[dict]:
        """Get combined list of discovered and paired devices."""
        if not self.adapter:
            return []  # still initializing
        # Get currently visible devices from BlueZ
        discovered = await self.adapter.get_audio_devices()

        # Merge with persistent store info
        stored_addresses = {d["address"] for d in self.store.devices}
        for device in discovered:
            device["stored"] = device["address"] in stored_addresses

        # Add stored devices not currently visible
        discovered_addresses = {d["address"] for d in discovered}
        for stored in self.store.devices:
            if stored["address"] not in discovered_addresses:
                discovered.append(
                    {
                        "address": stored["address"],
                        "name": stored["name"],
                        "paired": True,
                        "connected": False,
                        "rssi": None,
                        "stored": True,
                        "uuids": [],
                        "bearers": [],
                        "has_transport": False,
                        "adapter": "",
                    }
                )

        return discovered

    async def get_audio_sinks(self) -> list[dict]:
        """List Bluetooth PulseAudio sinks."""
        if not self.pulse:
            return []
        return await self.pulse.list_bt_sinks()

    async def list_adapters(self) -> list[dict]:
        """List all Bluetooth adapters on the system.

        Each adapter dict includes a flag indicating whether it's the
        one this add-on is configured to use, and whether it appears to
        be running HA's BLE scanning (Discovering=true).

        Enriches adapter entries with USB device names from the HA
        Supervisor hardware API when sysfs info isn't available (common
        inside Docker containers).
        """
        if not self.bus:
            return []
        adapters = await BluezAdapter.list_all(self.bus)

        # Enrich with USB device names from Supervisor if sysfs failed
        needs_enrichment = any(
            not a["hw_model"] or a["hw_model"] == a["modalias"]
            for a in adapters
        )
        if needs_enrichment:
            usb_names = await self._get_supervisor_usb_names()
            if usb_names:
                for a in adapters:
                    if a["hw_model"] and a["hw_model"] != a["modalias"]:
                        continue  # already has a good name from sysfs
                    # Try direct match by hci name (from sysfs path in Supervisor data)
                    hci_key = f"hci:{a['name']}"
                    if hci_key in usb_names:
                        a["hw_model"] = usb_names[hci_key]
                        continue
                    # Try match by Modalias → USB vendor:product
                    usb_id = self._modalias_to_usb_id(a["modalias"])
                    if usb_id and usb_id in usb_names:
                        a["hw_model"] = usb_names[usb_id]

        for a in adapters:
            a["selected"] = a["path"] == self._adapter_path
            a["ble_scanning"] = a["discovering"] and not a["selected"]
        return adapters

    @staticmethod
    async def _get_supervisor_usb_names() -> dict[str, str]:
        """Query the HA Supervisor hardware API for USB device names.

        Returns two mappings:
        - USB id (vendor:product, lowercase) → device description
        - hci adapter name → device description (when sysfs path reveals it)
        Combined into a single dict keyed by both.
        """
        import aiohttp
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            return {}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "http://supervisor/hardware/info",
                    headers={"Authorization": f"Bearer {token}"},
                ) as resp:
                    if resp.status != 200:
                        return {}
                    result = await resp.json()

            devices = result.get("data", {}).get("devices", [])
            names: dict[str, str] = {}

            for dev in devices:
                attrs = dev.get("attributes", {})
                subsystem = dev.get("subsystem", "")
                sysfs = dev.get("sysfs", dev.get("by_id", ""))
                dev_name = dev.get("name", "")

                # Build a human-readable name from available attributes
                name = (
                    attrs.get("ID_MODEL_FROM_DATABASE")
                    or attrs.get("ID_MODEL")
                    or ""
                )
                vendor = (
                    attrs.get("ID_VENDOR_FROM_DATABASE")
                    or attrs.get("ID_VENDOR")
                    or ""
                )
                full_name = f"{vendor} {name}".strip() if vendor and name else (name or vendor or dev_name)

                if not full_name:
                    continue

                # Key by USB vendor:product if available
                vid = attrs.get("ID_VENDOR_ID", "")
                pid = attrs.get("ID_MODEL_ID", "")
                if vid and pid:
                    usb_id = f"{vid.lower()}:{pid.lower()}"
                    names[usb_id] = full_name

                # Key by hci name if the sysfs path reveals the BT adapter
                # e.g. sysfs path containing /bluetooth/hci0
                if "bluetooth" in sysfs.lower() or subsystem == "bluetooth":
                    import re
                    m = re.search(r"(hci\d+)", sysfs)
                    if m:
                        names[f"hci:{m.group(1)}"] = full_name

            logger.info("Supervisor HW names: %s", names)
            # Also log raw device list for debugging (first time only)
            if not names:
                for dev in devices[:20]:
                    logger.info("Supervisor HW device: subsystem=%s name=%s sysfs=%s attrs=%s",
                                dev.get("subsystem"), dev.get("name"),
                                dev.get("sysfs", dev.get("by_id", "")),
                                {k: v for k, v in dev.get("attributes", {}).items()
                                 if any(kw in k.upper() for kw in ("VENDOR", "MODEL", "PRODUCT", "ID_"))})
            return names
        except Exception as e:
            logger.debug("Failed to query Supervisor hardware API: %s", e)
            return {}

    @staticmethod
    def _modalias_to_usb_id(modalias: str) -> str | None:
        """Convert a USB modalias to a vendor:product ID string.

        'usb:v1234p5678d0001' → '1234:5678'
        """
        import re
        if not modalias or not modalias.startswith("usb:"):
            return None
        m = re.match(r"usb:v([0-9A-Fa-f]{4})p([0-9A-Fa-f]{4})", modalias)
        if not m:
            return None
        return f"{m.group(1).lower()}:{m.group(2).lower()}"

    # -- Sink state polling --

    async def _sink_poll_loop(self) -> None:
        """Periodically check PulseAudio sink state and broadcast changes.

        Detects idle→running transitions (playback started/stopped) that
        don't trigger D-Bus signals.
        """
        prev_sink_count = -1  # force first log
        while True:
            try:
                await asyncio.sleep(self.SINK_POLL_INTERVAL)
                if not self.pulse:
                    continue
                sinks = await self.pulse.list_bt_sinks()
                # Log sink count transitions
                if len(sinks) != prev_sink_count:
                    names = [s["name"] for s in sinks] if sinks else []
                    logger.info(
                        "BT sinks: %d (was %d) %s",
                        len(sinks), max(prev_sink_count, 0), names,
                    )
                    prev_sink_count = len(sinks)
                snapshot = json.dumps(sinks, sort_keys=True)
                if snapshot != self._last_sink_snapshot:
                    self._last_sink_snapshot = snapshot
                    self.event_bus.emit("sinks_changed", {"sinks": sinks})
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug("Sink poll error: %s", e)

    VOLUME_POLL_INTERVAL = 5  # seconds between MediaTransport1 volume polls

    async def _volume_poll_loop(self) -> None:
        """Diagnostic: periodically read MediaTransport1 Volume via GetManagedObjects.

        Compares polled value with last PropertiesChanged signal to detect
        whether BlueZ has the volume but isn't signaling changes.
        """
        from .bluez.constants import BLUEZ_SERVICE, OBJECT_MANAGER_INTERFACE

        while True:
            try:
                await asyncio.sleep(self.VOLUME_POLL_INTERVAL)
                if not self.bus or not self.managed_devices:
                    continue

                intro = await self.bus.introspect(BLUEZ_SERVICE, "/")
                proxy = self.bus.get_proxy_object(BLUEZ_SERVICE, "/", intro)
                obj_mgr = proxy.get_interface(OBJECT_MANAGER_INTERFACE)
                objects = await obj_mgr.call_get_managed_objects()

                # Check each managed device for a MediaTransport1
                for addr in list(self.managed_devices):
                    dev_fragment = addr.replace(":", "_").upper()
                    found_transport = False

                    for path, ifaces in objects.items():
                        if dev_fragment not in path:
                            continue
                        if "org.bluez.MediaTransport1" not in ifaces:
                            continue
                        found_transport = True
                        tp = ifaces["org.bluez.MediaTransport1"]
                        if "Volume" not in tp:
                            logger.debug("DIAG: MediaTransport1 for %s has no Volume property", addr)
                            break

                        vol_raw = tp["Volume"].value if hasattr(tp["Volume"], "value") else tp["Volume"]
                        vol_pct = round(vol_raw / 127 * 100)
                        prev_polled = self._last_polled_volume.get(addr)
                        last_sig = self._last_signaled_volume.get(addr)

                        if prev_polled is not None and vol_raw != prev_polled:
                            # Volume changed since last poll
                            if last_sig is None or last_sig != vol_raw:
                                logger.info(
                                    "DIAG: Volume changed to %d%% (raw %d) for %s via poll "
                                    "— no PropertiesChanged signal received (last signal raw=%s)",
                                    vol_pct, vol_raw, addr, last_sig,
                                )
                                # Emit as AVRCP event so UI shows it
                                entry = {"address": addr, "property": "Volume", "value": f"{vol_pct}%", "ts": time.time()}
                                self.recent_avrcp.append(entry)
                                self.event_bus.emit("avrcp_event", entry)
                            else:
                                logger.debug(
                                    "DIAG: Volume poll %d%% for %s matches signal", vol_pct, addr,
                                )
                        elif prev_polled is not None:
                            logger.debug(
                                "DIAG: Volume poll unchanged %d%% for %s (signal raw=%s)",
                                vol_pct, addr, last_sig,
                            )

                        self._last_polled_volume[addr] = vol_raw

                        # -- AVRCP volume renegotiation checks --
                        attempts = self._renegotiation_count.get(addr, 0)
                        if attempts < self.MAX_RENEGOTIATION_ATTEMPTS:
                            needs_renegotiation = False
                            connect_time = self._device_connect_time.get(addr)

                            # Check A: transport missing Endpoint (stale/degraded)
                            if "Endpoint" not in tp:
                                logger.info(
                                    "DIAG: Transport for %s has no Endpoint — stale transport, "
                                    "triggering AVRCP renegotiation (attempt %d)",
                                    addr, attempts + 1,
                                )
                                needs_renegotiation = True

                            # Check B: no Volume signal within 15s of connection
                            elif (connect_time
                                  and time.time() - connect_time > 15
                                  and addr not in self._last_signaled_volume):
                                logger.info(
                                    "DIAG: No AVRCP volume signal for %s after %.0fs "
                                    "— triggering renegotiation (attempt %d)",
                                    addr, time.time() - connect_time, attempts + 1,
                                )
                                needs_renegotiation = True

                            if needs_renegotiation:
                                self._renegotiation_count[addr] = attempts + 1
                                asyncio.ensure_future(self._renegotiate_a2dp(addr))

                        break

                    if not found_transport:
                        logger.debug("DIAG: No MediaTransport1 for %s", addr)

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug("DIAG: Volume poll error: %s", e)

    async def _renegotiate_a2dp(self, address: str) -> None:
        """Force full AVRCP re-negotiation to restore Absolute Volume.

        Temporarily untrusts the device so BlueZ won't auto-accept a BLE
        reconnection during the disconnect gap (dual-mode speakers like
        Bose reconnect via LE before we can issue ConnectProfile for BREDR).

        If ConnectProfile lands on LE anyway (no transport), escalates
        through retry ConnectProfile → full disconnect/reconnect, mirroring
        the fallback logic in _ensure_a2dp_transport.

        Guards with _connecting + _suppress_reconnect to prevent
        _on_device_connected_async and the reconnect service from racing.
        """
        from .bluez.constants import A2DP_SINK_UUID

        device = self.managed_devices.get(address)
        if not device:
            return
        self._broadcast_status(f"Fixing volume control for {address}...")
        self._connecting.add(address)
        self._suppress_reconnect.add(address)
        try:
            # Temporarily untrust so BlueZ won't auto-accept BLE reconnection
            # during the disconnect gap (dual-mode speakers reconnect via LE
            # before we can issue ConnectProfile for BREDR)
            logger.info("AVRCP renegotiation: untrusting %s to prevent BLE auto-reconnect", address)
            try:
                await device.set_trusted(False)
            except Exception as e:
                logger.debug("Failed to untrust %s: %s", address, e)

            # Full device disconnect — tears down AVRCP + A2DP completely
            logger.info("AVRCP renegotiation: full disconnect %s...", address)
            await device.disconnect()
            await asyncio.sleep(2)

            # Reconnect via A2DP profile (ensures BREDR, full AVRCP negotiation)
            logger.info("AVRCP renegotiation: ConnectProfile(A2DP) for %s...", address)
            self._broadcast_status(f"Re-establishing audio for {address}...")
            await device.connect_profile(A2DP_SINK_UUID)

            # Restore trust immediately so normal operation continues
            try:
                await device.set_trusted(True)
            except Exception as e:
                logger.debug("Failed to re-trust %s: %s", address, e)

            # Give BlueZ time to set up transport + AVRCP
            await asyncio.sleep(3)

            has_transport = await self._log_transport_properties(address)

            # If no transport, ConnectProfile may have landed on LE —
            # try switching to A2DP (mirrors _ensure_a2dp_transport logic)
            if not has_transport:
                logger.info(
                    "AVRCP renegotiation: no transport after ConnectProfile for %s, "
                    "trying to switch from LE to A2DP...", address,
                )
                self._broadcast_status(f"Switching {address} to audio profile...")

                # Retry ConnectProfile (may activate A2DP on top of LE connection)
                try:
                    await device.connect_profile(A2DP_SINK_UUID)
                    await asyncio.sleep(3)
                    has_transport = await self._log_transport_properties(address)
                except Exception as e:
                    logger.debug("Retry ConnectProfile failed for %s: %s", address, e)

            if not has_transport:
                # Last resort: full disconnect/reconnect cycle to reset bearers
                logger.info(
                    "AVRCP renegotiation: A2DP still missing for %s, "
                    "trying full disconnect/reconnect cycle...", address,
                )
                self._broadcast_status(f"Full reconnect for {address}...")
                try:
                    await device.disconnect()
                    await asyncio.sleep(2)
                    await device.connect()
                    await device.wait_for_services(timeout=10)
                    await asyncio.sleep(3)
                    has_transport = await self._log_transport_properties(address)
                except Exception as e:
                    logger.warning("Full reconnect cycle failed for %s: %s", address, e)

            if not has_transport:
                logger.warning("AVRCP renegotiation: no transport after all attempts for %s", address)
                self._broadcast_status(f"Volume fix incomplete for {address} — try manual reconnect")
                await asyncio.sleep(3)
                return

            logger.info("AVRCP renegotiation transport OK for %s, waiting for PA sink...", address)
            self._broadcast_status(f"Waiting for audio sink for {address}...")

            sink_name = None
            if self.pulse:
                # First try: wait for PA to notice naturally
                sink_name = await self.pulse.wait_for_bt_sink(address, timeout=10)

                if not sink_name:
                    # PA missed the transport — activate per-device card profile
                    logger.info("PA sink not found, activating card profile for %s...", address)
                    self._broadcast_status(f"Activating audio profile for {address}...")
                    if await self.pulse.activate_bt_card_profile(address):
                        sink_name = await self.pulse.wait_for_bt_sink(address, timeout=15)

            # Verify MediaControl1 player link — confirms BlueZ wired our
            # MPRIS player into the fresh AVRCP session during ConnectProfile.
            await self._log_media_control_player(address)

            if sink_name:
                logger.info("AVRCP renegotiation succeeded for %s — sink %s", address, sink_name)
                self._broadcast_status(f"Volume control restored for {address}")
                if self.keepalive:
                    self.keepalive.set_target_sink(sink_name)
            else:
                logger.warning("AVRCP renegotiation: transport OK but no PA sink for %s", address)
                self._broadcast_status(f"Audio transport restored but no sink — try manual reconnect")
            await asyncio.sleep(3)
        except Exception as e:
            logger.warning("AVRCP renegotiation failed for %s: %s", address, e)
            self._broadcast_status(f"Volume fix failed for {address} — try manual reconnect")
            await asyncio.sleep(3)
        finally:
            # Always restore trust (safety net in case of early exception)
            try:
                await device.set_trusted(True)
            except Exception:
                pass
            self._connecting.discard(address)
            self._suppress_reconnect.discard(address)
            self.event_bus.emit("status", {"message": ""})
            await self._broadcast_all()

    # -- SSE broadcast helpers --

    async def _broadcast_devices(self) -> None:
        """Push full device list to all SSE clients."""
        try:
            devices = await self.get_all_devices()
            self.event_bus.emit("devices_changed", {"devices": devices})
        except Exception as e:
            logger.debug("Broadcast devices failed: %s", e)

    async def _broadcast_sinks(self) -> None:
        """Push full sink list to all SSE clients."""
        try:
            sinks = await self.get_audio_sinks()
            self._last_sink_snapshot = json.dumps(sinks, sort_keys=True)
            self.event_bus.emit("sinks_changed", {"sinks": sinks})
        except Exception as e:
            logger.debug("Broadcast sinks failed: %s", e)

    async def _broadcast_all(self) -> None:
        """Push both device and sink state to SSE clients."""
        await self._broadcast_devices()
        await self._broadcast_sinks()

    def _broadcast_status(self, message: str) -> None:
        """Push a status message to SSE clients."""
        self.event_bus.emit("status", {"message": message})

    def _on_device_disconnected(self, address: str) -> None:
        """Handle device disconnection event."""
        # Clean up volume tracking state (but NOT _volume_renegotiated —
        # that is only cleared by explicit user actions or startup init,
        # to prevent infinite renegotiation loops).
        self._device_connect_time.pop(address, None)
        self._last_polled_volume.pop(address, None)
        self._last_signaled_volume.pop(address, None)

        if address in self._suppress_reconnect:
            # User-initiated disconnect — don't auto-reconnect
            self._suppress_reconnect.discard(address)
            logger.info("Skipping auto-reconnect for %s (user-initiated disconnect)", address)
        elif self.reconnect_service:
            self.reconnect_service.handle_disconnect(address)
        asyncio.ensure_future(self._broadcast_all())

    def _on_device_connected(self, address: str) -> None:
        """Handle device connection event (D-Bus signal)."""
        # Track connection time for AVRCP volume renegotiation checks
        self._device_connect_time[address] = time.time()
        # Do NOT clear _volume_renegotiated here — organic reconnects after
        # a failed renegotiation would reset the flag and trigger another loop.
        # Only explicit user actions (connect_device) clear it.
        self._last_signaled_volume.pop(address, None)
        self._last_polled_volume.pop(address, None)
        asyncio.ensure_future(self._on_device_connected_async(address))

    async def _on_device_connected_async(self, address: str) -> None:
        """Async handler for device connection — broadcasts state and starts AVRCP."""
        await self._broadcast_all()
        # Try to subscribe to AVRCP after reconnection
        device = self.managed_devices.get(address)
        if device:
            try:
                await device.watch_media_player()
            except Exception as e:
                logger.debug("AVRCP watch on reconnect failed for %s: %s", address, e)

        # Disconnect HFP to force AVRCP volume (speakers send AT+VGS otherwise)
        await self._disconnect_hfp(address)

        # Skip A2DP transport setup if a manual connect is already handling it
        if address in self._connecting:
            logger.debug("Skipping auto A2DP setup for %s (manual connect in progress)", address)
            return

        # Check/activate A2DP transport (may need ConnectProfile)
        await self._ensure_a2dp_transport(address)

    async def _log_transport_properties(self, address: str) -> bool:
        """Enumerate BlueZ objects to find and log MediaTransport1 for a device.

        Waits briefly for the transport to appear (BlueZ may still be
        setting it up when the Connected signal fires).

        Returns True if a MediaTransport1 was found.
        """
        from .bluez.constants import BLUEZ_SERVICE, OBJECT_MANAGER_INTERFACE

        dev_fragment = address.replace(":", "_").upper()
        for attempt in range(3):
            if attempt > 0:
                await asyncio.sleep(2)
            try:
                intro = await self.bus.introspect(BLUEZ_SERVICE, "/")
                proxy = self.bus.get_proxy_object(BLUEZ_SERVICE, "/", intro)
                obj_mgr = proxy.get_interface(OBJECT_MANAGER_INTERFACE)
                objects = await obj_mgr.call_get_managed_objects()

                for path, ifaces in objects.items():
                    if dev_fragment not in path:
                        continue
                    if "org.bluez.MediaTransport1" not in ifaces:
                        continue
                    tp = ifaces["org.bluez.MediaTransport1"]
                    props = {}
                    for key, variant in tp.items():
                        props[key] = variant.value if hasattr(variant, "value") else str(variant)
                    vol_supported = "Volume" in tp
                    logger.info(
                        "MediaTransport1 for %s: path=%s volume_supported=%s props=%s",
                        address, path, vol_supported, props,
                    )
                    return True
            except Exception as e:
                logger.debug("Transport property check attempt %d failed: %s", attempt + 1, e)

        logger.info("No MediaTransport1 found for %s after 3 attempts", address)
        return False

    async def _refresh_avrcp_session(self, address: str) -> None:
        """Cycle AVRCP profiles to rebind the control channel to this process.

        After an add-on restart the old D-Bus unique name is gone, but the
        AVRCP session still references it.  Disconnecting and reconnecting
        the AVRCP profiles forces BlueZ to re-discover our newly registered
        MPRIS player without tearing down the A2DP audio stream.
        """
        from .bluez.constants import AVRCP_TARGET_UUID, AVRCP_CONTROLLER_UUID

        device = self.managed_devices.get(address)
        if not device:
            return

        logger.info("AVRCP refresh: cycling AVRCP profiles for %s...", address)

        # Disconnect AVRCP profiles (may not all be active)
        for uuid in (AVRCP_TARGET_UUID, AVRCP_CONTROLLER_UUID):
            try:
                await device.disconnect_profile(uuid)
            except Exception:
                pass
        await asyncio.sleep(1)

        # Reconnect AVRCP profiles
        for uuid in (AVRCP_TARGET_UUID, AVRCP_CONTROLLER_UUID):
            try:
                await device.connect_profile(uuid)
            except Exception:
                pass
        await asyncio.sleep(2)

        # Re-subscribe to the new AVRCP player node
        device.reset_avrcp_watch()
        try:
            await device.watch_media_player()
        except Exception as e:
            logger.debug("AVRCP watch after refresh for %s: %s", address, e)

        await self._log_media_control_player(address)

    # ---- Debug methods for interactive AVRCP testing ----

    async def debug_avrcp_cycle(self, address: str) -> dict:
        """Debug: cycle AVRCP profiles only (disconnect + reconnect)."""
        logger.info("[DEBUG] AVRCP Cycle for %s — start", address)
        await self._log_transport_properties(address)
        await self._log_media_control_player(address)
        await self._refresh_avrcp_session(address)
        await self._log_transport_properties(address)
        await self._log_media_control_player(address)
        logger.info("[DEBUG] AVRCP Cycle for %s — done", address)
        return {"action": "avrcp_cycle", "address": address}

    async def debug_mpris_reregister(self, address: str) -> dict:
        """Debug: unregister + re-register the MPRIS player."""
        logger.info("[DEBUG] MPRIS Re-register for %s — start", address)
        await self._log_transport_properties(address)
        await self._log_media_control_player(address)
        if self.media_player:
            await self.media_player.unregister()
            await asyncio.sleep(1)
            await self.media_player.register()
            await asyncio.sleep(2)
        else:
            logger.warning("[DEBUG] No media_player to re-register")
        await self._log_transport_properties(address)
        await self._log_media_control_player(address)
        logger.info("[DEBUG] MPRIS Re-register for %s — done", address)
        return {"action": "mpris_reregister", "address": address}

    async def debug_mpris_avrcp_cycle(self, address: str) -> dict:
        """Debug: unregister MPRIS, cycle AVRCP profiles, re-register MPRIS."""
        from .bluez.constants import AVRCP_TARGET_UUID, AVRCP_CONTROLLER_UUID

        logger.info("[DEBUG] MPRIS + AVRCP Cycle for %s — start", address)
        await self._log_transport_properties(address)
        await self._log_media_control_player(address)

        device = self.managed_devices.get(address)
        if not device:
            return {"action": "mpris_avrcp_cycle", "address": address, "error": "device not found"}

        # 1. Unregister MPRIS player
        if self.media_player:
            logger.info("[DEBUG] Unregistering MPRIS player...")
            await self.media_player.unregister()
        await asyncio.sleep(0.5)

        # 2. Disconnect AVRCP profiles
        for uuid in (AVRCP_TARGET_UUID, AVRCP_CONTROLLER_UUID):
            try:
                await device.disconnect_profile(uuid)
            except Exception:
                pass
        await asyncio.sleep(1)

        # 3. Re-register MPRIS player
        if self.media_player:
            logger.info("[DEBUG] Re-registering MPRIS player...")
            await self.media_player.register()
        await asyncio.sleep(0.5)

        # 4. Reconnect AVRCP profiles
        for uuid in (AVRCP_TARGET_UUID, AVRCP_CONTROLLER_UUID):
            try:
                await device.connect_profile(uuid)
            except Exception:
                pass
        await asyncio.sleep(2)

        # 5. Re-subscribe to AVRCP player node
        device.reset_avrcp_watch()
        try:
            await device.watch_media_player()
        except Exception as e:
            logger.debug("[DEBUG] AVRCP watch after cycle %s: %s", address, e)

        await self._log_transport_properties(address)
        await self._log_media_control_player(address)
        logger.info("[DEBUG] MPRIS + AVRCP Cycle for %s — done", address)
        return {"action": "mpris_avrcp_cycle", "address": address}

    async def debug_full_renegotiate(self, address: str) -> dict:
        """Debug: full disconnect + ConnectProfile(A2DP) renegotiation."""
        logger.info("[DEBUG] Full Renegotiate for %s — start", address)
        await self._log_transport_properties(address)
        await self._log_media_control_player(address)
        await self._renegotiate_a2dp(address)
        await self._log_transport_properties(address)
        await self._log_media_control_player(address)
        logger.info("[DEBUG] Full Renegotiate for %s — done", address)
        return {"action": "full_renegotiate", "address": address}

    async def debug_disconnect_hfp(self, address: str) -> dict:
        """Debug: disconnect HFP profile to force AVRCP volume."""
        logger.info("[DEBUG] Disconnect HFP for %s — start", address)
        result = await self._disconnect_hfp(address)
        logger.info("[DEBUG] Disconnect HFP for %s — done (success=%s)", address, result)
        return {"action": "disconnect_hfp", "address": address, "success": result}

    async def _disconnect_hfp(self, address: str) -> bool:
        """Disconnect HFP profile so the speaker uses AVRCP for volume control.

        Many speakers (e.g. Bose) send volume buttons as HFP AT+VGS commands
        instead of AVRCP absolute volume.  BlueZ doesn't map HFP volume to
        the A2DP MediaTransport, so the volume buttons appear dead.

        Disconnecting HFP forces the speaker to fall back to AVRCP volume,
        which BlueZ correctly propagates to MediaTransport1.Volume.
        """
        from .bluez.constants import HFP_UUID

        device = self.managed_devices.get(address)
        if not device:
            logger.debug("HFP disconnect: device %s not managed", address)
            return False

        try:
            await device.disconnect_profile(HFP_UUID)
            logger.info("HFP disconnected for %s — speaker should use AVRCP volume", address)
            return True
        except DBusError as e:
            err = str(e)
            if "Does Not Exist" in err or "NotConnected" in err:
                logger.debug("HFP not connected on %s (OK — already AVRCP-only)", address)
                return True
            logger.warning("HFP disconnect failed for %s: %s", address, e)
            return False

    async def _log_media_control_player(self, address: str) -> None:
        """Log whether BlueZ linked our MPRIS player to the device's AVRCP session."""
        from .bluez.constants import BLUEZ_SERVICE, OBJECT_MANAGER_INTERFACE

        dev_fragment = address.replace(":", "_").upper()
        try:
            intro = await self.bus.introspect(BLUEZ_SERVICE, "/")
            proxy = self.bus.get_proxy_object(BLUEZ_SERVICE, "/", intro)
            obj_mgr = proxy.get_interface(OBJECT_MANAGER_INTERFACE)
            objects = await obj_mgr.call_get_managed_objects()

            for path, ifaces in objects.items():
                if dev_fragment not in path:
                    continue
                if "org.bluez.MediaControl1" not in ifaces:
                    continue
                mc = ifaces["org.bluez.MediaControl1"]
                connected = mc.get("Connected")
                player = mc.get("Player")
                connected_val = connected.value if hasattr(connected, "value") else connected
                player_val = player.value if hasattr(player, "value") else player
                logger.info(
                    "MediaControl1 for %s: Connected=%s Player=%s",
                    address, connected_val, player_val,
                )
                return
            logger.info("No MediaControl1 found for %s", address)
        except Exception as e:
            logger.debug("MediaControl1 check failed for %s: %s", address, e)

    async def _ensure_a2dp_transport(self, address: str) -> bool:
        """Check for A2DP transport and try ConnectProfile if missing.

        When a device auto-reconnects (e.g. Bose speaker initiating after
        disconnect), it may connect only the BLE bearer without activating
        the A2DP audio profile.  Calling ConnectProfile with the A2DP Sink
        UUID explicitly tells BlueZ to set up the audio transport.

        Returns True if a transport exists or was successfully activated.
        """
        from .bluez.constants import A2DP_SINK_UUID

        # First check: transport may already exist
        if await self._log_transport_properties(address):
            return True

        # Log device UUIDs to confirm A2DP is advertised
        device = self.managed_devices.get(address)
        if not device:
            return False

        uuids = await device.get_uuids()
        logger.info("Device %s UUIDs: %s", address, uuids)

        has_a2dp = any("110b" in u.lower() for u in uuids)
        if not has_a2dp:
            logger.warning(
                "Device %s does not advertise A2DP Sink UUID — cannot activate audio",
                address,
            )
            return False

        # Try ConnectProfile to explicitly activate A2DP
        logger.info("No A2DP transport for %s, trying ConnectProfile(A2DP_SINK)...", address)
        try:
            await device.connect_profile(A2DP_SINK_UUID)
            await asyncio.sleep(3)
            if await self._log_transport_properties(address):
                return True
        except Exception as e:
            logger.warning("ConnectProfile(A2DP) failed for %s: %s", address, e)

        # ConnectProfile failed or didn't produce a transport — the device
        # is likely stuck in BLE-only mode (no BR/EDR bearer).  A full
        # disconnect + reconnect cycle resets the radio link and lets BlueZ
        # establish both bearers properly.
        logger.info(
            "A2DP still missing for %s, trying full disconnect/reconnect cycle...",
            address,
        )
        try:
            await device.disconnect()
            await asyncio.sleep(2)
            await device.connect()
            await device.wait_for_services(timeout=10)
            await asyncio.sleep(3)
            return await self._log_transport_properties(address)
        except Exception as e:
            logger.warning("Disconnect/reconnect cycle failed for %s: %s", address, e)
            return False

    def _on_pa_volume_change(self, sink_name: str, volume: int, mute: bool) -> None:
        """Handle PulseAudio Bluetooth sink volume change (AVRCP Absolute Volume)."""
        # Extract address from sink name like bluez_sink.XX_XX_XX_XX_XX_XX.a2dp_sink
        parts = sink_name.split(".")
        addr = parts[1].replace("_", ":") if len(parts) >= 2 else ""
        value = f"{volume}% (muted)" if mute else f"{volume}%"
        entry = {"address": addr, "property": "Volume", "value": value, "ts": time.time()}
        self.recent_avrcp.append(entry)
        self.event_bus.emit("avrcp_event", entry)

    def _on_avrcp_command(self, command: str, detail: str) -> None:
        """Handle MPRIS command from speaker buttons (via registered MPRIS player)."""
        entry = {"command": command, "detail": detail, "ts": time.time()}
        self.recent_mpris.append(entry)
        self.event_bus.emit("mpris_command", entry)

    def _on_avrcp_event(self, address: str, prop_name: str, value: object) -> None:
        """Handle AVRCP MediaPlayer1 property change — push to SSE."""
        # Convert value to JSON-safe representation
        if isinstance(value, dict):
            safe_val = {k: str(v) for k, v in value.items()}
        else:
            safe_val = str(value) if not isinstance(value, (str, int, float, bool)) else value
        entry = {"address": address, "property": prop_name, "value": safe_val, "ts": time.time()}
        self.recent_avrcp.append(entry)
        self.event_bus.emit("avrcp_event", entry)
