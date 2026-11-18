"""Collect log lines in a test although the test package silences logging."""

import contextlib
import logging


@contextlib.contextmanager
def warnings_from(logger: logging.Logger):
    """Yield the list of WARNING-or-higher messages *logger* emits meanwhile."""
    lines: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    handler = Collect(level=logging.WARNING)
    silenced = logging.root.manager.disable
    propagate = logger.propagate
    logging.disable(logging.NOTSET)
    logger.addHandler(handler)
    logger.propagate = False
    try:
        yield lines
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logging.disable(silenced)
