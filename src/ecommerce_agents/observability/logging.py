"""Logging setup with per-request correlation identifiers.

Only entry points (``main.py``, ``cli.py``) call :func:`configure_logging`;
library modules just use ``logging.getLogger(__name__)``. The ``session_id`` and
``run_id`` bound with :func:`bind_log_context` are stamped onto every log line
emitted while handling a request, so one conversation can be traced across
graph nodes and tool calls (and, later, in CloudWatch).
"""

import contextvars
import logging
from collections.abc import Iterator
from contextlib import contextmanager

_session_id: contextvars.ContextVar[str] = contextvars.ContextVar("session_id", default="-")
_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")

LOG_FORMAT = (
    "%(asctime)s level=%(levelname)s logger=%(name)s "
    "session_id=%(session_id)s run_id=%(run_id)s %(message)s"
)


class CorrelationFilter(logging.Filter):
    """Adds ``session_id`` and ``run_id`` from context variables to each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Attach correlation ids to a record.

        Args:
            record: The log record being emitted.

        Returns:
            Always True; the filter only enriches records, never drops them.
        """
        record.session_id = _session_id.get()
        record.run_id = _run_id.get()
        return True


@contextmanager
def bind_log_context(session_id: str, run_id: str) -> Iterator[None]:
    """Bind correlation ids to all logs emitted inside the ``with`` block.

    Args:
        session_id: Conversation/thread identifier.
        run_id: Identifier of this single graph invocation.

    Yields:
        None; ids are reset on exit.
    """
    session_token = _session_id.set(session_id)
    run_token = _run_id.set(run_id)
    try:
        yield
    finally:
        _session_id.reset(session_token)
        _run_id.reset(run_token)


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger. Call once from an application entry point.

    Args:
        level: Log level name (``DEBUG``, ``INFO``, ...).
    """
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(CorrelationFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Third-party clients log full request URLs/headers at INFO; keep them quiet.
    for noisy in ("httpx", "httpcore", "groq"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
