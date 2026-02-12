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
        self._keepalives: dict[str, KeepAliveService] = {}  # per-device keep-alive
        self.media_player: AVRCPMediaPlayer | None = None
        self.managed_devices: dict[str, BluezDevice] = {}
        self._web_server = None
        self.event_bus = EventBus()
        self._sink_poll_task: asyncio.Task | None = None
        self._last_sink_snapshot: str = ""
        self._last_signaled_volume: dict[str, int] = {}  # addr → raw 0-127
        self._device_connect_time: dict[str, float] = {}  # addr → time.time()
        self._connecting: set[str] = set()  # addrs with connection in progress
        self._suppress_reconnect: set[str] = set()  # addresses with user-initiated disconnect
        self._a2dp_attempts: dict[str, int] = {}  # addr → consecutive A2DP activation failures
        # Ring buffers so WebSocket clients get recent events on reconnect
        self.recent_mpris: collections.deque = collections.deque(maxlen=self.MAX_RECENT_EVENTS)
        self.recent_avrcp: collections.deque = collections.deque(maxlen=self.MAX_RECENT_EVENTS)
        # Scanning state
        self._scanning: bool = False
        self._scan_task: asyncio.Task | None = None
        self._scan_debounce_handle: asyncio.TimerHandle | None = None

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
                and msg.member == "InterfacesAdded"
                and msg.path == "/"
                and self._scanning
                and msg.body
                and len(msg.body) >= 2
            ):
                # ObjectManager.InterfacesAdded — new device discovered
                obj_path = msg.body[0]
                ifaces = msg.body[1]
                if (
                    isinstance(obj_path, str)
                    and obj_path.startswith("/org/bluez/")
                    and isinstance(ifaces, dict)
                    and "org.bluez.Device1" in ifaces
                ):
                    logger.info("New device discovered during scan: %s", obj_path)
                    self._schedule_scan_broadcast()
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

                    # During scanning, broadcast when UUIDs or Name change
                    # (UUIDs often arrive after InterfacesAdded)
                    if (
                        self._scanning
                        and iface_name == "org.bluez.Device1"
                        and {"UUIDs", "Name"}.intersection(prop_names)
                    ):
                        self._schedule_scan_broadcast()

                    if iface_name == "org.bluez.MediaTransport1":
                        if "Volume" in changed:
                            vol_raw = changed["Volume"].value  # 0-127 uint16
                            vol_pct = round(vol_raw / 127 * 100)
                            logger.info("AVRCP transport volume: %d%% (raw %d)", vol_pct, vol_raw)
                            parts = msg.path.split("/")
                            addr = next((p[4:].replace("_", ":") for p in parts if p.startswith("dev_")), "")
                            self._last_signaled_volume[addr] = vol_raw
                            entry = {"address": addr, "property": "Volume", "value": f"{vol_pct}%", "ts": time.time()}
                            self.recent_avrcp.append(entry)
                            self.event_bus.emit("avrcp_event", entry)
                        if "State" in changed:
                            state = changed["State"].value
                            # Tell speaker we're Playing so it enables AVRCP volume buttons
                            if self.media_player and state == "active":
                                self.media_player.set_playback_status("Playing")
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

        # 3c. Register null HFP handler to prevent HFP from being established
        #     Speakers like Bose send volume buttons as HFP AT+VGS commands
        #     instead of AVRCP.  Blocking HFP forces AVRCP volume.
        await self._register_null_hfp_handler()

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
                    logger.info("Device %s already connected at startup", addr)
                    self._last_signaled_volume.pop(addr, None)
                    self._device_connect_time[addr] = time.time()
                    # Disconnect any pre-existing HFP (null handler blocks new ones)
                    await self._disconnect_hfp(addr)
            except DBusError as e:
                logger.debug("Could not initialize stored device %s: %s", addr, e)

        # 6a. Clean up stale BlueZ device cache — remove unpaired, disconnected
        #     device objects that aren't in our persistent store.  These are
        #     leftover from previous discovery sessions and would otherwise show
        #     as "DISCOVERED" in the UI even when the device is powered off.
        try:
            from .bluez.constants import BLUEZ_SERVICE, OBJECT_MANAGER_INTERFACE, DEVICE_INTERFACE, AUDIO_UUIDS
            intro = await self.bus.introspect(BLUEZ_SERVICE, "/")
            proxy = self.bus.get_proxy_object(BLUEZ_SERVICE, "/", intro)
            obj_mgr = proxy.get_interface(OBJECT_MANAGER_INTERFACE)
            objects = await obj_mgr.call_get_managed_objects()
            stored_addrs = {d["address"] for d in self.store.devices}

            for path, ifaces in objects.items():
                if DEVICE_INTERFACE not in ifaces:
                    continue
                dev_props = ifaces[DEVICE_INTERFACE]
                addr_v = dev_props.get("Address")
                paired_v = dev_props.get("Paired")
                connected_v = dev_props.get("Connected")
                uuids_v = dev_props.get("UUIDs")
                if not addr_v:
                    continue
                addr = addr_v.value if hasattr(addr_v, "value") else addr_v
                paired = (paired_v.value if hasattr(paired_v, "value") else paired_v) if paired_v else False
                connected = (connected_v.value if hasattr(connected_v, "value") else connected_v) if connected_v else False
                uuids = set(uuids_v.value) if uuids_v else set()

                # Only clean up audio devices not in our store, not paired, not connected
                if addr in stored_addrs or paired or connected:
                    continue
                if not uuids.intersection(AUDIO_UUIDS):
                    continue

                adapter_path = path[: path.rfind("/")]
                try:
                    a_intr = await self.bus.introspect(BLUEZ_SERVICE, adapter_path)
                    a_proxy = self.bus.get_proxy_object(BLUEZ_SERVICE, adapter_path, a_intr)
                    from .bluez.constants import ADAPTER_INTERFACE
                    a_iface = a_proxy.get_interface(ADAPTER_INTERFACE)
                    await a_iface.call_remove_device(path)
                    logger.info("Removed stale cached device %s from %s", addr, adapter_path)
                except Exception as e:
                    logger.debug("Could not remove stale device %s: %s", addr, e)
        except Exception as e:
            logger.debug("Stale device cleanup failed: %s", e)

        # 6b. Detect devices connected at the BlueZ level but NOT in our
        #     store (e.g. store wiped during rebuild, or device paired outside
        #     the add-on).  Create BluezDevice wrappers so UI buttons work.
        try:
            from .bluez.constants import BLUEZ_SERVICE, OBJECT_MANAGER_INTERFACE, DEVICE_INTERFACE
            intro = await self.bus.introspect(BLUEZ_SERVICE, "/")
            proxy = self.bus.get_proxy_object(BLUEZ_SERVICE, "/", intro)
            obj_mgr = proxy.get_interface(OBJECT_MANAGER_INTERFACE)
            objects = await obj_mgr.call_get_managed_objects()

            for path, ifaces in objects.items():
                if DEVICE_INTERFACE not in ifaces:
                    continue
                if not path.startswith(self._adapter_path + "/"):
                    continue
                dev_props = ifaces[DEVICE_INTERFACE]
                addr_v = dev_props.get("Address")
                connected_v = dev_props.get("Connected")
                if not addr_v or not connected_v:
                    continue
                addr = addr_v.value if hasattr(addr_v, "value") else addr_v
                connected = connected_v.value if hasattr(connected_v, "value") else connected_v
                if not connected or addr in self.managed_devices:
                    continue

                logger.info(
                    "Found connected device %s not in managed_devices — initializing",
                    addr,
                )
                try:
                    device = await self._get_or_create_device(addr)
                    self._device_connect_time[addr] = time.time()
                    await self._disconnect_hfp(addr)
                except Exception as e:
                    logger.debug("Could not initialize unmanaged device %s: %s", addr, e)
        except Exception as e:
            logger.debug("Failed to enumerate connected BlueZ devices: %s", e)

        # 6c. If any device already has an active A2DP transport, signal
        #     PlaybackStatus=Playing so the speaker enables AVRCP volume buttons
        #     immediately (we won't see a State transition signal for transports
        #     that were already active before we started).
        if self.media_player:
            try:
                from .bluez.constants import BLUEZ_SERVICE, OBJECT_MANAGER_INTERFACE
                intro = await self.bus.introspect(BLUEZ_SERVICE, "/")
                proxy = self.bus.get_proxy_object(BLUEZ_SERVICE, "/", intro)
                obj_mgr = proxy.get_interface(OBJECT_MANAGER_INTERFACE)
                objects = await obj_mgr.call_get_managed_objects()
                for path, ifaces in objects.items():
                    if "org.bluez.MediaTransport1" not in ifaces:
                        continue
                    tp = ifaces["org.bluez.MediaTransport1"]
                    state_v = tp.get("State")
                    state = state_v.value if hasattr(state_v, "value") else state_v
                    if state == "active":
                        logger.info("Active A2DP transport found at startup (%s) — setting PlaybackStatus=Playing", path)
                        self.media_player.set_playback_status("Playing")
                        break
            except Exception as e:
                logger.debug("Could not check transport state at startup: %s", e)

        # 7. Start reconnection service
        self.reconnect_service = ReconnectService(self)
        await self.reconnect_service.start()

        # 8. Reconnect stored devices that aren't already connected
        await self.reconnect_service.reconnect_all()

        # 9. Migrate global keep-alive → per-device (one-time for upgrading users)
        await self._migrate_global_keepalive()

        # 9b. Start keep-alive for any connected devices that have it enabled
        for device_info in self.store.devices:
            addr = device_info["address"]
            if addr in self._device_connect_time:
                await self._start_keepalive_if_enabled(addr)

        # 10. Start periodic sink state polling
        self._sink_poll_task = asyncio.create_task(self._sink_poll_loop())

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

        # Cancel any running scan
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass

        # Stop all per-device keep-alive instances
        for addr in list(self._keepalives):
            await self._stop_keepalive(addr)

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

    async def _find_device_adapter(self, address: str) -> str | None:
        """Query BlueZ ObjectManager to find which adapter a device is on.

        Returns the adapter D-Bus path (e.g. '/org/bluez/hci0') or None.
        Prefers the configured adapter if the device exists on multiple adapters.
        """
        from .bluez.constants import BLUEZ_SERVICE, OBJECT_MANAGER_INTERFACE

        dev_suffix = f"/dev_{address.replace(':', '_')}"
        try:
            intro = await self.bus.introspect(BLUEZ_SERVICE, "/")
            proxy = self.bus.get_proxy_object(BLUEZ_SERVICE, "/", intro)
            obj_mgr = proxy.get_interface(OBJECT_MANAGER_INTERFACE)
            objects = await obj_mgr.call_get_managed_objects()

            found_adapters = []
            for path in objects:
                if path.endswith(dev_suffix):
                    adapter_path = path[: path.rfind("/")]
                    found_adapters.append(adapter_path)

            if not found_adapters:
                return None
            # Prefer the configured adapter if device exists there
            if self._adapter_path in found_adapters:
                return self._adapter_path
            return found_adapters[0]
        except Exception as e:
            logger.debug("_find_device_adapter for %s: %s", address, e)
            return None

    async def _get_or_create_device(self, address: str) -> BluezDevice:
        """Get an existing managed device or create and register a new one.

        Ensures only one BluezDevice (and one D-Bus subscription) exists per address.
        Discovers the actual adapter the device is on via ObjectManager rather
        than assuming the configured adapter.
        """
        device = self.managed_devices.get(address)
        if device:
            return device

        # Discover actual adapter (device may be paired on a different adapter)
        actual_adapter = await self._find_device_adapter(address)
        adapter_path = actual_adapter or self._adapter_path
        if actual_adapter and actual_adapter != self._adapter_path:
            logger.info(
                "Device %s is on %s (configured: %s)",
                address, actual_adapter, self._adapter_path,
            )

        device = BluezDevice(self.bus, address, adapter_path)
        await device.initialize()
        device.on_disconnected(self._on_device_disconnected)
        device.on_connected(self._on_device_connected)
        device.on_avrcp_event(self._on_avrcp_event)
        self.managed_devices[address] = device
        return device

    SCAN_DEBOUNCE_SECONDS = 1.0  # coalesce rapid D-Bus signals during scan

    async def scan_devices(self, duration: int | None = None) -> None:
        """Start a background discovery scan for A2DP audio devices.

        Returns immediately. Devices appear incrementally via WebSocket
        'devices_changed' events. A 'scan_finished' event is emitted when done.
        """
        duration = duration or self.config.scan_duration_seconds

        # If already scanning, cancel the old scan and start fresh
        if self._scanning and self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
            await self.adapter.stop_discovery()

        self._scanning = True
        self.event_bus.emit("scan_started", {"duration": duration})
        self._scan_task = asyncio.create_task(self._run_scan(duration))

    async def _run_scan(self, duration: int) -> None:
        """Background scan task: runs discovery for *duration* seconds."""
        try:
            await self.adapter.start_discovery()
            await asyncio.sleep(duration)
        except asyncio.CancelledError:
            logger.info("Scan cancelled")
            return
        except Exception as e:
            logger.error("Scan failed: %s", e)
            self.event_bus.emit("scan_finished", {"error": str(e)})
            return
        finally:
            self._scanning = False
            self._cancel_scan_debounce()
            try:
                await self.adapter.stop_discovery()
            except Exception:
                pass

        # Final broadcast after scan completes
        await self._broadcast_devices()
        self.event_bus.emit("scan_finished", {})

    def _schedule_scan_broadcast(self) -> None:
        """Schedule a debounced device broadcast during scanning.

        If a broadcast is already scheduled, this is a no-op.
        Ensures we don't flood ObjectManager queries on every D-Bus signal.
        """
        if not self._scanning:
            return
        if self._scan_debounce_handle and not self._scan_debounce_handle.cancelled():
            return  # already scheduled
        loop = asyncio.get_event_loop()
        self._scan_debounce_handle = loop.call_later(
            self.SCAN_DEBOUNCE_SECONDS,
            lambda: asyncio.ensure_future(self._debounced_scan_broadcast()),
        )

    async def _debounced_scan_broadcast(self) -> None:
        """Execute the debounced broadcast."""
        self._scan_debounce_handle = None
        if self._scanning:
            await self._broadcast_devices()

    def _cancel_scan_debounce(self) -> None:
        """Cancel any pending debounced broadcast."""
        if self._scan_debounce_handle and not self._scan_debounce_handle.cancelled():
            self._scan_debounce_handle.cancel()
        self._scan_debounce_handle = None

    @property
    def is_scanning(self) -> bool:
        """Whether a background scan is currently in progress."""
        return self._scanning

    async def pair_device(self, address: str) -> dict:
        """Pair, trust, persist, and connect a Bluetooth audio device."""
        self._broadcast_status(f"Pairing with {address}...")
        self._a2dp_attempts.pop(address, None)  # fresh pair — reset retry counter
        # Mark as connecting early so the Connected signal fired during pair()
        # doesn't race with connect_device() and double-fire HFP disconnect.
        self._connecting.add(address)
        try:
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
            # _connecting is already set; pass _from_pair so connect_device
            # skips the duplicate-connection guard.
            connected = await self.connect_device(address, _from_pair=True)
            return {"address": address, "name": name, "connected": connected}
        except Exception:
            self._connecting.discard(address)
            self.event_bus.emit("status", {"message": ""})
            raise

    async def connect_device(self, address: str, *, _from_pair: bool = False) -> bool:
        """Connect to a paired device and verify A2DP sink appears."""
        # If another connection attempt is already in progress, wait for it
        if not _from_pair and address in self._connecting:
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

            # Verify PulseAudio sink appeared
            if self.pulse:
                self._broadcast_status(f"Waiting for A2DP sink for {address}...")
                sink_name = await self.pulse.wait_for_bt_sink(address, timeout=15)
                if sink_name:
                    # Disconnect HFP only AFTER A2DP is up — doing it earlier
                    # can cause the speaker to drop the entire connection when
                    # HFP is the only active profile.
                    await self._disconnect_hfp(address)
                    await self._start_keepalive_if_enabled(address)
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
            self.event_bus.emit("status", {"message": ""})

    async def disconnect_device(self, address: str) -> None:
        """Disconnect a device without removing it from the store."""
        self._broadcast_status(f"Disconnecting {address}...")
        # Cancel any pending reconnection
        if self.reconnect_service:
            self.reconnect_service.cancel_reconnect(address)

        # Suppress auto-reconnect for this user-initiated disconnect
        self._suppress_reconnect.add(address)

        try:
            device = await self._get_or_create_device(address)
            await device.disconnect()
        except Exception as e:
            logger.warning("Disconnect failed for %s: %s", address, e)
        self.event_bus.emit("status", {"message": ""})
        await self._broadcast_all()

    async def force_reconnect_device(self, address: str) -> bool:
        """Force disconnect + reconnect cycle (recovery for zombie connections)."""
        self._broadcast_status(f"Force reconnecting {address}...")
        try:
            await self.disconnect_device(address)
        except Exception as e:
            logger.warning(
                "Force reconnect: disconnect failed for %s: %s (continuing)",
                address, e,
            )

        self._broadcast_status(f"Waiting for {address} to reset...")
        await asyncio.sleep(10)

        self._broadcast_status(f"Reconnecting to {address}...")
        return await self.connect_device(address)

    async def forget_device(self, address: str) -> None:
        """Unpair, remove from BlueZ, and delete from persistent store."""
        self._broadcast_status(f"Forgetting {address}...")
        self._a2dp_attempts.pop(address, None)
        # Cancel reconnection
        if self.reconnect_service:
            self.reconnect_service.cancel_reconnect(address)

        # Disconnect and clean up D-Bus subscriptions
        device = self.managed_devices.pop(address, None)
        if not device:
            # Device not in managed_devices — try to create a temporary wrapper
            try:
                device = await self._get_or_create_device(address)
                self.managed_devices.pop(address, None)  # don't keep it
            except Exception:
                pass
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
        self.event_bus.emit("status", {"message": ""})
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
            addr = device["address"]
            device["stored"] = addr in stored_addresses
            if device["stored"]:
                s = self.store.get_device_settings(addr)
                device["keep_alive_enabled"] = s["keep_alive_enabled"]
                device["keep_alive_method"] = s["keep_alive_method"]
                device["keep_alive_active"] = addr in self._keepalives

        # Add stored devices not currently visible
        discovered_addresses = {d["address"] for d in discovered}
        for stored in self.store.devices:
            addr = stored["address"]
            if addr not in discovered_addresses:
                s = self.store.get_device_settings(addr)
                discovered.append(
                    {
                        "address": addr,
                        "name": stored["name"],
                        "paired": True,
                        "connected": False,
                        "rssi": None,
                        "stored": True,
                        "uuids": [],
                        "bearers": [],
                        "has_transport": False,
                        "adapter": "",
                        "keep_alive_enabled": s["keep_alive_enabled"],
                        "keep_alive_method": s["keep_alive_method"],
                        "keep_alive_active": addr in self._keepalives,
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
                    # Try match by real USB ID from sysfs (preferred),
                    # fall back to modalias-derived ID (less reliable in Docker)
                    usb_id = a.get("usb_id") or self._modalias_to_usb_id(a["modalias"])
                    if usb_id and usb_id in usb_names:
                        a["hw_model"] = usb_names[usb_id]

        for a in adapters:
            a["selected"] = a["path"] == self._adapter_path
            a["ble_scanning"] = a["discovering"] and not a["selected"]
        return adapters

    @staticmethod
    async def _get_supervisor_usb_names() -> dict[str, str]:
        """Query the HA Supervisor hardware API for USB device names.

        Returns mappings keyed by:
        - USB id (vendor:product, lowercase) → device description
        - hci adapter name (hci:hciX) → device description

        The hci mapping is built by cross-referencing bluetooth devices
        (which have hci names but no USB product info) with their parent
        USB devices (which have product info but no hci names) via sysfs
        path prefix matching.
        """
        import aiohttp
        import re
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            logger.warning("SUPERVISOR_TOKEN not set — cannot query hardware API")
            return {}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "http://supervisor/hardware/info",
                    headers={"Authorization": f"Bearer {token}"},
                ) as resp:
                    if resp.status != 200:
                        logger.warning("Supervisor hardware API returned %s", resp.status)
                        return {}
                    result = await resp.json()

            devices = result.get("data", {}).get("devices", [])
            names: dict[str, str] = {}
            # sysfs path → (usb_id, full_name) for parent-path matching
            usb_by_path: dict[str, tuple[str, str]] = {}

            # Pass 1: collect USB devices with vendor/product info
            for dev in devices:
                attrs = dev.get("attributes", {})
                sysfs = dev.get("sysfs", "")
                dev_name = dev.get("name", "")

                vid = attrs.get("ID_VENDOR_ID", "")
                pid = attrs.get("ID_MODEL_ID", "")
                if not (vid and pid):
                    continue

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

                usb_id = f"{vid.lower()}:{pid.lower()}"
                names[usb_id] = full_name
                if sysfs:
                    usb_by_path[sysfs] = (usb_id, full_name)

            # Pass 2: find bluetooth/hci devices and map to parent USB names
            for dev in devices:
                sysfs = dev.get("sysfs", dev.get("by_id", ""))
                subsystem = dev.get("subsystem", "")
                dev_name = dev.get("name", "")

                if subsystem != "bluetooth" and "bluetooth" not in sysfs.lower():
                    continue

                # Extract hci name from sysfs path or device name
                m = re.search(r"(hci\d+)", sysfs) or re.search(r"(hci\d+)", dev_name)
                if not m:
                    continue
                hci_name = m.group(1)
                hci_key = f"hci:{hci_name}"

                # Check if this device itself has vendor/product info
                attrs = dev.get("attributes", {})
                vid = attrs.get("ID_VENDOR_ID", "")
                pid = attrs.get("ID_MODEL_ID", "")
                if vid and pid:
                    usb_id = f"{vid.lower()}:{pid.lower()}"
                    if usb_id in names:
                        names[hci_key] = names[usb_id]
                        continue

                # Find parent USB device by sysfs path prefix
                if sysfs:
                    for usb_sysfs, (_uid, usb_name) in usb_by_path.items():
                        if sysfs.startswith(usb_sysfs + "/"):
                            names[hci_key] = usb_name
                            break

            logger.info("Supervisor HW names: %s", names)
            if not any(k.startswith("hci:") for k in names):
                for dev in devices[:30]:
                    logger.info("Supervisor HW device: subsystem=%s name=%s sysfs=%s attrs=%s",
                                dev.get("subsystem"), dev.get("name"),
                                dev.get("sysfs", dev.get("by_id", "")),
                                {k: v for k, v in dev.get("attributes", {}).items()
                                 if any(kw in k.upper() for kw in ("VENDOR", "MODEL", "PRODUCT", "ID_"))})
            return names
        except Exception as e:
            logger.warning("Failed to query Supervisor hardware API: %s", e)
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
        """Push both device and sink state to WebSocket clients."""
        await self._broadcast_devices()
        await self._broadcast_sinks()

    def _broadcast_status(self, message: str) -> None:
        """Push a status message to WebSocket clients."""
        self.event_bus.emit("status", {"message": message})

    def _on_device_disconnected(self, address: str) -> None:
        """Handle device disconnection event."""
        self._device_connect_time.pop(address, None)
        self._last_signaled_volume.pop(address, None)
        asyncio.ensure_future(self._stop_keepalive(address))

        if address in self._suppress_reconnect:
            # User-initiated disconnect — don't auto-reconnect
            self._suppress_reconnect.discard(address)
            logger.info("Skipping auto-reconnect for %s (user-initiated disconnect)", address)
        elif self.reconnect_service:
            self.reconnect_service.handle_disconnect(address)
        asyncio.ensure_future(self._broadcast_all())

    def _on_device_connected(self, address: str) -> None:
        """Handle device connection event (D-Bus signal)."""
        self._device_connect_time[address] = time.time()
        self._last_signaled_volume.pop(address, None)
        asyncio.ensure_future(self._on_device_connected_async(address))

    async def _on_device_connected_async(self, address: str) -> None:
        """Async handler for device connection — broadcasts state and starts AVRCP."""
        await self._broadcast_all()

        # If a connect or HFP reconnect cycle is in progress, don't interfere
        if address in self._connecting:
            logger.debug("Skipping auto setup for %s (connect/cycle in progress)", address)
            return

        # Try to subscribe to AVRCP after reconnection
        try:
            device = await self._get_or_create_device(address)
            try:
                await device.watch_media_player()
            except Exception as e:
                logger.debug("AVRCP watch on reconnect failed for %s: %s", address, e)
        except Exception as e:
            logger.debug("Cannot access reconnected device %s: %s", address, e)

        # Disconnect HFP to force AVRCP volume (speakers send AT+VGS otherwise)
        await self._disconnect_hfp(address)

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

        try:
            device = await self._get_or_create_device(address)
        except Exception as e:
            logger.warning("AVRCP refresh: cannot access device %s: %s", address, e)
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

        try:
            device = await self._get_or_create_device(address)
        except Exception as e:
            return {"action": "mpris_avrcp_cycle", "address": address, "error": str(e)}

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

    async def debug_disconnect_hfp(self, address: str) -> dict:
        """Debug: disconnect HFP profile to force AVRCP volume."""
        logger.info("[DEBUG] Disconnect HFP for %s — start", address)
        result = await self._disconnect_hfp(address)
        logger.info("[DEBUG] Disconnect HFP for %s — done (success=%s)", address, result)
        return {"action": "disconnect_hfp", "address": address, "success": result}

    async def debug_hfp_reconnect_cycle(self, address: str) -> dict:
        """Debug: disconnect HFP, then full device disconnect + reconnect."""
        logger.info("[DEBUG] HFP Reconnect Cycle for %s — start", address)
        await self._log_transport_properties(address)
        await self._log_media_control_player(address)
        await self._hfp_reconnect_cycle(address)
        await self._log_transport_properties(address)
        await self._log_media_control_player(address)
        logger.info("[DEBUG] HFP Reconnect Cycle for %s — done", address)
        return {"action": "hfp_reconnect_cycle", "address": address}

    async def _hfp_reconnect_cycle(self, address: str) -> None:
        """Disconnect and reconnect without HFP for AVRCP volume control.

        Speakers like Bose cache HFP as the volume path for the current
        Bluetooth session.  Simply disconnecting HFP mid-session doesn't
        make the speaker switch to AVRCP — a full reconnect cycle forces
        a fresh session where the speaker discovers HFP is gone and falls
        back to AVRCP absolute volume.
        """
        from .bluez.constants import HFP_UUID

        logger.info("HFP reconnect cycle for %s — start", address)

        try:
            device = await self._get_or_create_device(address)
        except Exception as e:
            logger.warning("HFP cycle: cannot access device %s: %s", address, e)
            return

        # Guard against _on_device_connected_async and reconnect service racing
        self._connecting.add(address)
        self._suppress_reconnect.add(address)
        try:
            # 1. Disconnect HFP
            try:
                await device.disconnect_profile(HFP_UUID)
                logger.info("HFP cycle: HFP disconnected for %s", address)
            except DBusError as e:
                logger.debug("HFP cycle: HFP disconnect for %s: %s", address, e)

            await asyncio.sleep(0.5)

            # 2. Full device disconnect
            logger.info("HFP cycle: disconnecting device %s", address)
            await device.disconnect()
            await asyncio.sleep(3)

            # 3. Reconnect
            logger.info("HFP cycle: reconnecting device %s", address)
            try:
                await device.connect()
            except DBusError as e:
                logger.warning("HFP cycle: reconnect failed for %s: %s", address, e)
                return

            # 4. Wait for services
            await device.wait_for_services(timeout=10)
            await asyncio.sleep(2)

            # 5. Disconnect HFP again (device.connect() reconnects all profiles)
            try:
                await device.disconnect_profile(HFP_UUID)
                logger.info("HFP cycle: HFP disconnected after reconnect for %s", address)
            except DBusError as e:
                err = str(e)
                if "Does Not Exist" in err or "NotConnected" in err:
                    logger.info("HFP cycle: HFP not present after reconnect for %s — good!", address)
                else:
                    logger.warning("HFP cycle: HFP disconnect after reconnect for %s: %s", address, e)

            await asyncio.sleep(1)

            # 6. Set up AVRCP watch
            device.reset_avrcp_watch()
            try:
                await device.watch_media_player()
            except Exception as e:
                logger.debug("HFP cycle: AVRCP watch for %s: %s", address, e)

            # 7. Ensure A2DP transport
            await self._ensure_a2dp_transport(address)

            # 8. Set up PA sink
            if self.pulse:
                sink_name = await self.pulse.get_sink_for_address(address)
                if sink_name:
                    logger.info("HFP cycle: PA sink for %s: %s", address, sink_name)
                    await self._start_keepalive_if_enabled(address)

        except DBusError as e:
            logger.warning("HFP cycle: failed for %s: %s", address, e)
        except Exception as e:
            logger.warning("HFP cycle: unexpected error for %s: %s", address, e)
        finally:
            self._connecting.discard(address)
            self._suppress_reconnect.discard(address)

        logger.info("HFP reconnect cycle for %s — done", address)

    async def _register_null_hfp_handler(self) -> None:
        """Register a null HFP profile handler to block HFP connections.

        By registering as the HFP handler via ProfileManager1, BlueZ routes
        HFP connection attempts to us instead of its built-in handler.  We
        reject them by closing the fd, so HFP is never established and the
        speaker must use AVRCP for volume control.
        """
        from .bluez.constants import HFP_UUID, BLUEZ_SERVICE
        from dbus_next.service import ServiceInterface, method
        from dbus_next import Variant

        profile_path = "/org/ha/bluetooth_audio/null_hfp"

        class NullHFPProfile(ServiceInterface):
            """Null HFP profile — rejects all connections."""

            def __init__(self):
                super().__init__("org.bluez.Profile1")

            @method()
            def Release(self):
                logger.info("[NullHFP] Profile released by BlueZ")

            @method()
            def NewConnection(self, device: 'o', fd: 'h', fd_properties: 'a{sv}'):
                logger.info("[NullHFP] Rejecting HFP connection from %s", device)
                import os
                try:
                    os.close(fd)
                except OSError:
                    pass

            @method()
            def RequestDisconnection(self, device: 'o'):
                logger.info("[NullHFP] Disconnect requested for %s", device)

        try:
            if profile_path not in self.bus._path_exports:
                self.bus.export(profile_path, NullHFPProfile())

            intro = await self.bus.introspect(BLUEZ_SERVICE, "/org/bluez")
            proxy = self.bus.get_proxy_object(BLUEZ_SERVICE, "/org/bluez", intro)
            profile_mgr = proxy.get_interface("org.bluez.ProfileManager1")

            await profile_mgr.call_register_profile(
                profile_path,
                HFP_UUID,
                {
                    "Name": Variant("s", "Null HFP"),
                    "Role": Variant("s", "client"),
                },
            )
            logger.info("Null HFP profile handler registered — HFP connections will be rejected")
        except DBusError as e:
            if "AlreadyExists" in str(e):
                logger.info("Null HFP profile already registered")
            else:
                logger.warning("Failed to register null HFP handler: %s (HFP may still work)", e)
        except Exception as e:
            logger.warning("Unexpected error registering null HFP handler: %s", e)

    async def _disconnect_hfp(self, address: str) -> bool:
        """Disconnect HFP profile so the speaker uses AVRCP for volume control.

        Many speakers (e.g. Bose) send volume buttons as HFP AT+VGS commands
        instead of AVRCP absolute volume.  BlueZ doesn't map HFP volume to
        the A2DP MediaTransport, so the volume buttons appear dead.

        Disconnecting HFP forces the speaker to fall back to AVRCP volume,
        which BlueZ correctly propagates to MediaTransport1.Volume.
        """
        from .bluez.constants import HFP_UUID

        try:
            device = await self._get_or_create_device(address)
        except Exception as e:
            logger.warning("HFP disconnect: cannot access device %s: %s", address, e)
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

    MAX_A2DP_ATTEMPTS = 3  # give up after this many consecutive failures

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
            self._a2dp_attempts.pop(address, None)
            return True

        # Bail out if we've already retried too many times
        attempts = self._a2dp_attempts.get(address, 0)
        if attempts >= self.MAX_A2DP_ATTEMPTS:
            logger.warning(
                "A2DP transport activation for %s failed after %d attempts — giving up",
                address, attempts,
            )
            return False

        self._a2dp_attempts[address] = attempts + 1

        # Log device UUIDs to confirm A2DP is advertised
        try:
            device = await self._get_or_create_device(address)
        except Exception as e:
            logger.warning("Cannot access device %s for A2DP check: %s", address, e)
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
                self._a2dp_attempts.pop(address, None)
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
        # Mark as connecting so the Connected signal handler won't re-enter
        self._connecting.add(address)
        try:
            await device.disconnect()
            await asyncio.sleep(2)
            await device.connect()
            await device.wait_for_services(timeout=10)
            await asyncio.sleep(3)
            found = await self._log_transport_properties(address)
            if found:
                self._a2dp_attempts.pop(address, None)
            return found
        except Exception as e:
            logger.warning("Disconnect/reconnect cycle failed for %s: %s", address, e)
            return False
        finally:
            self._connecting.discard(address)

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
        """Handle AVRCP MediaPlayer1 property change — push to WebSocket."""
        # Convert value to JSON-safe representation
        if isinstance(value, dict):
            safe_val = {k: str(v) for k, v in value.items()}
        else:
            safe_val = str(value) if not isinstance(value, (str, int, float, bool)) else value
        entry = {"address": address, "property": prop_name, "value": safe_val, "ts": time.time()}
        self.recent_avrcp.append(entry)
        self.event_bus.emit("avrcp_event", entry)

    # -- Per-device keep-alive management --

    async def _migrate_global_keepalive(self) -> None:
        """One-time migration: if old global keep_alive_enabled was true,
        enable keep-alive on all stored devices."""
        from pathlib import Path

        marker = Path("/data/.keepalive_migrated")
        if marker.exists():
            return
        try:
            opts_path = Path("/data/options.json")
            if opts_path.exists():
                data = json.loads(opts_path.read_text())
                if data.get("keep_alive_enabled", False):
                    method = data.get("keep_alive_method", "infrasound")
                    logger.info(
                        "Migrating global keep-alive (method=%s) to per-device settings",
                        method,
                    )
                    for device_info in self.store.devices:
                        await self.store.update_device_settings(
                            device_info["address"],
                            {"keep_alive_enabled": True, "keep_alive_method": method},
                        )
            marker.write_text("migrated")
        except Exception as e:
            logger.warning("Keep-alive migration failed (non-fatal): %s", e)

    async def _start_keepalive_if_enabled(self, address: str) -> None:
        """Start keep-alive for a device if enabled in its settings and connected."""
        settings = self.store.get_device_settings(address)
        if not settings["keep_alive_enabled"]:
            return
        if address in self._keepalives:
            return  # already running

        if not self.pulse:
            return
        sink_name = await self.pulse.get_sink_for_address(address)
        if not sink_name:
            logger.debug("Cannot start keep-alive for %s: no PA sink yet", address)
            return

        ka = KeepAliveService(method=settings["keep_alive_method"])
        ka.set_target_sink(sink_name)
        await ka.start()
        self._keepalives[address] = ka
        logger.info("Keep-alive started for %s (method=%s)", address, settings["keep_alive_method"])
        self.event_bus.emit("keepalive_changed", {
            "address": address, "enabled": True, "method": settings["keep_alive_method"],
        })

    async def _stop_keepalive(self, address: str) -> None:
        """Stop keep-alive for a device if running."""
        ka = self._keepalives.pop(address, None)
        if ka:
            await ka.stop()
            logger.info("Keep-alive stopped for %s", address)
            self.event_bus.emit("keepalive_changed", {"address": address, "enabled": False})

    async def update_device_settings(self, address: str, settings: dict) -> dict | None:
        """Update per-device settings and react to changes immediately."""
        device_info = await self.store.update_device_settings(address, settings)
        if device_info is None:
            return None

        # React to keep-alive changes if device is connected
        if address in self._device_connect_time:
            if "keep_alive_enabled" in settings:
                if device_info.get("keep_alive_enabled", False):
                    # Method may have changed — stop old instance and restart
                    await self._stop_keepalive(address)
                    await self._start_keepalive_if_enabled(address)
                else:
                    await self._stop_keepalive(address)

        await self._broadcast_devices()
        return device_info
