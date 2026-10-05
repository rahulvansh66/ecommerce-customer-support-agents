"""Async connection pool for Neon Postgres.

One pool is opened at application startup (FastAPI lifespan or CLI start) and
closed on shutdown; query code borrows connections from it via
:meth:`Database.connection`. Rows come back as dicts (``dict_row``).

Note: psycopg's async mode needs a selector event loop. On Windows the CLI sets
``WindowsSelectorEventLoopPolicy`` and ``main.py`` gives uvicorn
:func:`selector_loop_factory`.
"""

import asyncio
import logging
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from ecommerce_agents.config import Settings

logger = logging.getLogger(__name__)


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """Create a selector event loop (compatible with psycopg's async mode).

    Usable as a uvicorn custom loop factory:
    ``--loop ecommerce_agents.db.pool:selector_loop_factory``. Needed because
    uvicorn forces the Proactor loop on Windows regardless of the event loop policy.

    Returns:
        A new ``asyncio.SelectorEventLoop``.
    """
    return asyncio.SelectorEventLoop()


def ensure_selector_event_loop() -> None:
    """Use the selector event loop on Windows, which psycopg's async mode requires.

    Call from an entry point before the event loop is created. No-op elsewhere.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class Database:
    """Owns the async connection pool used by all query functions."""

    def __init__(self, settings: Settings) -> None:
        """Create the (not yet opened) pool.

        Args:
            settings: Resolved settings; supplies the DSN and pool sizing.

        Raises:
            ConfigError: If the Neon connection string is not configured.
        """
        self._settings = settings
        cfg = settings.database
        # Session-level statement timeout keeps a slow query from hanging a request.
        options = f"-c statement_timeout={cfg.statement_timeout_ms}"
        self._pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
            conninfo=settings.require_database_url(),
            min_size=cfg.pool_min_size,
            max_size=cfg.pool_max_size,
            timeout=cfg.pool_timeout_s,
            kwargs={"row_factory": dict_row, "options": options},
            open=False,
        )

    async def open(self) -> None:
        """Open the pool and wait until the minimum connections are ready.

        Raises:
            psycopg_pool.PoolTimeout: If Neon cannot be reached within the pool timeout.
        """
        started = time.perf_counter()
        await self._pool.open(wait=True, timeout=self._settings.database.pool_timeout_s)
        logger.info(
            "component=db status=opened min=%d max=%d duration_s=%.2f",
            self._settings.database.pool_min_size,
            self._settings.database.pool_max_size,
            time.perf_counter() - started,
        )

    async def close(self) -> None:
        """Close the pool and its connections."""
        await self._pool.close()
        logger.info("component=db status=closed")

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection[DictRow]]:
        """Borrow a connection from the pool.

        Yields:
            An async psycopg connection returning dict rows.
        """
        async with self._pool.connection() as conn:
            yield conn

    async def ping(self) -> bool:
        """Check that the database answers a trivial query.

        Returns:
            True if ``SELECT 1`` succeeds, False otherwise (failure is logged).
        """
        try:
            async with self.connection() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            logger.exception("component=db status=ping_failed")
            return False
