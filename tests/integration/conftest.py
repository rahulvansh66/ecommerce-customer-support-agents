"""Fixtures for the integration tests (real Neon Postgres).

Role in the system: owns setup and teardown of the database pool and skips the whole
layer when no database is configured, so a plain ``pytest`` run never fails on a
machine without credentials. Tests run against the separate test database
(``NEON_POSTGRES_TEST_CONNECTION_STRING``, built by ``init_neon.py --test``), never the
main one, so they may write freely; rebuild it with ``init_neon.py --test --reset``.
"""

from collections.abc import AsyncIterator

import pytest

from ecommerce_agents.config import get_settings
from ecommerce_agents.db.pool import Database, ensure_selector_event_loop

# psycopg async needs the selector loop on Windows; set before pytest-asyncio creates loops
ensure_selector_event_loop()


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    """Yield an opened pool on the test database and close it after the test.

    Skips the test when the test DSN is not configured, and fails hard if it points at
    the same database as the main DSN, so the real dataset can never be modified.

    Yields:
        An opened ``Database`` backed by the seeded ``neondb_test`` database.
    """
    settings = get_settings()
    test_dsn = settings.secrets.neon_postgres_test_connection_string
    if test_dsn is None:
        pytest.skip("NEON_POSTGRES_TEST_CONNECTION_STRING not set (run init_neon.py --test)")

    # Guard: the test DSN must never equal the main one
    main_dsn = settings.secrets.neon_postgres_connection_string
    if main_dsn is not None and main_dsn.get_secret_value() == test_dsn.get_secret_value():
        pytest.fail("test DSN equals the main DSN; refusing to run against the real database")

    # Swap the test DSN in as the connection string the pool will use
    secrets = settings.secrets.model_copy(update={"neon_postgres_connection_string": test_dsn})
    database = Database(settings.model_copy(update={"secrets": secrets}))
    await database.open()
    yield database
    await database.close()
