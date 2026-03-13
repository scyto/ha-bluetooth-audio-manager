"""Event bus for real-time UI updates via WebSocket."""

import asyncio
import logging

logger = logging.getLogger(__name__)


class EventBus:
    """Simple pub/sub using asyncio.Queue per connected WebSocket client."""

    def __init__(self):
        self._clients: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        """Add a new client. Returns a queue to read events from."""
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._clients.add(q)
        logger.info("EventBus client subscribed (%d total)", len(self._clients))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """Remove a client."""
        self._clients.discard(q)
        logger.info("EventBus client unsubscribed (%d remaining)", len(self._clients))

    def emit(self, event: str, data: dict) -> None:
        """Push an event to all connected clients."""
        if not self._clients:
            return
        logger.debug("EventBus emit: %s → %d client(s)", event, len(self._clients))
        for q in list(self._clients):
            try:
                q.put_nowait({"event": event, "data": data})
            except asyncio.QueueFull:
                logger.warning("Dropping event '%s' for slow client (queue full)", event)

    @property
    def client_count(self) -> int:
        return len(self._clients)
