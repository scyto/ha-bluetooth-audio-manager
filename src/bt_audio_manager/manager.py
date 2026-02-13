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
from .audio.mpd import MPDManager
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

# Common Bluetooth USB vendor IDs → friendly vendor names.
# Used as a last-resort fallback when both sysfs and the Supervisor's
# udev database lack a human-readable product name.
_USB_BT_VENDORS: dict[str, str] = {
    "8087": "Intel",
    "0cf3": "Qualcomm / Atheros",
    "0a5c": "Broadcom",
    "0bda": "Realtek",
    "2357": "TP-Link",
    "0a12": "Cambridge Silicon Radio",
    "413c": "Dell",
    "0489": "Foxconn / Hon Hai",
    "13d3": "IMC Networks",
    "04ca": "Lite-On",
    "0930": "Toshiba",
}


class BluetoothAudioManager:
    """Central orchestrator for the Bluetooth Audio Manager app."""

    SINK_POLL_INTERVAL = 5  # seconds between sink state polls
    MAX_RECENT_EVENTS = 50  # ring buffer size for MPRIS/AVRCP events

    def __init__(self, config: AppConfig):
        self.config = config
        self._adapter_path: str | None = None  # resolved in start()
        self.bus: MessageBus | None = None
        self.adapter: BluezAdapter | None = None
        self.agent: PairingAgent | None = None
        self.pulse: PulseAudioManager | None = None
        self.store: PersistenceStore | None = None
        self.reconnect_service: ReconnectService | None = None
        self._keepalives: dict[str, KeepAliveService] = {}  # per-device keep-alive
        self._pending_suspends: dict[str, asyncio.Task] = {}  # addr → delayed suspend task
        self._auto_disconnect_tasks: dict[str, asyncio.Task] = {}  # addr → auto-disconnect timer
        self._suspended_sinks: set[str] = set()  # addresses with suspended sinks
        self._mpd_instances: dict[str, MPDManager] = {}  # addr → per-device MPD
        self._last_avrcp_device: tuple[str, float] | None = None  # (addr, timestamp)
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
        self._null_hfp_registered: bool = False  # tracks null HFP handler state
        # Ring buffers so WebSocket clients get recent events on reconnect
        self.recent_mpris: collections.deque = collections.deque(maxlen=self.MAX_RECENT_EVENTS)
        self.recent_avrcp: collections.deque = collections.deque(maxlen=self.MAX_RECENT_EVENTS)
        # Scanning state
        self._scanning: bool = False
        self._scan_task: asyncio.Task | None = None
        self._scan_debounce_handle: asyncio.TimerHandle | None = None

    async def _resolve_adapter_path(self) -> str:
        """Resolve the configured bt_adapter to a BlueZ D-Bus path.

        Handles three formats:
        - "auto"                → first powered adapter (or /org/bluez/hci0)
        - "78:20:51:F5:F1:07"  → look up by MAC address
        - "hci1" (legacy)      → look up by HCI name, migrate to MAC
        """
        cfg = self.config
        adapters = await BluezAdapter.list_all(self.bus)

        if cfg.bt_adapter == "auto":
            # Prefer first powered adapter, else first available
            powered = [a for a in adapters if a["powered"]]
            choice = powered[0] if powered else (adapters[0] if adapters else None)
            if choice:
                logger.info("Auto-selected adapter %s (%s)", choice["name"], choice["address"])
                return choice["path"]
            return "/org/bluez/hci0"

        if cfg.bt_adapter_is_mac:
            # New format: look up by MAC address
            match = next((a for a in adapters if a["address"] == cfg.bt_adapter), None)
            if match:
                logger.info(
                    "Resolved adapter MAC %s → %s", cfg.bt_adapter, match["path"],
                )
                return match["path"]
            # Configured adapter not found — fall back to auto for this session
            logger.warning(
                "Configured adapter %s not found — falling back to auto "
                "(adapter may be disconnected; settings preserved)",
                cfg.bt_adapter,
            )
            self._broadcast_status(
                f"Configured adapter {cfg.bt_adapter} not found — using default"
            )
            powered = [a for a in adapters if a["powered"]]
            choice = powered[0] if powered else (adapters[0] if adapters else None)
            return choice["path"] if choice else "/org/bluez/hci0"

        # Legacy format: HCI name like "hci1"
        match = next((a for a in adapters if a["name"] == cfg.bt_adapter), None)
        if match:
            logger.info(
                "Migrating legacy adapter setting %s → %s",
                cfg.bt_adapter, match["address"],
            )
            cfg.bt_adapter = match["address"]
            cfg.save_settings()
            return match["path"]
        # Legacy HCI name not found — fall back
        logger.warning(
            "Legacy adapter %s not found — falling back to auto",
            cfg.bt_adapter,
        )
        powered = [a for a in adapters if a["powered"]]
        choice = powered[0] if powered else (adapters[0] if adapters else None)
        return choice["path"] if choice else "/org/bluez/hci0"

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

                    # Silently discard noisy RSSI / ManufacturerData / TxPower
                    # churn — these fire many times per second per device and
                    # provide no actionable information for this app.
                    _NOISY_PROPS = {"RSSI", "ManufacturerData", "TxPower", "ServiceData"}
                    if iface_name == "org.bluez.Device1" and set(prop_names) <= _NOISY_PROPS:
                        pass
                    else:
                        # Log values for key interfaces; just names for the rest.
                        # Adapter1 changes (UUIDs, Class) are demoted to debug —
                        # they fire in bursts during profile re-registration and
                        # aren't actionable.
                        _VALUE_IFACES = {
                            "org.bluez.MediaTransport1",
                            "org.bluez.Device1",
                            "org.bluez.Adapter1",
                        }
                        is_adapter = iface_name == "org.bluez.Adapter1"
                        log_fn = logger.debug if is_adapter else logger.info
                        if iface_name in _VALUE_IFACES and isinstance(changed, dict):
                            props_str = " ".join(
                                f"{k}={v.value}" for k, v in changed.items()
                            )
                            log_fn(
                                "BlueZ PropertiesChanged: iface=%s %s path=%s",
                                iface_name, props_str, msg.path,
                            )
                        else:
                            log_fn(
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
                        # Extract device address from transport D-Bus path
                        parts = msg.path.split("/")
                        transport_addr = next(
                            (p[4:].replace("_", ":") for p in parts if p.startswith("dev_")), ""
                        )
                        if "Volume" in changed:
                            vol_raw = changed["Volume"].value  # 0-127 uint16
                            vol_pct = round(vol_raw / 127 * 100)
                            logger.info("AVRCP transport volume: %d%% (raw %d)", vol_pct, vol_raw)
                            self._last_signaled_volume[transport_addr] = vol_raw
                            entry = {"address": transport_addr, "property": "Volume", "value": f"{vol_pct}%", "ts": time.time()}
                            self.recent_avrcp.append(entry)
                            self.event_bus.emit("avrcp_event", entry)
                            # Sync volume to the device's MPD instance
                            mpd = self._mpd_instances.get(transport_addr)
                            if mpd and mpd.is_running:
                                asyncio.ensure_future(mpd.set_volume(vol_pct))
                        if "State" in changed:
                            state = changed["State"].value
                            if self.media_player and state == "active":
                                if self._is_avrcp_enabled(transport_addr):
                                    self.media_player.set_playback_status("Playing")
                                else:
                                    logger.info(
                                        "AVRCP disabled for %s — skipping PlaybackStatus=Playing",
                                        transport_addr,
                                    )
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

        # 2. Resolve configured adapter (MAC or legacy HCI) → D-Bus path
        self._adapter_path = await self._resolve_adapter_path()
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

        # 3c. Register null HFP handler to prevent HFP from being established
        #     Speakers like Bose send volume buttons as HFP AT+VGS commands
        #     instead of AVRCP.  Blocking HFP forces AVRCP volume.
        #     Skipped when any device uses HFP audio profile (it would block
        #     legitimate HFP connections).
        if self._has_hfp_profile_devices():
            logger.info("HFP audio profile device(s) found — skipping null HFP handler")
        else:
            await self._register_null_hfp_handler()

        # 5. Initialize PulseAudio manager
        pulse_server = os.environ.get("PULSE_SERVER", "<unset>")
        logger.info("PULSE_SERVER=%s", pulse_server)
        self.pulse = PulseAudioManager()
        try:
            await self.pulse.connect()
            self.pulse.on_volume_change(self._on_pa_volume_change)
            self.pulse.on_sink_state_change(self._on_pa_sink_running)
            self.pulse.on_sink_idle(self._on_pa_sink_idle)
            await self.pulse.start_event_monitor()
        except Exception as e:
            logger.warning("PulseAudio connection failed (will retry): %s", e)
            self.pulse = None

        # 6. Register BluezDevice objects for all stored devices so UI
        #    actions (disconnect, forget) work immediately, even if the
        #    device is already connected from a previous app session.
        for device_info in self.store.devices:
            addr = device_info["address"]
            try:
                device = await self._get_or_create_device(addr)
                if await device.is_connected():
                    logger.info("Device %s already connected at startup", addr)
                    self._last_signaled_volume.pop(addr, None)
                    self._device_connect_time[addr] = time.time()
                    audio_profile = self._get_audio_profile(addr)
                    if audio_profile == "hfp":
                        # Activate HFP PA card profile (PA defaults to a2dp
                        # after reboot even if device was using HFP before)
                        if self.pulse:
                            await self.pulse.activate_bt_card_profile(addr, profile="hfp")
                    elif self._should_disconnect_hfp(addr):
                        await self._disconnect_hfp(addr)
            except Exception as e:
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
        #     the app).  Create BluezDevice wrappers so UI buttons work.
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
                    if self._should_disconnect_hfp(addr):
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
                        # Extract device address from transport path
                        parts = path.split("/")
                        addr = next(
                            (p[4:].replace("_", ":") for p in parts if p.startswith("dev_")), ""
                        )
                        if self._is_avrcp_enabled(addr):
                            logger.info(
                                "Active A2DP transport at startup (%s) — setting PlaybackStatus=Playing", path,
                            )
                            self.media_player.set_playback_status("Playing")
                            break
                        else:
                            logger.info(
                                "Active A2DP transport at startup (%s) but AVRCP disabled — skipping", path,
                            )
            except Exception as e:
                logger.debug("Could not check transport state at startup: %s", e)

        # 7. Start reconnection service
        self.reconnect_service = ReconnectService(self)
        await self.reconnect_service.start()

        # 8. Reconnect stored devices that aren't already connected
        await self.reconnect_service.reconnect_all()

        # 9. Migrate global keep-alive → per-device (one-time for upgrading users)
        await self._migrate_global_keepalive()

        # 9b. Start idle mode handlers and MPD for any connected devices
        for device_info in self.store.devices:
            addr = device_info["address"]
            if addr in self._device_connect_time:
                await self._apply_idle_mode(addr)
                await self._start_mpd_if_enabled(addr)

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

        # Stop all MPD instances
        for addr in list(self._mpd_instances):
            await self._stop_mpd(addr)

        # Stop all idle handlers (keep-alive, pending suspends, auto-disconnect)
        for addr in list(self._keepalives):
            await self._stop_keepalive(addr)
        for addr in list(self._pending_suspends):
            self._cancel_pending_suspend(addr)
        for addr in list(self._auto_disconnect_tasks):
            self._cancel_auto_disconnect_timer(addr)

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
        # audio to persist if the app restarts)
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

            # Always call connect() even if already connected — BlueZ's
            # pair auto-connect only creates a link-level connection; the
            # explicit Connect() D-Bus call is needed to set up A2DP profiles.
            already_connected = False
            try:
                already_connected = await device.is_connected()
            except Exception:
                pass

            if already_connected:
                logger.info("Device %s already connected, calling connect() to ensure A2DP profiles", address)
            await device.connect()

            self._broadcast_status(f"Waiting for services on {address}...")
            await device.wait_for_services(timeout=10)

            # Try to subscribe to AVRCP media player signals
            try:
                await device.watch_media_player()
            except Exception as e:
                logger.debug("AVRCP watch failed for %s: %s", address, e)

            # Activate the selected audio profile and verify sink appeared
            audio_profile = self._get_audio_profile(address)
            if self.pulse:
                profile_label = "HFP" if audio_profile == "hfp" else "A2DP"
                self._broadcast_status(f"Waiting for {profile_label} sink for {address}...")
                # Set the PA card to the desired profile
                activated = await self.pulse.activate_bt_card_profile(address, profile=audio_profile)

                # HFP fallback: if PA card doesn't have headset-head-unit,
                # explicitly ask BlueZ to connect the HFP profile and retry.
                if not activated and audio_profile == "hfp":
                    from .bluez.constants import HFP_UUID
                    logger.info("HFP PA profile not available — trying ConnectProfile(HFP)...")
                    self._broadcast_status(f"Connecting HFP profile for {address}...")
                    try:
                        await device.connect_profile(HFP_UUID)
                        await asyncio.sleep(3)
                        activated = await self.pulse.activate_bt_card_profile(address, profile="hfp")
                    except Exception as e:
                        logger.warning("ConnectProfile(HFP) failed for %s: %s", address, e)

                    if not activated:
                        # Last resort: reload PA bluetooth module and retry
                        logger.info("HFP still not available — reloading PA bluetooth module...")
                        self._broadcast_status(f"Reloading audio subsystem for {address}...")
                        await self._reload_pa_bluetooth_module()
                        # Reconnect the device (module reload drops BT cards)
                        try:
                            await device.connect()
                            await device.wait_for_services(timeout=10)
                            await asyncio.sleep(2)
                            activated = await self.pulse.activate_bt_card_profile(address, profile="hfp")
                        except Exception as e:
                            logger.warning("Reconnect after PA reload failed for %s: %s", address, e)

                sink_name = None
                if activated:
                    self._broadcast_status(f"Waiting for {profile_label} sink for {address}...")
                    sink_name = await self.pulse.wait_for_bt_sink(
                        address, timeout=15, connected_check=device.is_connected
                    )
                if sink_name:
                    # Disconnect HFP only for A2DP devices — doing it earlier
                    # can cause the speaker to drop the entire connection when
                    # HFP is the only active profile.
                    if self._should_disconnect_hfp(address):
                        await self._disconnect_hfp(address)
                    await self._apply_idle_mode(address)
                    await self._start_mpd_if_enabled(address)
                    await self._broadcast_all()
                    return True
                logger.warning("%s sink for %s did not appear in PulseAudio", profile_label, address)
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
        # different adapter than the one this app is configured to use)
        await BluezAdapter.remove_device_any_adapter(self.bus, address)

        # Stop MPD and release port before removing from store
        await self._stop_mpd(address)
        await self.store.release_mpd_port(address)

        # Remove from persistent store
        await self.store.remove_device(address)
        logger.info("Device %s forgotten", address)
        self.event_bus.emit("status", {"message": ""})
        await self._broadcast_all()

    async def clear_all_devices(self) -> None:
        """Disconnect, unpair, and remove ALL devices from BlueZ and the
        persistent store.

        Used before switching adapters so the new adapter starts fresh.
        Broadcasts status updates during the cleanup for frontend progress.
        """
        # 1. Stop reconnect service to prevent interference
        if self.reconnect_service:
            await self.reconnect_service.stop()

        # 2. Stop all idle handlers
        for addr in list(self._keepalives):
            await self._stop_keepalive(addr)
        for addr in list(self._pending_suspends):
            self._cancel_pending_suspend(addr)
        for addr in list(self._auto_disconnect_tasks):
            self._cancel_auto_disconnect_timer(addr)

        # 3. Collect all known addresses (managed + stored)
        addresses = set(self.managed_devices.keys())
        addresses.update(d["address"] for d in self.store.devices)

        if not addresses:
            self._broadcast_status("No devices to clean up")
            await asyncio.sleep(0.5)
            return

        total = len(addresses)

        # 4. Disconnect all connected devices
        for i, addr in enumerate(addresses, 1):
            self._broadcast_status(f"Disconnecting device {i}/{total}...")
            self._suppress_reconnect.add(addr)
            device = self.managed_devices.get(addr)
            if device:
                try:
                    await device.disconnect()
                except Exception as exc:
                    logger.warning("clear_all: disconnect %s failed: %s", addr, exc)

        # 5. Brief pause for BlueZ to process disconnections
        await asyncio.sleep(1)

        # 6. Remove each device from BlueZ and clean up D-Bus subscriptions
        for i, addr in enumerate(addresses, 1):
            self._broadcast_status(f"Removing device {i}/{total}...")
            device = self.managed_devices.pop(addr, None)
            if device:
                device.cleanup()
            try:
                await BluezAdapter.remove_device_any_adapter(self.bus, addr)
            except Exception as exc:
                logger.warning("clear_all: BlueZ remove %s failed: %s", addr, exc)

        # 7. Clear persistent store (single write instead of N individual removes)
        self.store._devices.clear()
        await self.store.save()

        # 8. Clear internal tracking state
        self._device_connect_time.clear()
        self._last_signaled_volume.clear()
        self._suppress_reconnect.clear()
        self._a2dp_attempts.clear()
        self._connecting.clear()

        self._broadcast_status(f"Cleared {total} device(s)")
        logger.info("clear_all_devices: removed %d device(s)", total)
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
                device["idle_mode"] = s.get("idle_mode", "default")
                device["keep_alive_method"] = s["keep_alive_method"]
                device["keep_alive_active"] = addr in self._keepalives
                device["power_save_delay"] = s.get("power_save_delay", 0)
                device["auto_disconnect_minutes"] = s.get("auto_disconnect_minutes", 30)
                device["mpd_enabled"] = s.get("mpd_enabled", False)
                device["mpd_port"] = s.get("mpd_port")
                device["mpd_hw_volume"] = s.get("mpd_hw_volume", 100)
                device["avrcp_enabled"] = s.get("avrcp_enabled", True)

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
                        "idle_mode": s.get("idle_mode", "default"),
                        "keep_alive_method": s["keep_alive_method"],
                        "keep_alive_active": addr in self._keepalives,
                        "power_save_delay": s.get("power_save_delay", 0),
                        "auto_disconnect_minutes": s.get("auto_disconnect_minutes", 30),
                        "mpd_enabled": s.get("mpd_enabled", False),
                        "mpd_port": s.get("mpd_port"),
                        "mpd_hw_volume": s.get("mpd_hw_volume", 100),
                        "avrcp_enabled": s.get("avrcp_enabled", True),
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
        one this app is configured to use, and whether it appears to
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

        # Final fallback: use built-in USB vendor names for adapters
        # that still have no friendly name (udev database was incomplete)
        for a in adapters:
            if not a["hw_model"] or a["hw_model"] == a["modalias"]:
                usb_id = a.get("usb_id", "")
                if usb_id:
                    vendor_name = _USB_BT_VENDORS.get(usb_id.split(":")[0], "")
                    if vendor_name:
                        a["hw_model"] = f"{vendor_name} ({usb_id})"

        ha_bt_macs = await self._get_ha_bluetooth_macs()

        for a in adapters:
            a["selected"] = a["path"] == self._adapter_path
            a["ble_scanning"] = a["discovering"]
            a["ha_managed"] = a["address"].upper() in ha_bt_macs
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
        # Try hostname first, then fall back to standard Supervisor IP
        # (AppArmor may block /etc/resolv.conf, breaking DNS resolution)
        urls = ["http://supervisor/hardware/info", "http://172.30.32.2/hardware/info"]
        result = None
        for url in urls:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status != 200:
                            logger.warning("Supervisor hardware API returned %s from %s", resp.status, url)
                            continue
                        result = await resp.json()
                        break
            except Exception as e:
                logger.debug("Supervisor API at %s failed: %s", url, e)
                continue
        if result is None:
            logger.warning("Failed to query Supervisor hardware API (tried hostname and IP)")
            return {}
        try:

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

                # Prefer udev database names; raw ID_MODEL/ID_VENDOR are
                # often just hex IDs (e.g. "8087") which aren't useful.
                name = attrs.get("ID_MODEL_FROM_DATABASE") or ""
                vendor = attrs.get("ID_VENDOR_FROM_DATABASE") or ""
                if not name and not vendor:
                    # No udev database entry — skip this device, the raw
                    # IDs aren't useful as display names
                    continue
                full_name = f"{vendor} {name}".strip() if vendor and name else (name or vendor)
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
            return names
        except Exception as e:
            logger.warning("Failed to query Supervisor hardware API: %s", e)
            return {}

    @staticmethod
    async def _get_ha_bluetooth_macs() -> set[str]:
        """Query HA Core for adapters configured in the Bluetooth integration.

        Returns a set of uppercase MAC addresses that HA is managing.
        Falls back to an empty set on any failure (non-blocking).
        """
        import aiohttp

        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            return set()
        urls = [
            "http://supervisor/core/api/config/config_entries/entry",
            "http://172.30.32.2/core/api/config/config_entries/entry",
        ]
        for url in urls:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status != 200:
                            logger.debug(
                                "HA Core config entries API returned %s from %s",
                                resp.status, url,
                            )
                            continue
                        entries = await resp.json()
                        break
            except Exception as e:
                logger.debug("HA Core API at %s failed: %s", url, e)
                continue
        else:
            logger.debug("Could not reach HA Core API for Bluetooth integration info")
            return set()

        import re

        mac_re = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
        macs: set[str] = set()
        for entry in entries:
            if entry.get("domain") != "bluetooth":
                continue
            # Prefer unique_id (may be a MAC); fall back to parsing title
            # e.g. "cyber-blue(HK)Ltd CSR8510 A10 (00:1A:7D:DA:71:11)"
            uid = entry.get("unique_id") or ""
            m = mac_re.search(uid) or mac_re.search(entry.get("title", ""))
            if m:
                macs.add(m.group(1).upper())
        if macs:
            logger.info("HA Bluetooth integration manages adapters: %s", macs)
        return macs

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
        asyncio.ensure_future(self._stop_idle_handler(address))
        asyncio.ensure_future(self._stop_mpd(address))

        if address in self._connecting:
            # Active pair/connect flow in progress — don't start competing reconnect
            logger.info("Skipping auto-reconnect for %s (connection in progress)", address)
        elif address in self._suppress_reconnect:
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
        try:
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
            if self._should_disconnect_hfp(address):
                await self._disconnect_hfp(address)

            audio_profile = self._get_audio_profile(address)
            if audio_profile == "hfp":
                # For HFP devices: activate headset-head-unit PA profile and
                # apply idle mode / MPD (the auto-reconnect signal handler
                # doesn't go through connect_device's setup path).
                if self.pulse:
                    await self.pulse.activate_bt_card_profile(address, profile="hfp")
                    sink_name = await self.pulse.get_sink_for_address(address)
                    if sink_name:
                        await self._apply_idle_mode(address)
                        await self._start_mpd_if_enabled(address)
            else:
                # Check/activate A2DP transport (may need ConnectProfile)
                await self._ensure_a2dp_transport(address)
        except Exception as e:
            logger.warning("Post-connect setup failed for %s: %s", address, e)

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

        After an app restart the old D-Bus unique name is gone, but the
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
                    await self._apply_idle_mode(address)
                    await self._start_mpd_if_enabled(address)

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
            self._null_hfp_registered = True
            logger.info("Null HFP profile handler registered — HFP connections will be rejected")
        except DBusError as e:
            if "AlreadyExists" in str(e):
                self._null_hfp_registered = True
                logger.info("Null HFP profile already registered")
            else:
                logger.warning("Failed to register null HFP handler: %s (HFP may still work)", e)
        except Exception as e:
            logger.warning("Unexpected error registering null HFP handler: %s", e)

    async def _unregister_null_hfp_handler(self) -> None:
        """Unregister the null HFP handler so real HFP connections can proceed.

        Also reloads PulseAudio's Bluetooth module so it re-registers its
        own HFP/HSP profile handler (our null handler displaced it).
        """
        if not self._null_hfp_registered:
            return
        from .bluez.constants import BLUEZ_SERVICE
        profile_path = "/org/ha/bluetooth_audio/null_hfp"
        try:
            intro = await self.bus.introspect(BLUEZ_SERVICE, "/org/bluez")
            proxy = self.bus.get_proxy_object(BLUEZ_SERVICE, "/org/bluez", intro)
            profile_mgr = proxy.get_interface("org.bluez.ProfileManager1")
            await profile_mgr.call_unregister_profile(profile_path)
            self._null_hfp_registered = False
            logger.info("Null HFP handler unregistered — HFP connections now allowed")
        except Exception as e:
            logger.debug("Could not unregister null HFP handler: %s", e)

        # Reload PA's Bluetooth module so it re-registers its HFP handler
        await self._reload_pa_bluetooth_module()

    async def _reload_pa_bluetooth_module(self) -> None:
        """Reload PulseAudio's module-bluez5-discover to restore HFP/HSP handling.

        Our null HFP handler displaced PulseAudio's native HFP profile
        registration.  Unregistering ours doesn't restore PA's — the only
        way is to unload/reload the module so PA re-registers with BlueZ.

        Strategy:
        1. Try ``pactl unload-module`` + ``pactl load-module`` (fast, minimal disruption).
        2. If the load fails (common: "lock: Permission denied" from the
           external PA server), restart the entire HA audio service via the
           Supervisor API and reconnect our PA client.
        """
        # --- Attempt 1: pactl unload + load ---
        try:
            proc = await asyncio.create_subprocess_exec(
                "pactl", "list", "modules", "short",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            module_index = None
            for line in stdout.decode().splitlines():
                if "module-bluez5-discover" in line:
                    module_index = line.split()[0]
                    break

            if module_index:
                proc = await asyncio.create_subprocess_exec(
                    "pactl", "unload-module", module_index,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
                logger.info("Unloaded module-bluez5-discover (index %s)", module_index)
                await asyncio.sleep(2)

                proc = await asyncio.create_subprocess_exec(
                    "pactl", "load-module", "module-bluez5-discover",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode == 0:
                    logger.info("Reloaded module-bluez5-discover — PA HFP/HSP handler restored")
                    await asyncio.sleep(2)
                    return
                logger.warning(
                    "pactl load-module failed: %s — falling back to audio service restart",
                    stderr.decode(errors="replace").strip(),
                )
        except (FileNotFoundError, OSError) as exc:
            logger.warning("pactl not available: %s — falling back to audio service restart", exc)
        except Exception as e:
            logger.warning("pactl module reload failed: %s — falling back to audio service restart", e)

        # --- Attempt 2: restart the HA audio service via Supervisor API ---
        await self._restart_audio_service()

    async def _restart_audio_service(self) -> None:
        """Restart the HA audio service via the Supervisor API.

        This is the nuclear option when ``pactl load-module`` fails.
        It restarts PulseAudio entirely, which re-registers all Bluetooth
        handlers fresh.  Our PA client reconnects automatically afterward.
        """
        import aiohttp

        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            logger.warning("SUPERVISOR_TOKEN not set — cannot restart audio service")
            return

        self._broadcast_status("Restarting audio service...")

        for url in ("http://supervisor/audio/restart",
                     "http://172.30.32.2/audio/restart"):
            try:
                async with aiohttp.ClientSession() as session:
                    headers = {"Authorization": f"Bearer {token}"}
                    async with session.post(
                        url, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        if resp.status == 200:
                            logger.info("Audio service restart requested via Supervisor")
                            # Wait for PA to come back, then reconnect our client
                            await asyncio.sleep(5)
                            try:
                                await self.pulse.reconnect(retries=10, delay=2.0)
                                logger.info("PA client reconnected after audio restart")
                            except ConnectionError:
                                logger.error("PA did not come back after audio restart")
                            return
                        body = await resp.text()
                        logger.warning("Audio restart via %s returned %d: %s", url, resp.status, body)
            except Exception as e:
                logger.warning("Audio restart via %s failed: %s", url, e)
                continue

        logger.error("Failed to restart audio service via Supervisor API")

    def _broadcast_toast(self, message: str, level: str = "info") -> None:
        """Push a toast notification to WebSocket clients."""
        self.event_bus.emit("toast", {"message": message, "level": level})

    async def _poll_card_profile(
        self, address: str, profile: str, attempts: int = 5, interval: float = 2,
    ) -> bool:
        """Poll PA card until the requested profile appears and is activated."""
        for i in range(attempts):
            if i > 0:
                await asyncio.sleep(interval)
            if await self.pulse.activate_bt_card_profile(address, profile=profile):
                logger.info(
                    "Card profile %s activated for %s (poll attempt %d)",
                    profile, address, i + 1,
                )
                return True
            logger.debug(
                "Card profile %s not yet available for %s (attempt %d/%d)",
                profile, address, i + 1, attempts,
            )
        return False

    async def _apply_audio_profile(self, address: str, profile: str) -> None:
        """Activate the requested PA card profile with full fallback chain.

        Runs as a background task after settings are saved.  Sends toast
        notifications via WebSocket so the user sees progress and errors.

        For HFP: BlueZ does NOT call PA's ``NewConnection()`` for devices
        that are already connected when a profile handler is registered.
        We must explicitly ``DisconnectProfile`` + ``ConnectProfile`` to
        force a fresh RFCOMM channel, triggering PA's callback.
        """
        profile_label = "HFP" if profile == "hfp" else "A2DP"
        try:
            # --- Quick check: profile may already be available ---
            activated = await self.pulse.activate_bt_card_profile(address, profile=profile)

            if not activated and profile == "hfp":
                from .bluez.constants import HFP_UUID

                # --- Fallback 1: Disconnect + ConnectProfile to force fresh RFCOMM ---
                # BlueZ may consider the profile "connected" without an actual
                # RFCOMM channel (returns instant success).  DisconnectProfile
                # first to force a real fresh connection that triggers PA's
                # NewConnection() callback.
                self._broadcast_status("Activating HFP profile...")
                logger.info("HFP Fallback 1: DisconnectProfile + ConnectProfile for %s", address)
                try:
                    device = await self._get_or_create_device(address)
                    # Force disconnect the HFP profile first
                    try:
                        await device.disconnect_profile(HFP_UUID)
                        logger.info("HFP Fallback 1: DisconnectProfile succeeded for %s", address)
                        await asyncio.sleep(1)
                    except Exception as e:
                        logger.debug("HFP Fallback 1: DisconnectProfile: %s (continuing)", e)
                    await device.connect_profile(HFP_UUID)
                    logger.info("HFP Fallback 1: ConnectProfile succeeded for %s", address)
                except Exception as e:
                    logger.warning("HFP Fallback 1: ConnectProfile failed for %s: %s", address, e)

                # Poll — PA needs time to process NewConnection and create the profile
                activated = await self._poll_card_profile(address, "hfp", attempts=5, interval=2)

            if not activated and profile == "hfp":
                from .bluez.constants import HFP_UUID

                # --- Fallback 2: full disconnect/reconnect + PA restart ---
                # Nuclear option: restart PA to re-register all handlers,
                # then reconnect device so BlueZ establishes HFP fresh.
                logger.info("HFP Fallback 2: PA restart + reconnect for %s", address)
                self._broadcast_status("Restarting audio for HFP...")
                await self._reload_pa_bluetooth_module()
                try:
                    device = await self._get_or_create_device(address)
                    await device.disconnect()
                    await asyncio.sleep(3)
                    await device.connect()
                    await device.wait_for_services(timeout=10)
                    # Explicit ConnectProfile after fresh connect
                    try:
                        await device.connect_profile(HFP_UUID)
                        logger.info("HFP Fallback 2: ConnectProfile succeeded for %s", address)
                    except Exception as e:
                        logger.debug("HFP Fallback 2: ConnectProfile after connect: %s", e)
                    activated = await self._poll_card_profile(address, "hfp", attempts=5, interval=2)
                except Exception as e:
                    logger.warning("HFP Fallback 2: reconnect cycle failed for %s: %s", address, e)

            self._broadcast_status("")  # clear status banner

            if activated:
                sink_name = await self.pulse.wait_for_bt_sink(address, timeout=10)
                if sink_name:
                    await self._apply_idle_mode(address)
                    await self._start_mpd_if_enabled(address)
                self._broadcast_toast(
                    f"Audio profile switched to {profile_label}", "success",
                )
                logger.info("Audio profile for %s → %s", address, profile_label)
            elif profile == "a2dp":
                # Switching back to A2DP — disconnect HFP if needed
                if self._should_disconnect_hfp(address):
                    await self._disconnect_hfp(address)
                sink_name = await self.pulse.wait_for_bt_sink(address, timeout=10)
                if sink_name:
                    await self._apply_idle_mode(address)
                self._broadcast_toast(
                    f"Audio profile switched to {profile_label}", "success",
                )
                logger.info("Audio profile for %s → %s", address, profile_label)
            else:
                self._broadcast_toast(
                    f"Failed to activate {profile_label} — device may not support "
                    f"HFP, or PulseAudio could not establish the HFP channel. "
                    f"Try disconnecting and reconnecting the device.",
                    "error",
                )
                logger.warning(
                    "Audio profile switch to %s failed for %s — "
                    "PA card has no HFP profile after all fallbacks",
                    profile_label, address,
                )
        except Exception as e:
            self._broadcast_status("")
            self._broadcast_toast(
                f"Audio profile switch failed: {e}", "error",
            )
            logger.error("Audio profile switch for %s failed: %s", address, e)
        finally:
            await self._broadcast_devices()

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
        logger.debug("Device %s UUIDs: %s", address, uuids)

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
        # Sync PA volume change to MPD so HA's media_player entity reflects
        # the speaker's actual volume (speaker buttons → AVRCP → PA → MPD)
        if addr:
            mpd = self._mpd_instances.get(addr)
            if mpd and mpd.is_running:
                asyncio.ensure_future(mpd.set_volume(volume))

    @staticmethod
    def _addr_from_sink_name(sink_name: str) -> str:
        """Extract BT address from sink name like 'bluez_sink.XX_XX_XX_XX_XX_XX.a2dp_sink'."""
        parts = sink_name.split(".")
        return parts[1].replace("_", ":") if len(parts) >= 2 else ""

    def _is_avrcp_enabled(self, address: str) -> bool:
        """Check if AVRCP media buttons are enabled for a device."""
        if not address:
            return True  # unknown device — default to enabled
        settings = self.store.get_device_settings(address)
        return settings.get("avrcp_enabled", True)

    def _get_audio_profile(self, address: str) -> str:
        """Get the audio profile setting for a device ('a2dp' or 'hfp')."""
        if not address:
            return "a2dp"
        settings = self.store.get_device_settings(address)
        return settings.get("audio_profile", "a2dp")

    def _should_disconnect_hfp(self, address: str) -> bool:
        """Check if HFP should be disconnected for this device (A2DP mode only)."""
        return self._get_audio_profile(address) != "hfp"

    def _has_hfp_profile_devices(self) -> bool:
        """Check if any stored device uses the HFP audio profile."""
        if not self.store:
            return False
        for d in self.store.devices:
            settings = self.store.get_device_settings(d["address"])
            if settings.get("audio_profile") == "hfp":
                return True
        return False

    def _on_pa_sink_running(self, sink_name: str) -> None:
        """Handle BT sink transition to 'running' — audio is actively flowing.

        Sets PlaybackStatus='Playing' so the speaker enables AVRCP volume
        buttons (if AVRCP is enabled for this device).
        Cancels any pending power-save suspend or auto-disconnect timer.
        """
        addr = self._addr_from_sink_name(sink_name)
        # Cancel pending power-save suspend
        self._cancel_pending_suspend(addr)
        # Cancel auto-disconnect timer
        self._cancel_auto_disconnect_timer(addr)
        # Resume sink if it was suspended (PA does this automatically on play,
        # but track our state)
        self._suspended_sinks.discard(addr)
        if self.media_player:
            if self._is_avrcp_enabled(addr):
                self.media_player.set_playback_status("Playing")
                logger.info("Sink running for %s — set PlaybackStatus=Playing", addr)
            else:
                logger.info("AVRCP disabled for %s — skipping PlaybackStatus=Playing on sink running", addr)

    def _on_pa_sink_idle(self, sink_name: str) -> None:
        """Handle BT sink transition from 'running' to idle — audio stopped.

        Sets PlaybackStatus='Stopped' so the speaker knows nothing is playing
        (if AVRCP is enabled for this device — auto-tracking mode).
        Then applies idle mode behaviour (power-save suspend, auto-disconnect).
        """
        addr = self._addr_from_sink_name(sink_name)
        if self.media_player:
            if self._is_avrcp_enabled(addr):
                self.media_player.set_playback_status("Stopped")
                logger.info("Sink idle for %s — set PlaybackStatus=Stopped", addr)
            else:
                logger.info("AVRCP disabled for %s — skipping PlaybackStatus=Stopped on sink idle", addr)
        # Apply idle mode behaviour
        if addr and self.store:
            settings = self.store.get_device_settings(addr)
            mode = settings.get("idle_mode", "default")
            if mode == "power_save":
                delay = settings.get("power_save_delay", 0)
                self._schedule_sink_suspend(addr, sink_name, delay)
            elif mode == "auto_disconnect":
                self._start_auto_disconnect_timer(addr, settings)

    AVRCP_DEVICE_WINDOW = 2.0  # seconds to consider _last_avrcp_device valid

    def _on_avrcp_command(self, command: str, detail: str) -> None:
        """Handle MPRIS command from speaker buttons (via registered MPRIS player)."""
        entry = {"command": command, "detail": detail, "ts": time.time()}

        # Resolve which device sent this command
        target_addr = None
        if self._last_avrcp_device:
            addr, ts = self._last_avrcp_device
            if time.time() - ts < self.AVRCP_DEVICE_WINDOW:
                target_addr = addr

        if target_addr:
            entry["address"] = target_addr

        self.recent_mpris.append(entry)
        self.event_bus.emit("mpris_command", entry)

        # Guard: if we know the source device and AVRCP is disabled, skip routing
        if target_addr and not self._is_avrcp_enabled(target_addr):
            logger.info("AVRCP disabled for %s — ignoring %s command", target_addr, command)
            return

        # Route to the specific device's MPD instance
        if not self._mpd_instances:
            return
        if target_addr:
            mpd = self._mpd_instances.get(target_addr)
            if mpd and mpd.is_running:
                asyncio.ensure_future(mpd.handle_command(command, detail))
        else:
            # Single-instance fallback: if only one MPD is running, route to it
            running = [(a, m) for a, m in self._mpd_instances.items() if m.is_running]
            if len(running) == 1:
                addr, mpd = running[0]
                if not self._is_avrcp_enabled(addr):
                    logger.info("AVRCP disabled for %s — ignoring %s (single-instance fallback)", addr, command)
                    return
                logger.info("Single MPD instance — routing %s to %s", command, addr)
                entry["address"] = addr
                asyncio.ensure_future(mpd.handle_command(command, detail))
            else:
                logger.debug("Cannot determine source device for MPRIS command %s, ignoring", command)

    def _on_avrcp_event(self, address: str, prop_name: str, value: object) -> None:
        """Handle AVRCP MediaPlayer1 property change — push to WebSocket."""
        # Track last active device for AVRCP command routing
        if prop_name == "Status":
            self._last_avrcp_device = (address, time.time())

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
                            {"idle_mode": "keep_alive", "keep_alive_method": method},
                        )
            marker.write_text("migrated")
        except Exception as e:
            logger.warning("Keep-alive migration failed (non-fatal): %s", e)

    async def _start_keepalive_if_enabled(self, address: str) -> None:
        """Start keep-alive for a device if idle_mode is 'keep_alive' and connected."""
        settings = self.store.get_device_settings(address)
        if settings.get("idle_mode", "default") != "keep_alive":
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

    # -- Idle mode management --

    async def _apply_idle_mode(self, address: str) -> None:
        """Start the appropriate idle handler for the device's idle_mode setting."""
        settings = self.store.get_device_settings(address)
        mode = settings.get("idle_mode", "default")

        # Stop any existing idle handler first
        await self._stop_idle_handler(address)

        if mode == "keep_alive":
            await self._start_keepalive_if_enabled(address)
        # power_save and auto_disconnect are triggered reactively by
        # _on_pa_sink_idle, not started proactively here

    async def _stop_idle_handler(self, address: str) -> None:
        """Stop any active idle handler for a device."""
        await self._stop_keepalive(address)
        self._cancel_pending_suspend(address)
        self._cancel_auto_disconnect_timer(address)
        # Resume sink if we suspended it
        if address in self._suspended_sinks and self.pulse:
            sink_name = await self.pulse.get_sink_for_address(address)
            if sink_name:
                await self.pulse.resume_sink(sink_name)
            self._suspended_sinks.discard(address)

    def _schedule_sink_suspend(self, address: str, sink_name: str, delay: int) -> None:
        """Schedule a delayed PA sink suspension for power-save mode."""
        self._cancel_pending_suspend(address)
        if delay <= 0:
            task = asyncio.ensure_future(self._do_sink_suspend(address, sink_name))
        else:
            task = asyncio.ensure_future(self._delayed_sink_suspend(address, sink_name, delay))
        self._pending_suspends[address] = task

    async def _delayed_sink_suspend(self, address: str, sink_name: str, delay: int) -> None:
        """Wait then suspend the sink."""
        try:
            logger.info("Power-save: suspending sink for %s in %ds", address, delay)
            await asyncio.sleep(delay)
            await self._do_sink_suspend(address, sink_name)
        except asyncio.CancelledError:
            logger.debug("Power-save suspend cancelled for %s", address)

    async def _do_sink_suspend(self, address: str, sink_name: str) -> None:
        """Actually suspend the PA sink."""
        self._pending_suspends.pop(address, None)
        if self.pulse:
            ok = await self.pulse.suspend_sink(sink_name)
            if ok:
                self._suspended_sinks.add(address)
                logger.info("Power-save: sink suspended for %s", address)

    def _cancel_pending_suspend(self, address: str) -> None:
        """Cancel a pending power-save suspend task."""
        task = self._pending_suspends.pop(address, None)
        if task and not task.done():
            task.cancel()

    def _start_auto_disconnect_timer(self, address: str, settings: dict) -> None:
        """Start an auto-disconnect timer that fires when idle for N minutes."""
        self._cancel_auto_disconnect_timer(address)
        minutes = settings.get("auto_disconnect_minutes", 30)
        logger.info("Auto-disconnect: will disconnect %s in %d minutes if idle", address, minutes)
        task = asyncio.ensure_future(self._auto_disconnect_after(address, minutes * 60))
        self._auto_disconnect_tasks[address] = task

    async def _auto_disconnect_after(self, address: str, seconds: int) -> None:
        """Wait then disconnect the device."""
        try:
            await asyncio.sleep(seconds)
            self._auto_disconnect_tasks.pop(address, None)
            logger.info("Auto-disconnect: disconnecting %s after idle timeout", address)
            await self.disconnect_device(address)
        except asyncio.CancelledError:
            logger.debug("Auto-disconnect timer cancelled for %s", address)

    def _cancel_auto_disconnect_timer(self, address: str) -> None:
        """Cancel a pending auto-disconnect timer."""
        task = self._auto_disconnect_tasks.pop(address, None)
        if task and not task.done():
            task.cancel()

    # -- Per-device MPD management --

    def _get_mpd_password(self) -> str | None:
        """Read the global MPD password from add-on options."""
        try:
            from pathlib import Path
            opts_path = Path("/data/options.json")
            if opts_path.exists():
                data = json.loads(opts_path.read_text())
                pw = data.get("mpd_password", "")
                return pw if pw else None
        except Exception as e:
            logger.debug("Could not read mpd_password from options: %s", e)
        return None

    async def _start_mpd_if_enabled(self, address: str) -> None:
        """Start a per-device MPD instance if mpd_enabled in its settings."""
        settings = self.store.get_device_settings(address)
        if not settings.get("mpd_enabled", False):
            return
        if address in self._mpd_instances:
            return  # already running
        if not self.pulse:
            return

        sink_name = await self.pulse.get_sink_for_address(address)
        if not sink_name:
            logger.debug("No PA sink for %s yet, MPD start deferred", address)
            return

        # Allocate a port from the pool
        port = await self.store.allocate_mpd_port(address)
        if port is None:
            logger.warning("Cannot start MPD for %s: all 10 ports in use", address)
            return

        # Auto-generate MPD name from device name + last 5 chars of MAC
        # for disambiguation when multiple devices share the same name
        device_info = self.store.get_device(address)
        device_name = device_info["name"] if device_info else address
        mac_suffix = address.replace(":", "")[-5:].upper()
        mpd_name = f"{device_name} ({mac_suffix})"

        mpd_password = self._get_mpd_password()

        mpd = MPDManager(
            address=address,
            port=port,
            speaker_name=mpd_name,
            password=mpd_password,
        )
        try:
            await mpd.start(sink_name)
            self._mpd_instances[address] = mpd
            # Tell speaker we're Playing so it enables AVRCP volume buttons
            if self.media_player and self._is_avrcp_enabled(address):
                self.media_player.set_playback_status("Playing")
            # Sync volume: set hardware to 100% so MPD is the single volume
            # control, or sync MPD to hardware if a stream is active.
            await self._init_mpd_volume(address, mpd, sink_name)
        except Exception as e:
            logger.warning("MPD start failed for %s: %s", address, e)

    async def _init_mpd_volume(
        self, address: str, mpd: "MPDManager", sink_name: str
    ) -> None:
        """Set hardware volume to configured level when no stream is active.

        Makes MPD the single volume control — HA automations can then
        reliably set volume via ``media_player.volume_set`` before TTS.
        If a stream IS active (e.g. add-on restarted mid-playback), sync
        MPD volume to the current hardware level instead.
        """
        if not self.pulse:
            return
        try:
            settings = self.store.get_device_settings(address)
            hw_vol = settings.get("mpd_hw_volume", 100)
            vol_state = await self.pulse.get_sink_volume(sink_name)
            if not vol_state:
                return
            current_vol, state = vol_state
            if state != "running":
                # No active stream — safe to set hardware to configured level
                await self.pulse.set_sink_volume(sink_name, hw_vol)
                logger.info(
                    "Hardware volume set to %d%% for %s (was %d%%, state=%s)",
                    hw_vol, address, current_vol, state,
                )
            else:
                # Stream active — sync MPD to current hardware volume
                await mpd.set_volume(current_vol)
                logger.info(
                    "MPD initial volume synced to hardware: %d%% for %s",
                    current_vol, address,
                )
        except Exception as e:
            logger.debug("MPD volume init for %s failed: %s", address, e)

    async def _stop_mpd(self, address: str) -> None:
        """Stop the MPD instance for a specific device (keeps port assigned)."""
        mpd = self._mpd_instances.pop(address, None)
        if mpd and mpd.is_running:
            await mpd.stop()

    async def update_device_settings(self, address: str, settings: dict) -> dict | None:
        """Update per-device settings and react to changes immediately."""
        device_info = await self.store.update_device_settings(address, settings)
        if device_info is None:
            return None

        # React to audio profile changes — fire as background task so
        # the settings API returns immediately and the modal can close.
        if "audio_profile" in settings:
            new_profile = settings["audio_profile"]
            if new_profile == "hfp" and self._null_hfp_registered:
                await self._unregister_null_hfp_handler()

            if address in self._device_connect_time and self.pulse:
                asyncio.create_task(
                    self._apply_audio_profile(address, new_profile)
                )

        # React to idle mode changes if device is connected
        idle_keys = {"idle_mode", "keep_alive_method", "power_save_delay", "auto_disconnect_minutes"}
        if address in self._device_connect_time and idle_keys.intersection(settings):
            new_mode = self.store.get_device_settings(address).get("idle_mode", "default")

            # Stop old handlers — but skip the sink resume when staying in
            # power_save, otherwise resume + immediate re-suspend race and
            # the suspend can silently fail (delay=0 case).
            staying_in_power_save = new_mode == "power_save"
            await self._stop_keepalive(address)
            self._cancel_pending_suspend(address)
            self._cancel_auto_disconnect_timer(address)
            if not staying_in_power_save and address in self._suspended_sinks and self.pulse:
                sink_name = await self.pulse.get_sink_for_address(address)
                if sink_name:
                    await self.pulse.resume_sink(sink_name)
                self._suspended_sinks.discard(address)

            # Apply new idle mode
            await self._apply_idle_mode(address)

            # If in power_save and sink is currently idle → schedule suspend
            if new_mode == "power_save" and self.pulse:
                sink_name = await self.pulse.get_sink_for_address(address)
                if sink_name:
                    vol_info = await self.pulse.get_sink_volume(sink_name)
                    if vol_info and vol_info[1] != "running":
                        s = self.store.get_device_settings(address)
                        delay = s.get("power_save_delay", 0)
                        self._schedule_sink_suspend(address, sink_name, delay)

        # React to MPD changes if device is connected
        if address in self._device_connect_time:
            mpd_changed = {"mpd_enabled", "mpd_port", "mpd_hw_volume"}.intersection(settings)
            if mpd_changed:
                if device_info.get("mpd_enabled", False):
                    # Restart to pick up any config changes (port, name)
                    await self._stop_mpd(address)
                    await self._start_mpd_if_enabled(address)
                else:
                    await self._stop_mpd(address)
                    await self.store.release_mpd_port(address)

        # Eagerly allocate a port when MPD is enabled so the UI shows it
        # immediately (even if the device isn't connected yet / no PA sink)
        if "mpd_enabled" in settings and device_info.get("mpd_enabled", False):
            if self.store.get_device_settings(address).get("mpd_port") is None:
                await self.store.allocate_mpd_port(address)

        # React to AVRCP changes if device is connected
        if address in self._device_connect_time and "avrcp_enabled" in settings:
            if self.media_player:
                if not device_info.get("avrcp_enabled", True):
                    # AVRCP just disabled — set Stopped
                    self.media_player.set_playback_status("Stopped")
                    logger.info("AVRCP disabled for %s — set PlaybackStatus=Stopped", address)
                else:
                    # AVRCP just enabled — set Playing if sink is currently running
                    if self.pulse:
                        sink_name = await self.pulse.get_sink_for_address(address)
                        if sink_name:
                            vol_info = await self.pulse.get_sink_volume(sink_name)
                            if vol_info and vol_info[1] == "running":
                                self.media_player.set_playback_status("Playing")
                                logger.info("AVRCP enabled for %s — sink running, set PlaybackStatus=Playing", address)

        await self._broadcast_devices()
        return device_info
