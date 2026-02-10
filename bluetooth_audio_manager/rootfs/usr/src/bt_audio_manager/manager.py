"""Top-level orchestrator for Bluetooth audio device management.

Coordinates all sub-components: BlueZ adapter, pairing agent, device
management, PulseAudio, reconnection, keep-alive, and the web server.
"""

import asyncio
import logging

from dbus_next.aio import MessageBus
from dbus_next import BusType
from dbus_next.errors import DBusError

from .audio.keepalive import KeepAliveService
from .audio.pulse import PulseAudioManager
from .bluez.adapter import BluezAdapter
from .bluez.agent import PairingAgent
from .bluez.device import BluezDevice
from .config import AppConfig
from .persistence.store import PersistenceStore
from .reconnect import ReconnectService

logger = logging.getLogger(__name__)


class BluetoothAudioManager:
    """Central orchestrator for the Bluetooth Audio Manager add-on."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.bus: MessageBus | None = None
        self.adapter: BluezAdapter | None = None
        self.agent: PairingAgent | None = None
        self.pulse: PulseAudioManager | None = None
        self.store: PersistenceStore | None = None
        self.reconnect_service: ReconnectService | None = None
        self.keepalive: KeepAliveService | None = None
        self.managed_devices: dict[str, BluezDevice] = {}
        self._web_server = None

    async def start(self) -> None:
        """Full startup sequence."""
        # 1. Connect to system D-Bus
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        logger.info("Connected to system D-Bus")

        # 2. Initialize BlueZ adapter
        self.adapter = BluezAdapter(self.bus)
        await self.adapter.initialize()

        # 3. Register pairing agent
        self.agent = PairingAgent(self.bus)
        await self.agent.register()

        # 4. Load persistent device store
        self.store = PersistenceStore()
        await self.store.load()

        # 5. Initialize PulseAudio manager
        self.pulse = PulseAudioManager()
        try:
            await self.pulse.connect()
        except Exception as e:
            logger.warning("PulseAudio connection failed (will retry): %s", e)
            self.pulse = None

        # 6. Start reconnection service
        self.reconnect_service = ReconnectService(self)
        await self.reconnect_service.start()

        # 7. Reconnect previously paired devices
        await self.reconnect_service.reconnect_all()

        # 8. Start keep-alive if enabled
        if self.config.keep_alive_enabled:
            self.keepalive = KeepAliveService(method=self.config.keep_alive_method)
            await self.keepalive.start()

        logger.info("Bluetooth Audio Manager started successfully")

    async def shutdown(self) -> None:
        """Graceful teardown in reverse order."""
        logger.info("Shutting down Bluetooth Audio Manager...")

        # Stop keep-alive
        if self.keepalive:
            await self.keepalive.stop()

        # Stop reconnection service
        if self.reconnect_service:
            await self.reconnect_service.stop()

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

    async def scan_devices(self, duration: int | None = None) -> list[dict]:
        """Run a time-limited discovery scan for A2DP audio devices."""
        duration = duration or self.config.scan_duration_seconds
        return await self.adapter.discover_for_duration(duration)

    async def pair_device(self, address: str) -> dict:
        """Pair, trust, and persist a Bluetooth audio device."""
        device = BluezDevice(self.bus, address)
        await device.initialize()

        # Pair
        await device.pair()

        # Trust (enables BlueZ-level auto-reconnect)
        await device.set_trusted(True)

        # Get name for display
        name = await device.get_name()

        # Register disconnect handler
        device.on_disconnected(self._on_device_disconnected)
        self.managed_devices[address] = device

        # Persist
        await self.store.add_device(address, name)

        logger.info("Device %s (%s) paired and stored", address, name)
        return {"address": address, "name": name, "connected": False}

    async def connect_device(self, address: str) -> bool:
        """Connect to a paired device and verify A2DP sink appears."""
        device = self.managed_devices.get(address)
        if not device:
            device = BluezDevice(self.bus, address)
            await device.initialize()
            device.on_disconnected(self._on_device_disconnected)
            self.managed_devices[address] = device

        await device.connect()
        await device.wait_for_services(timeout=10)

        # Verify PulseAudio sink appeared
        if self.pulse:
            sink_name = await self.pulse.wait_for_bt_sink(address, timeout=15)
            if sink_name:
                if self.keepalive:
                    self.keepalive.set_target_sink(sink_name)
                return True
            logger.warning("A2DP sink for %s did not appear in PulseAudio", address)
            return False

        # PulseAudio not available — connection may still work at BlueZ level
        return await device.is_connected()

    async def disconnect_device(self, address: str) -> None:
        """Disconnect a device without removing it from the store."""
        # Cancel any pending reconnection
        if self.reconnect_service:
            self.reconnect_service.cancel_reconnect(address)

        device = self.managed_devices.get(address)
        if device:
            await device.disconnect()

    async def forget_device(self, address: str) -> None:
        """Unpair, remove from BlueZ, and delete from persistent store."""
        # Cancel reconnection
        if self.reconnect_service:
            self.reconnect_service.cancel_reconnect(address)

        # Disconnect
        device = self.managed_devices.pop(address, None)
        if device:
            try:
                await device.disconnect()
            except DBusError:
                pass

        # Remove from BlueZ
        from .bluez.device import address_to_path
        device_path = address_to_path(address)
        await self.adapter.remove_device(device_path)

        # Remove from persistent store
        await self.store.remove_device(address)
        logger.info("Device %s forgotten", address)

    async def get_all_devices(self) -> list[dict]:
        """Get combined list of discovered and paired devices."""
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

    def _on_device_disconnected(self, address: str) -> None:
        """Handle device disconnection event."""
        if self.reconnect_service:
            self.reconnect_service.handle_disconnect(address)
