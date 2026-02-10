"""aiohttp web server for the add-on's ingress UI and REST API."""

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from .api import create_api_routes

if TYPE_CHECKING:
    from ..manager import BluetoothAudioManager

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
PORT = 8099

# Version used for cache-busting query strings on static assets
_BUILD_VERSION = os.environ.get("BUILD_VERSION", "dev")


@web.middleware
async def _no_cache_static(request: web.Request, handler):
    """Prevent browser caching of static assets."""
    response = await handler(request)
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


class WebServer:
    """Ingress web server providing the device management UI and REST API."""

    def __init__(self, manager: "BluetoothAudioManager"):
        self._manager = manager
        self._app = web.Application(middlewares=[_no_cache_static])
        self._runner: web.AppRunner | None = None
        self._index_html: str | None = None

        # API routes
        api_routes = create_api_routes(manager)
        self._app.router.add_routes(api_routes)

        # Static files (UI)
        self._app.router.add_static("/static", STATIC_DIR)

        # Root serves the main page
        self._app.router.add_get("/", self._serve_index)

    def _get_index_html(self) -> str:
        """Read index.html and inject cache-busting version query strings."""
        if self._index_html is None:
            raw = (STATIC_DIR / "index.html").read_text()
            # Append version query string to static asset URLs
            raw = raw.replace("static/style.css", f"static/style.css?v={_BUILD_VERSION}")
            raw = raw.replace("static/app.js", f"static/app.js?v={_BUILD_VERSION}")
            self._index_html = raw
        return self._index_html

    async def _serve_index(self, request: web.Request) -> web.Response:
        """Serve the main UI page with cache-busting asset URLs."""
        return web.Response(
            text=self._get_index_html(),
            content_type="text/html",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
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
