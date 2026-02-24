"""REST API endpoints for the Bluetooth Audio Manager."""

import asyncio
import logging
import os
import re
from typing import TYPE_CHECKING

from aiohttp import web
from aiohttp.web import WebSocketResponse
from dbus_next.errors import DBusError

if TYPE_CHECKING:
    from ..manager import BluetoothAudioManager
    from .log_handler import WebSocketLogHandler

logger = logging.getLogger(__name__)

# Strict Bluetooth MAC address pattern (AA:BB:CC:DD:EE:FF)
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")

# Map common BlueZ D-Bus error strings to user-friendly messages
_BLUEZ_ERROR_MAP = {
    "Page Timeout": "Device not responding. Make sure it is in pairing mode and nearby.",
    "In Progress": "A pairing or connection attempt is already in progress. Please wait.",
    "Already Exists": "Device is already paired.",
    "Does Not Exist": "Device not found. Try scanning again.",
    "Not Ready": "Bluetooth adapter is not ready. Try again in a moment.",
    "Connection refused": "Device refused the connection. Is it in pairing mode?",
    "br-connection-canceled": "Connection was canceled (device may have been busy).",
    "br-connection-busy": "A connection attempt is already in progress. Please wait.",
    "le-connection-abort-by-local": "Connection aborted locally.",
    "Software caused connection abort": "Connection dropped unexpectedly. Try again.",
    "Host is down": "Device is not reachable. Make sure it is powered on and nearby.",
}


def _friendly_error(e: Exception) -> str:
    """Convert a DBusError or other exception to a user-friendly message."""
    msg = str(e)
    if isinstance(e, DBusError):
        for pattern, friendly in _BLUEZ_ERROR_MAP.items():
            if pattern in msg:
                return friendly
    # Don't leak raw D-Bus internals to the client
    logger.debug("Unmapped error returned to client: %s", msg)
    return "Operation failed. Check add-on logs for details."


def _get_validated_address(body: dict) -> tuple[str | None, web.Response | None]:
    """Extract and validate a Bluetooth MAC address from a request body.

    Returns (address, None) on success or (None, error_response) on failure.
    """
    address = body.get("address")
    if not address:
        return None, web.json_response({"error": "address is required"}, status=400)
    if not isinstance(address, str) or not _MAC_RE.match(address):
        return None, web.json_response(
            {"error": "Invalid Bluetooth address format (expected XX:XX:XX:XX:XX:XX)"},
            status=400,
        )
    return address, None


async def _ws_sender(
    ws: WebSocketResponse, queue: asyncio.Queue,
) -> None:
    """Forward EventBus events to a WebSocket client."""
    try:
        while not ws.closed:
            msg = await queue.get()
            event = msg["event"]
            data = msg["data"]
            await ws.send_json({"type": event, **data})
    except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
        pass


def create_api_routes(
    manager: "BluetoothAudioManager",
    log_handler: "WebSocketLogHandler | None" = None,
) -> list[web.RouteDef]:
    """Create all API route definitions."""
    routes = web.RouteTableDef()

    @routes.get("/api/health")
    async def health(request: web.Request) -> web.Response:
        """Liveness check for the HA watchdog."""
        return web.json_response({"status": "ok"})

    @routes.get("/api/info")
    async def info(request: web.Request) -> web.Response:
        """Return app version and adapter info for the UI."""
        import os
        from ..bluez.constants import HFP_SWITCHING_ENABLED
        path = manager._adapter_path or "/org/bluez/hci0"
        adapter_name = path.rsplit("/", 1)[-1]
        return web.json_response({
            "version": os.environ.get("BUILD_VERSION", "dev"),
            "adapter": adapter_name,
            "adapter_path": path,
            "adapter_mac": manager.config.bt_adapter
            if manager.config.bt_adapter_is_mac else None,
            "hfp_switching_enabled": HFP_SWITCHING_ENABLED,
        })

    @routes.get("/api/adapters")
    async def list_adapters(request: web.Request) -> web.Response:
        """List all Bluetooth adapters on the system."""
        try:
            adapters = await manager.list_adapters()
            return web.json_response({"adapters": adapters})
        except Exception as e:
            logger.error("Failed to list adapters: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    @routes.post("/api/set-adapter")
    async def set_adapter(request: web.Request) -> web.Response:
        """Set the Bluetooth adapter. Persists to settings.json.

        Accepts {"adapter": "hci1", "clean": true}.
        When clean=true, disconnects and removes all devices before saving.
        Requires a restart to take effect.
        """
        try:
            body = await request.json()
            adapter_name = body.get("adapter")
            if not adapter_name or not isinstance(adapter_name, str):
                return web.json_response(
                    {"error": "adapter is required and must be a string"}, status=400
                )
            # Validate format: "auto", MAC address, or legacy hciN name
            valid = (
                adapter_name == "auto"
                or _MAC_RE.match(adapter_name)
                or re.match(r"^hci\d+$", adapter_name)
            )
            if not valid:
                return web.json_response(
                    {"error": "adapter must be 'auto', a MAC address, or an hciN name"},
                    status=400,
                )

            clean = body.get("clean", False)
            if clean:
                await manager.clear_all_devices()

            manager.config.bt_adapter = adapter_name
            manager.config.save_settings()

            logger.info(
                "Adapter selection changed to %s (restart required, clean=%s)",
                adapter_name, clean,
            )
            return web.json_response({
                "adapter": adapter_name,
                "restart_required": True,
                "cleaned": clean,
            })
        except Exception as e:
            logger.error("Failed to set adapter: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    @routes.post("/api/restart")
    async def restart_addon(request: web.Request) -> web.Response:
        """Restart this app via the HA Supervisor API."""
        import aiohttp
        try:
            supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
            if not supervisor_token:
                return web.json_response(
                    {"error": "Supervisor API not available"}, status=500
                )

            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {supervisor_token}"}
                async with session.post(
                    "http://supervisor/addons/self/restart",
                    headers=headers,
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        return web.json_response(
                            {"error": f"Restart failed: {text}"}, status=500
                        )

            return web.json_response({"restarting": True})
        except Exception as e:
            logger.error("Failed to restart app: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    @routes.get("/api/devices")
    async def list_devices(request: web.Request) -> web.Response:
        """List all discovered and paired audio devices."""
        try:
            devices = await manager.get_all_devices()
            return web.json_response({"devices": devices})
        except Exception as e:
            logger.error("Failed to list devices: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    @routes.post("/api/scan")
    async def scan(request: web.Request) -> web.Response:
        """Start a background discovery scan for Bluetooth audio devices.

        Returns immediately. Devices appear incrementally via WebSocket
        'devices_changed' events.
        """
        try:
            body = await request.json() if request.body_exists else {}
            duration = body.get("duration", manager.config.scan_duration_seconds)
            await manager.scan_devices(duration)
            return web.json_response({"scanning": True, "duration": duration})
        except Exception as e:
            if "In Progress" in str(e):
                return web.json_response({"scanning": True})
            logger.error("Scan failed: %s", e)
            return web.json_response({"error": _friendly_error(e)}, status=500)

    @routes.get("/api/scan/status")
    async def scan_status(request: web.Request) -> web.Response:
        """Check if a scan is currently in progress."""
        return web.json_response({"scanning": manager.is_scanning})

    @routes.post("/api/pair")
    async def pair(request: web.Request) -> web.Response:
        """Pair and trust a Bluetooth audio device."""
        address = None
        try:
            body = await request.json()
            address, err = _get_validated_address(body)
            if err:
                return err
            result = await manager.pair_device(address)
            return web.json_response(result)
        except Exception as e:
            logger.error("Pair failed for %s: %s", address, e)
            return web.json_response({"error": _friendly_error(e)}, status=500)

    @routes.post("/api/connect")
    async def connect(request: web.Request) -> web.Response:
        """Connect to a paired device."""
        address = None
        try:
            body = await request.json()
            address, err = _get_validated_address(body)
            if err:
                return err
            success = await manager.connect_device(address)
            return web.json_response({"connected": success, "address": address})
        except Exception as e:
            logger.error("Connect failed for %s: %s", address, e)
            return web.json_response({"error": _friendly_error(e)}, status=500)

    @routes.post("/api/disconnect")
    async def disconnect(request: web.Request) -> web.Response:
        """Disconnect a device without forgetting it."""
        address = None
        try:
            body = await request.json()
            address, err = _get_validated_address(body)
            if err:
                return err
            await manager.disconnect_device(address)
            return web.json_response({"disconnected": True, "address": address})
        except Exception as e:
            logger.error("Disconnect failed for %s: %s", address, e)
            return web.json_response({"error": _friendly_error(e)}, status=500)

    @routes.post("/api/force-reconnect")
    async def force_reconnect(request: web.Request) -> web.Response:
        """Force disconnect + reconnect cycle for zombie connections."""
        address = None
        try:
            body = await request.json()
            address, err = _get_validated_address(body)
            if err:
                return err
            success = await manager.force_reconnect_device(address)
            return web.json_response({"reconnected": success, "address": address})
        except Exception as e:
            logger.error("Force reconnect failed for %s: %s", address, e)
            return web.json_response({"error": _friendly_error(e)}, status=500)

    @routes.post("/api/forget")
    async def forget(request: web.Request) -> web.Response:
        """Unpair and remove a device completely."""
        address = None
        try:
            body = await request.json()
            address, err = _get_validated_address(body)
            if err:
                return err
            await manager.forget_device(address)
            return web.json_response({"forgotten": True, "address": address})
        except Exception as e:
            logger.error("Forget failed for %s: %s", address, e)
            return web.json_response({"error": _friendly_error(e)}, status=500)

    @routes.put("/api/devices/{address}/settings")
    async def update_device_settings(request: web.Request) -> web.Response:
        """Update per-device settings (keep-alive, etc.)."""
        address = request.match_info["address"]
        if not _MAC_RE.match(address):
            return web.json_response(
                {"error": "Invalid Bluetooth address format (expected XX:XX:XX:XX:XX:XX)"},
                status=400,
            )
        try:
            # Auto-store paired devices not yet in the persistence store
            # (can happen when BlueZ paired a device before the add-on tracked it,
            # or device connected after startup missed the Phase 6b import)
            if manager.store.get_device(address) is None:
                bluez_dev = manager.managed_devices.get(address)
                if not bluez_dev:
                    try:
                        bluez_dev = await manager._get_or_create_device(address)
                    except Exception:
                        bluez_dev = None
                if bluez_dev:
                    name = await bluez_dev.get_name()
                    await manager.store.add_device(address, name)
                    logger.info("Auto-stored BlueZ device %s (%s)", address, name)
                else:
                    return web.json_response(
                        {"error": f"Device {address} not found"}, status=404
                    )

            body = await request.json()
            allowed_keys = {
                "audio_profile",
                "idle_mode", "keep_alive_method",
                "power_save_delay", "auto_disconnect_minutes",
                "mpd_enabled", "mpd_port", "mpd_hw_volume",
                "avrcp_enabled",
            }
            settings = {k: v for k, v in body.items() if k in allowed_keys}
            if not settings:
                return web.json_response(
                    {"error": "No valid settings provided"}, status=400
                )
            if "audio_profile" in settings:
                from ..bluez.constants import HFP_SWITCHING_ENABLED
                if not HFP_SWITCHING_ENABLED:
                    # HFP switching disabled (SCO unavailable) — ignore silently
                    del settings["audio_profile"]
                elif settings["audio_profile"] not in ("a2dp", "hfp"):
                    return web.json_response(
                        {"error": "audio_profile must be 'a2dp' or 'hfp'"}, status=400
                    )
            if "idle_mode" in settings:
                valid_modes = ("default", "power_save", "keep_alive", "auto_disconnect")
                if settings["idle_mode"] not in valid_modes:
                    return web.json_response(
                        {"error": f"idle_mode must be one of {valid_modes}"}, status=400
                    )
            if "keep_alive_method" in settings:
                if settings["keep_alive_method"] not in ("silence", "infrasound"):
                    return web.json_response(
                        {"error": "keep_alive_method must be 'silence' or 'infrasound'"},
                        status=400,
                    )
            if "power_save_delay" in settings:
                val = settings["power_save_delay"]
                if not isinstance(val, int) or val < 0 or val > 300:
                    return web.json_response(
                        {"error": "power_save_delay must be 0-300 seconds"}, status=400
                    )
            if "auto_disconnect_minutes" in settings:
                val = settings["auto_disconnect_minutes"]
                if not isinstance(val, int) or val < 5 or val > 60:
                    return web.json_response(
                        {"error": "auto_disconnect_minutes must be 5-60"}, status=400
                    )
            if "mpd_enabled" in settings:
                if not isinstance(settings["mpd_enabled"], bool):
                    return web.json_response(
                        {"error": "mpd_enabled must be a boolean"}, status=400
                    )
            if "avrcp_enabled" in settings:
                if not isinstance(settings["avrcp_enabled"], bool):
                    return web.json_response(
                        {"error": "avrcp_enabled must be a boolean"}, status=400
                    )
            if "mpd_port" in settings:
                port = settings["mpd_port"]
                if not isinstance(port, int) or port < 6600 or port > 6609:
                    return web.json_response(
                        {"error": "mpd_port must be an integer 6600-6609"}, status=400
                    )
                used = manager.store._used_mpd_ports()
                if port in used and used[port] != address:
                    return web.json_response(
                        {"error": f"Port {port} is already in use by another device"},
                        status=409,
                    )
                await manager.store.set_mpd_port(address, port)
            if "mpd_hw_volume" in settings:
                v = settings["mpd_hw_volume"]
                if not isinstance(v, int) or v < 1 or v > 100:
                    return web.json_response(
                        {"error": "mpd_hw_volume must be an integer 1-100"}, status=400
                    )
            result = await manager.update_device_settings(address, settings)
            if result is None:
                return web.json_response(
                    {"error": f"Device {address} not found"}, status=404
                )
            # Return current settings (includes auto-allocated port, etc.)
            current = manager.store.get_device_settings(address)
            return web.json_response({"address": address, "settings": current})
        except Exception as e:
            logger.error("Failed to update settings for %s: %s", address, e)
            return web.json_response({"error": str(e)}, status=500)

    @routes.get("/api/settings")
    async def get_settings(request: web.Request) -> web.Response:
        """Return current runtime settings (auto_reconnect, intervals, etc.)."""
        return web.json_response(manager.config.runtime_settings)

    @routes.put("/api/settings")
    async def update_settings(request: web.Request) -> web.Response:
        """Update runtime settings (hot-reload, no restart needed)."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        # Validate and apply each setting
        errors = []
        if "auto_reconnect" in body:
            if not isinstance(body["auto_reconnect"], bool):
                errors.append("auto_reconnect must be a boolean")
        if "reconnect_interval_seconds" in body:
            v = body["reconnect_interval_seconds"]
            if not isinstance(v, int) or v < 5 or v > 600:
                errors.append("reconnect_interval_seconds must be an integer between 5 and 600")
        if "reconnect_max_backoff_seconds" in body:
            v = body["reconnect_max_backoff_seconds"]
            if not isinstance(v, int) or v < 60 or v > 3600:
                errors.append("reconnect_max_backoff_seconds must be an integer between 60 and 3600")
        if "scan_duration_seconds" in body:
            v = body["scan_duration_seconds"]
            if not isinstance(v, int) or v < 5 or v > 120:
                errors.append("scan_duration_seconds must be an integer between 5 and 120")

        if errors:
            return web.json_response({"error": "; ".join(errors)}, status=400)

        # Apply to live config
        allowed = {"auto_reconnect", "reconnect_interval_seconds",
                    "reconnect_max_backoff_seconds", "scan_duration_seconds"}
        for key in allowed:
            if key in body:
                setattr(manager.config, key, body[key])

        # Persist
        manager.config.save_settings()

        # Broadcast change to all WS clients
        manager.event_bus.emit("settings_changed", manager.config.runtime_settings)

        logger.info("Runtime settings updated: %s", manager.config.runtime_settings)
        return web.json_response(manager.config.runtime_settings)

    @routes.get("/api/audio/sinks")
    async def audio_sinks(request: web.Request) -> web.Response:
        """List Bluetooth PulseAudio sinks."""
        try:
            sinks = await manager.get_audio_sinks()
            return web.json_response({"sinks": sinks})
        except Exception as e:
            logger.error("Failed to list audio sinks: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    @routes.get("/api/state")
    async def state(request: web.Request) -> web.Response:
        """Combined state endpoint for UI polling.

        Returns devices, sinks, and recent MPRIS/AVRCP events in one
        request.  The client passes ``?mpris_after=<ts>&avrcp_after=<ts>``
        to receive only events newer than the given timestamps.
        """
        try:
            mpris_after = float(request.query.get("mpris_after", 0))
            avrcp_after = float(request.query.get("avrcp_after", 0))

            devices = await manager.get_all_devices()
            sinks = await manager.get_audio_sinks()

            mpris = [e for e in manager.recent_mpris if e["ts"] > mpris_after]
            avrcp = [e for e in manager.recent_avrcp if e["ts"] > avrcp_after]

            return web.json_response({
                "devices": devices,
                "sinks": sinks,
                "mpris_events": mpris,
                "avrcp_events": avrcp,
            })
        except Exception as e:
            logger.error("Failed to get state: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    # ---- Debug endpoints for interactive AVRCP testing ----

    @routes.post("/api/debug/avrcp-cycle")
    async def debug_avrcp_cycle(request: web.Request) -> web.Response:
        """Debug: cycle AVRCP profiles only."""
        address = None
        try:
            body = await request.json()
            address, err = _get_validated_address(body)
            if err:
                return err
            result = await manager.debug_avrcp_cycle(address)
            return web.json_response(result)
        except Exception as e:
            logger.error("debug_avrcp_cycle failed for %s: %s", address, e)
            return web.json_response({"error": _friendly_error(e)}, status=500)

    @routes.post("/api/debug/mpris-reregister")
    async def debug_mpris_reregister(request: web.Request) -> web.Response:
        """Debug: unregister + re-register MPRIS player."""
        address = None
        try:
            body = await request.json()
            address, err = _get_validated_address(body)
            if err:
                return err
            result = await manager.debug_mpris_reregister(address)
            return web.json_response(result)
        except Exception as e:
            logger.error("debug_mpris_reregister failed for %s: %s", address, e)
            return web.json_response({"error": _friendly_error(e)}, status=500)

    @routes.post("/api/debug/mpris-avrcp-cycle")
    async def debug_mpris_avrcp_cycle(request: web.Request) -> web.Response:
        """Debug: unregister MPRIS, cycle AVRCP, re-register MPRIS."""
        address = None
        try:
            body = await request.json()
            address, err = _get_validated_address(body)
            if err:
                return err
            result = await manager.debug_mpris_avrcp_cycle(address)
            return web.json_response(result)
        except Exception as e:
            logger.error("debug_mpris_avrcp_cycle failed for %s: %s", address, e)
            return web.json_response({"error": _friendly_error(e)}, status=500)

    @routes.post("/api/debug/disconnect-hfp")
    async def debug_disconnect_hfp(request: web.Request) -> web.Response:
        """Debug: disconnect HFP profile to force AVRCP volume."""
        address = None
        try:
            body = await request.json()
            address, err = _get_validated_address(body)
            if err:
                return err
            result = await manager.debug_disconnect_hfp(address)
            return web.json_response(result)
        except Exception as e:
            logger.error("debug_disconnect_hfp failed for %s: %s", address, e)
            return web.json_response({"error": _friendly_error(e)}, status=500)

    @routes.post("/api/debug/hfp-reconnect-cycle")
    async def debug_hfp_reconnect_cycle(request: web.Request) -> web.Response:
        """Debug: disconnect HFP, full reconnect, disconnect HFP again."""
        address = None
        try:
            body = await request.json()
            address, err = _get_validated_address(body)
            if err:
                return err
            result = await manager.debug_hfp_reconnect_cycle(address)
            return web.json_response(result)
        except Exception as e:
            logger.error("debug_hfp_reconnect_cycle failed for %s: %s", address, e)
            return web.json_response({"error": _friendly_error(e)}, status=500)

    @routes.get("/api/diagnostics/mpris")
    async def diagnostics_mpris(request: web.Request) -> web.Response:
        """Diagnostic endpoint for MPRIS/AVRCP troubleshooting."""
        from ..bluez.constants import (
            BLUEZ_SERVICE, OBJECT_MANAGER_INTERFACE, PLAYER_PATH,
        )
        results = {}
        bus = manager.bus

        # 1. Check registration state
        results["player_registered"] = (
            manager.media_player is not None
            and manager.media_player._registered
        )

        if not bus:
            results["error"] = "D-Bus not connected"
            return web.json_response(results)

        results["bus_name"] = bus.unique_name
        results["player_path"] = PLAYER_PATH

        # 2. Check local export (no D-Bus round-trip — system bus policy
        #    blocks method calls to our own unique name, but BlueZ has
        #    elevated permissions and CAN call us).
        from ..bluez.media_player import MPRISPlayerInterface
        try:
            exported_ifaces = bus._path_exports.get(PLAYER_PATH, [])
            results["player_exported"] = any(
                isinstance(i, MPRISPlayerInterface) for i in exported_ifaces
            )
        except Exception as e:
            results["player_exported"] = False
            results["player_export_error"] = str(e)

        # 3. Verify our bus name is active on the system bus
        try:
            dbus_intro = await bus.introspect(
                "org.freedesktop.DBus", "/org/freedesktop/DBus",
            )
            dbus_proxy = bus.get_proxy_object(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", dbus_intro,
            )
            dbus_iface = dbus_proxy.get_interface("org.freedesktop.DBus")
            has_owner = await dbus_iface.call_name_has_owner(bus.unique_name)
            results["bus_name_active"] = has_owner
        except Exception as e:
            results["bus_name_active_error"] = str(e)

        # NOTE: D-Bus round-trip self-test is not possible on the system bus
        # because the default policy denies method_call to arbitrary unique
        # names.  BlueZ (running as root) can still call our methods.

        # 4. Enumerate all BlueZ objects — show device interfaces
        #    (especially MediaControl1 which indicates AVRCP is active)
        #    and any MediaPlayer1 objects.
        try:
            intro = await bus.introspect(BLUEZ_SERVICE, "/")
            proxy = bus.get_proxy_object(BLUEZ_SERVICE, "/", intro)
            obj_mgr = proxy.get_interface(OBJECT_MANAGER_INTERFACE)
            objects = await obj_mgr.call_get_managed_objects()

            bluez_players = []
            device_details = []
            transport_details = []
            for path, ifaces in objects.items():
                if "org.bluez.MediaPlayer1" in ifaces:
                    props = ifaces["org.bluez.MediaPlayer1"]
                    bluez_players.append({
                        "path": path,
                        "status": str(props.get("Status", {}).value)
                            if props.get("Status") else "unknown",
                    })
                if "org.bluez.Device1" in ifaces:
                    dev_props = ifaces["org.bluez.Device1"]
                    addr = str(dev_props.get("Address", {}).value) if dev_props.get("Address") else "?"
                    connected = dev_props.get("Connected", {}).value if dev_props.get("Connected") else False
                    iface_names = sorted(ifaces.keys())
                    device_details.append({
                        "path": path,
                        "address": addr,
                        "connected": connected,
                        "interfaces": iface_names,
                        "has_media_control": "org.bluez.MediaControl1" in ifaces,
                        "has_media_transport": "org.bluez.MediaTransport1" in ifaces,
                    })
                if "org.bluez.MediaTransport1" in ifaces:
                    tp = ifaces["org.bluez.MediaTransport1"]
                    # Extract all transport properties for diagnosis
                    tp_info = {"path": path}
                    for key in ("Device", "UUID", "Codec", "State", "Volume"):
                        v = tp.get(key)
                        if v is not None:
                            tp_info[key.lower()] = (
                                v.value if hasattr(v, "value") else str(v)
                            )
                    tp_info["volume_supported"] = "Volume" in tp
                    tp_info["all_properties"] = sorted(tp.keys())
                    transport_details.append(tp_info)
            results["bluez_media_players"] = bluez_players
            results["bluez_devices"] = device_details
            results["bluez_transports"] = transport_details
        except Exception as e:
            results["bluez_objects_error"] = str(e)

        return web.json_response(results)

    @routes.get("/api/logs")
    async def get_logs(request: web.Request) -> web.Response:
        """Return recent application log entries."""
        if log_handler is None:
            return web.json_response({"logs": []})
        return web.json_response({"logs": list(log_handler.recent_logs)})

    @routes.get("/api/ws")
    async def websocket_handler(request: web.Request) -> WebSocketResponse:
        """WebSocket endpoint for real-time UI updates.

        Replaces SSE which is broken through HA ingress due to a
        compression bug (supervisor#6470).  WebSocket bypasses both
        the compression issue and the HA service worker.
        """
        ws = WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        logger.info("WS client connected from %s", request.remote)

        bus = manager.event_bus
        sender: asyncio.Task | None = None
        queue: asyncio.Queue | None = None
        try:
            # Send initial state so UI renders immediately.
            # NOTE: get_all_devices() triggers adapter scanning which
            # emits log events.  We must NOT subscribe to the EventBus
            # until after the historical replay so that live events
            # don't race ahead of replayed entries in the client.
            devices = await manager.get_all_devices()
            try:
                sinks = await manager.get_audio_sinks()
            except Exception:
                logger.warning("PA unavailable during WS init — sending empty sinks")
                sinks = []
            await ws.send_json({"type": "devices_changed", "devices": devices})
            await ws.send_json({"type": "sinks_changed", "sinks": sinks})
            await ws.send_json({
                "type": "scan_state",
                "scanning": manager.is_scanning,
            })

            # Replay recent MPRIS/AVRCP events
            for entry in manager.recent_mpris:
                await ws.send_json({"type": "mpris_command", **entry})
            for entry in manager.recent_avrcp:
                await ws.send_json({"type": "avrcp_event", **entry})

            # Replay recent log entries
            if log_handler:
                for entry in log_handler.recent_logs:
                    await ws.send_json({"type": "log_entry", **entry})

            # Deliver pending toasts (e.g. device reimport warning from startup)
            if manager._pending_toasts:
                for toast in manager._pending_toasts:
                    await ws.send_json({"type": "toast", **toast})
                manager._pending_toasts.clear()

            # Subscribe to live events AFTER replay so log order is
            # preserved.  Events generated during replay (e.g. from
            # get_all_devices) are already in the ring buffer.
            queue = bus.subscribe()
            sender = asyncio.create_task(_ws_sender(ws, queue))

            # Block until client disconnects (reads drain client msgs)
            async for _msg in ws:
                pass
        except (ConnectionResetError, ConnectionError) as e:
            logger.info("WS stream closed: %s", type(e).__name__)
        except Exception as e:
            logger.warning("WS unexpected error: %s: %s", type(e).__name__, e)
        finally:
            if sender is not None:
                sender.cancel()
                try:
                    await sender
                except asyncio.CancelledError:
                    pass
            if queue is not None:
                bus.unsubscribe(queue)
            logger.info("WS client disconnected")
        return ws

    return routes
