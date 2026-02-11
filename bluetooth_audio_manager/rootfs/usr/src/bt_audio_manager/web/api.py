"""REST API endpoints for the Bluetooth Audio Manager."""

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING

from aiohttp import web
from dbus_next.errors import DBusError

if TYPE_CHECKING:
    from ..manager import BluetoothAudioManager

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


async def _send_sse(
    response: web.StreamResponse, event: str, data: dict
) -> None:
    """Write a single SSE frame to the stream.

    Includes a 2 KB padding comment to push the data through the
    HA ingress proxy buffer (the proxy may hold small writes until
    enough data accumulates).
    """
    payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    # Pad to 8 KB to flush through the HA ingress proxy buffer.
    # The proxy holds small writes until enough data accumulates;
    # 8 KB exceeds typical proxy buffer thresholds (4 KB / 8 KB).
    target = 8192
    pad_len = max(0, target - len(payload) - 6)  # 6 = ": " + "\n\n"
    padding = ": " + "." * pad_len + "\n\n" if pad_len > 0 else ""
    await response.write((payload + padding).encode())


def create_api_routes(manager: "BluetoothAudioManager") -> list[web.RouteDef]:
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
        """Set the Bluetooth adapter for this add-on via the HA Supervisor API.

        Accepts {"adapter": "hci1"} and updates the add-on options.
        Requires a restart to take effect.
        """
        import aiohttp
        try:
            body = await request.json()
            adapter_name = body.get("adapter")
            if not adapter_name:
                return web.json_response(
                    {"error": "adapter is required"}, status=400
                )

            supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
            if not supervisor_token:
                return web.json_response(
                    {"error": "Supervisor API not available (not running in HAOS?)"}, status=500
                )

            # Read current options from Supervisor
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {supervisor_token}"}

                # Get current options
                async with session.get(
                    "http://supervisor/addons/self/options",
                    headers=headers,
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        return web.json_response(
                            {"error": f"Failed to read current options: {text}"}, status=500
                        )
                    result = await resp.json()
                    current_options = result.get("data", {}).get("options", {})

                # Update bt_adapter
                current_options["bt_adapter"] = adapter_name

                # Write back via Supervisor
                async with session.post(
                    "http://supervisor/addons/self/options",
                    headers=headers,
                    json={"options": current_options},
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        return web.json_response(
                            {"error": f"Failed to save options: {text}"}, status=500
                        )

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
            logger.error("Scan failed: %s", e)
            return web.json_response({"error": str(e)}, status=500)

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

    @routes.post("/api/debug/full-renegotiate")
    async def debug_full_renegotiate(request: web.Request) -> web.Response:
        """Debug: full disconnect + ConnectProfile(A2DP) renegotiation."""
        address = None
        try:
            body = await request.json()
            address = body.get("address")
            if not address:
                return web.json_response({"error": "address is required"}, status=400)
            result = await manager.debug_full_renegotiate(address)
            return web.json_response(result)
        except Exception as e:
            logger.error("debug_full_renegotiate failed for %s: %s", address, e)
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

    @routes.post("/api/debug/register-null-hfp")
    async def debug_register_null_hfp(request: web.Request) -> web.Response:
        """Debug: register null HFP profile handler to block HFP."""
        address = None
        try:
            body = await request.json()
            address = body.get("address")
            if not address:
                return web.json_response({"error": "address is required"}, status=400)
            result = await manager.debug_register_null_hfp(address)
            return web.json_response(result)
        except Exception as e:
            logger.error("debug_register_null_hfp failed for %s: %s", address, e)
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

    @routes.get("/api/events")
    async def sse_events(request: web.Request) -> web.StreamResponse:
        """Server-Sent Events stream for real-time UI updates."""
        logger.info("SSE client connected from %s", request.remote)
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)

        bus = manager.event_bus
        queue = bus.subscribe()
        try:
            # Tell EventSource to reconnect after 3 seconds if disconnected
            await response.write(b"retry: 3000\n\n")

            # Send initial state so UI renders immediately
            devices = await manager.get_all_devices()
            sinks = await manager.get_audio_sinks()
            await _send_sse(response, "devices_changed", {"devices": devices})
            await _send_sse(response, "sinks_changed", {"sinks": sinks})

            # Replay recent MPRIS/AVRCP events so reconnecting clients
            # don't lose transient events (HA ingress drops SSE often)
            for entry in manager.recent_mpris:
                await _send_sse(response, "mpris_command", entry)
            for entry in manager.recent_avrcp:
                await _send_sse(response, "avrcp_event", entry)

            logger.info("SSE initial state sent, streaming events...")

            # Stream events as they occur, with periodic heartbeat
            # to keep HA ingress proxy connection alive
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15)
                    logger.info("SSE sending: %s", msg["event"])
                    await _send_sse(response, msg["event"], msg["data"])
                except asyncio.TimeoutError:
                    # SSE comment keeps proxy alive — padded to 8 KB
                    # to flush through the HA ingress proxy buffer
                    heartbeat = ": heartbeat\n" + ": " + "." * 8172 + "\n\n"
                    await response.write(heartbeat.encode())
        except (ConnectionResetError, ConnectionError, asyncio.CancelledError) as e:
            logger.info("SSE stream closed: %s", type(e).__name__)
        except Exception as e:
            logger.warning("SSE stream unexpected error: %s: %s", type(e).__name__, e)
        finally:
            bus.unsubscribe(queue)
            logger.info("SSE client disconnected")
        return response

    return routes
