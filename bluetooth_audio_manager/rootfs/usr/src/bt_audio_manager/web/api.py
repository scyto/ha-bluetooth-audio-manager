"""REST API endpoints for the Bluetooth Audio Manager."""

import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from ..manager import BluetoothAudioManager

logger = logging.getLogger(__name__)


def create_api_routes(manager: "BluetoothAudioManager") -> list[web.RouteDef]:
    """Create all API route definitions."""
    routes = web.RouteTableDef()

    @routes.get("/api/health")
    async def health(request: web.Request) -> web.Response:
        """Liveness check for the HA watchdog."""
        return web.json_response({"status": "ok"})

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
            return web.json_response({"error": str(e)}, status=500)

    @routes.post("/api/connect")
    async def connect(request: web.Request) -> web.Response:
        """Connect to a paired device."""
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
            return web.json_response({"error": str(e)}, status=500)

    @routes.post("/api/disconnect")
    async def disconnect(request: web.Request) -> web.Response:
        """Disconnect a device without forgetting it."""
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
            return web.json_response({"error": str(e)}, status=500)

    @routes.post("/api/forget")
    async def forget(request: web.Request) -> web.Response:
        """Unpair and remove a device completely."""
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
            return web.json_response({"error": str(e)}, status=500)

    @routes.get("/api/audio/sinks")
    async def audio_sinks(request: web.Request) -> web.Response:
        """List Bluetooth PulseAudio sinks."""
        try:
            sinks = await manager.get_audio_sinks()
            return web.json_response({"sinks": sinks})
        except Exception as e:
            logger.error("Failed to list audio sinks: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    return routes
