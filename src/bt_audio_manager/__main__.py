"""Entry point for the Bluetooth Audio Manager app."""

import asyncio
import logging
import os
import signal
import sys

from .config import AppConfig
from .manager import BluetoothAudioManager
from .web.log_handler import WebSocketLogHandler
from .web.server import WebServer

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(level_name: str) -> None:
    """Configure logging to stdout (captured by Docker/HA)."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        stream=sys.stdout,
    )
    # Quiet noisy libraries
    logging.getLogger("dbus_next").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def main() -> None:
    """Start all services and run until signalled to stop."""
    config = AppConfig.load()
    setup_logging(config.log_level)

    logger = logging.getLogger(__name__)
    version = os.environ.get("BUILD_VERSION", "dev")
    logger.info("Bluetooth Audio Manager v%s starting...", version)

    manager = BluetoothAudioManager(config)

    # Stream application logs to the UI via WebSocket
    ws_log_handler = WebSocketLogHandler(manager.event_bus)
    ws_log_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.getLogger().addHandler(ws_log_handler)

    web_server = WebServer(manager, log_handler=ws_log_handler)

    # Handle shutdown signals
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Received shutdown signal")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        # Start web server first so ingress doesn't show 502 during init
        await web_server.start()
        await manager.start()
        logger.info("All services running. Waiting for shutdown signal...")
        await shutdown_event.wait()
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)
    finally:
        await web_server.stop()
        await manager.shutdown()
        logger.info("Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
