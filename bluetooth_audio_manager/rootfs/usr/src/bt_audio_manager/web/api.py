"""REST API endpoints for the Bluetooth Audio Manager."""

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from aiohttp import web
from aiohttp.web import WebSocketResponse
from dbus_next.errors import DBusError

if TYPE_CHECKING:
    from ..manager import BluetoothAudioManager
    from .log_handler import WebSocketLogHandler

logger = logging.getLogger(__name__)

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
    return msg


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
        """Return add-on version and adapter info for the UI."""
        import os
        adapter_name = manager._adapter_path.rsplit("/", 1)[-1]
        return web.json_response({
            "version": os.environ.get("BUILD_VERSION", "dev"),
            "adapter": adapter_name,
            "adapter_path": manager._adapter_path,
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

        Accepts {"adapter": "hci1"}. Requires a restart to take effect.
        """
        try:
            body = await request.json()
            adapter_name = body.get("adapter")
            if not adapter_name:
                return web.json_response(
                    {"error": "adapter is required"}, status=400
                )

            manager.config.bt_adapter = adapter_name
            manager.config.save_settings()

            logger.info("Adapter selection changed to %s (restart required)", adapter_name)
            return web.json_response({
                "adapter": adapter_name,
                "restart_required": True,
            })
        except Exception as e:
            logger.error("Failed to set adapter: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    @routes.post("/api/restart")
    async def restart_addon(request: web.Request) -> web.Response:
        """Restart this add-on via the HA Supervisor API."""
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
            logger.error("Failed to restart add-on: %s", e)
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
        """Start a discovery scan for Bluetooth audio devices."""
        try:
            body = await request.json() if request.body_exists else {}
            duration = body.get("duration", manager.config.scan_duration_seconds)
            devices = await manager.scan_devices(duration)
            return web.json_response({"devices": devices})
        except Exception as e:
            if "In Progress" in str(e):
                # Scan already running — not an error
                return web.json_response({"scanning": True})
            logger.error("Scan failed: %s", e)
            return web.json_response({"error": _friendly_error(e)}, status=500)

    @routes.post("/api/pair")
    async def pair(request: web.Request) -> web.Response:
        """Pair and trust a Bluetooth audio device."""
        address = None
        try:
            body = await request.json()
            address = body.get("address")
            if not address:
                return web.json_response(
                    {"error": "address is required"}, status=400
                )
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
            address = body.get("address")
            if not address:
                return web.json_response(
                    {"error": "address is required"}, status=400
                )
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
            address = body.get("address")
            if not address:
                return web.json_response(
                    {"error": "address is required"}, status=400
                )
            await manager.disconnect_device(address)
            return web.json_response({"disconnected": True, "address": address})
        except Exception as e:
            logger.error("Disconnect failed for %s: %s", address, e)
            return web.json_response({"error": _friendly_error(e)}, status=500)

    @routes.post("/api/forget")
    async def forget(request: web.Request) -> web.Response:
        """Unpair and remove a device completely."""
        address = None
        try:
            body = await request.json()
            address = body.get("address")
            if not address:
                return web.json_response(
                    {"error": "address is required"}, status=400
                )
            await manager.forget_device(address)
            return web.json_response({"forgotten": True, "address": address})
        except Exception as e:
            logger.error("Forget failed for %s: %s", address, e)
            return web.json_response({"error": _friendly_error(e)}, status=500)

    @routes.put("/api/devices/{address}/settings")
    async def update_device_settings(request: web.Request) -> web.Response:
        """Update per-device settings (keep-alive, etc.)."""
        address = request.match_info["address"]
        try:
            body = await request.json()
            allowed_keys = {"keep_alive_enabled", "keep_alive_method"}
            settings = {k: v for k, v in body.items() if k in allowed_keys}
            if not settings:
                return web.json_response(
                    {"error": "No valid settings provided"}, status=400
                )
            if "keep_alive_method" in settings:
                if settings["keep_alive_method"] not in ("silence", "infrasound"):
                    return web.json_response(
                        {"error": "keep_alive_method must be 'silence' or 'infrasound'"},
                        status=400,
                    )
            if "keep_alive_enabled" in settings:
                if not isinstance(settings["keep_alive_enabled"], bool):
                    return web.json_response(
                        {"error": "keep_alive_enabled must be a boolean"}, status=400
                    )
            result = await manager.update_device_settings(address, settings)
            if result is None:
                return web.json_response(
                    {"error": f"Device {address} not found"}, status=404
                )
            return web.json_response({"address": address, "settings": settings})
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
            if not isinstance(v, int) or v < 5 or v > 60:
                errors.append("scan_duration_seconds must be an integer between 5 and 60")

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
            address = body.get("address")
            if not address:
                return web.json_response({"error": "address is required"}, status=400)
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
            address = body.get("address")
            if not address:
                return web.json_response({"error": "address is required"}, status=400)
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
            address = body.get("address")
            if not address:
                return web.json_response({"error": "address is required"}, status=400)
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
            address = body.get("address")
            if not address:
                return web.json_response({"error": "address is required"}, status=400)
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
            address = body.get("address")
            if not address:
                return web.json_response({"error": "address is required"}, status=400)
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
        queue = bus.subscribe()
        sender = asyncio.create_task(_ws_sender(ws, queue))
        try:
            # Send initial state so UI renders immediately
            devices = await manager.get_all_devices()
            sinks = await manager.get_audio_sinks()
            await ws.send_json({"type": "devices_changed", "devices": devices})
            await ws.send_json({"type": "sinks_changed", "sinks": sinks})

            # Replay recent MPRIS/AVRCP events
            for entry in manager.recent_mpris:
                await ws.send_json({"type": "mpris_command", **entry})
            for entry in manager.recent_avrcp:
                await ws.send_json({"type": "avrcp_event", **entry})

            # Replay recent log entries
            if log_handler:
                for entry in log_handler.recent_logs:
                    await ws.send_json({"type": "log_entry", **entry})

            # Block until client disconnects (reads drain client msgs)
            async for _msg in ws:
                pass
        except (ConnectionResetError, ConnectionError) as e:
            logger.info("WS stream closed: %s", type(e).__name__)
        except Exception as e:
            logger.warning("WS unexpected error: %s: %s", type(e).__name__, e)
        finally:
            sender.cancel()
            bus.unsubscribe(queue)
            logger.info("WS client disconnected")
        return ws

    return routes
