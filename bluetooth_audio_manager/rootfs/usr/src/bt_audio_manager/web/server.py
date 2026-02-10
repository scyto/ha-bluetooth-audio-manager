"""aiohttp web server for the add-on's ingress UI and REST API."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from .api import create_api_routes

if TYPE_CHECKING:
    from ..manager import BluetoothAudioManager

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
PORT = 8099


class WebServer:
    """Ingress web server providing the device management UI and REST API."""

    def __init__(self, manager: "BluetoothAudioManager"):
        self._manager = manager
        self._app = web.Application()
        self._runner: web.AppRunner | None = None

        # API routes
        api_routes = create_api_routes(manager)
        self._app.router.add_routes(api_routes)

        # Static files (UI)
        self._app.router.add_static("/static", STATIC_DIR)

        # Root serves the main page
        self._app.router.add_get("/", self._serve_index)

    async def _serve_index(self, request: web.Request) -> web.FileResponse:
        """Serve the main UI page."""
        return web.FileResponse(
            STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-cache"},
        )

    async def start(self) -> None:
        """Start the web server."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", PORT)
        await site.start()
        logger.info("Web server listening on port %d", PORT)

    async def stop(self) -> None:
        """Stop the web server."""
        if self._runner:
            await self._runner.cleanup()
        logger.info("Web server stopped")
