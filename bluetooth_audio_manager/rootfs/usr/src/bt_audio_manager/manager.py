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
        self._last_sink_snapshot: str = ""
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
                        entry = {"command": "Volume", "detail": f"{vol_pct}%", "ts": time.time()}
                        self.recent_mpris.append(entry)
                        self.event_bus.emit("mpris_command", entry)
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

        # 2. Initialize BlueZ adapter
        self.adapter = BluezAdapter(self.bus)
        await self.adapter.initialize()

        # 3. Register pairing agent
        self.agent = PairingAgent(self.bus)
        await self.agent.register()

        # 3b. Register AVRCP media player (receives speaker button commands)
        self.media_player = AVRCPMediaPlayer(self.bus, self._on_avrcp_command)
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
                    logger.info("Device %s already connected, registering handlers", addr)
                    try:
                        await device.watch_media_player()
                    except Exception as e:
                        logger.debug("AVRCP on existing connection %s: %s", addr, e)
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

        device = BluezDevice(self.bus, address)
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
        # Cancel any pending auto-reconnect to avoid racing
        if self.reconnect_service:
            self.reconnect_service.cancel_reconnect(address)
        # Clear any disconnect suppression (user wants to connect now)
        self._suppress_reconnect.discard(address)
        self._broadcast_status(f"Connecting to {address}...")
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

        # Remove from BlueZ
        from .bluez.device import address_to_path
        device_path = address_to_path(address)
        await self.adapter.remove_device(device_path)

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
                    }
                )

        return discovered

    async def get_audio_sinks(self) -> list[dict]:
        """List Bluetooth PulseAudio sinks."""
        if not self.pulse:
            return []
        return await self.pulse.list_bt_sinks()

    # -- Sink state polling --

    async def _sink_poll_loop(self) -> None:
        """Periodically check PulseAudio sink state and broadcast changes.

        Detects idle→running transitions (playback started/stopped) that
        don't trigger D-Bus signals.
        """
        while True:
            try:
                await asyncio.sleep(self.SINK_POLL_INTERVAL)
                if not self.pulse:
                    continue
                sinks = await self.pulse.list_bt_sinks()
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
        """Push both device and sink state to SSE clients."""
        await self._broadcast_devices()
        await self._broadcast_sinks()

    def _broadcast_status(self, message: str) -> None:
        """Push a status message to SSE clients."""
        self.event_bus.emit("status", {"message": message})

    def _on_device_disconnected(self, address: str) -> None:
        """Handle device disconnection event."""
        if address in self._suppress_reconnect:
            # User-initiated disconnect — don't auto-reconnect
            self._suppress_reconnect.discard(address)
            logger.info("Skipping auto-reconnect for %s (user-initiated disconnect)", address)
        elif self.reconnect_service:
            self.reconnect_service.handle_disconnect(address)
        asyncio.ensure_future(self._broadcast_all())

    def _on_device_connected(self, address: str) -> None:
        """Handle device connection event (D-Bus signal)."""
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

        # Log MediaTransport1 properties for volume diagnosis
        await self._log_transport_properties(address)

    async def _log_transport_properties(self, address: str) -> None:
        """Enumerate BlueZ objects to find and log MediaTransport1 for a device.

        Waits briefly for the transport to appear (BlueZ may still be
        setting it up when the Connected signal fires).
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
                    return  # found it
            except Exception as e:
                logger.debug("Transport property check attempt %d failed: %s", attempt + 1, e)

        logger.info("No MediaTransport1 found for %s after 3 attempts", address)

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
