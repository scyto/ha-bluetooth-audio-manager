"""Logging handler that streams log records to the UI via EventBus."""

import collections
import logging

from .events import EventBus


class WebSocketLogHandler(logging.Handler):
    """Captures log records and pushes them to connected WebSocket clients.

    Maintains a ring buffer of recent entries so new clients can replay history.
    """

    MAX_RECENT_LOGS = 500

    def __init__(self, event_bus: EventBus) -> None:
        super().__init__()
        self._event_bus = event_bus
        self.recent_logs: collections.deque[dict] = collections.deque(
            maxlen=self.MAX_RECENT_LOGS,
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
            }
            self.recent_logs.append(entry)
            self._event_bus.emit("log_entry", entry)
        except Exception:
            self.handleError(record)
