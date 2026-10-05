"""Fixtures shared by the unit tests (fast, no network, no DB, LLM mocked).

Role in the system: keeps unit-only fixtures out of the repo-wide test root so the
integration layer never inherits them.
"""

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from ecommerce_agents.config import Secrets, Settings


@pytest.fixture
def settings() -> Settings:
    """Settings with defaults and no secrets (ignores the real .env file).

    Returns:
        A ``Settings`` instance safe to use in unit tests.
    """
    return Settings(secrets=Secrets(_env_file=None))


@pytest.fixture(autouse=True)
def forbid_real_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any unit test that tries to open a real Postgres connection.

    Unit tests must mock the DB layer (``queries`` or ``Database``); this guard turns an
    accidental Neon call into an immediate, explicit failure instead of a slow, billed one.

    Args:
        monkeypatch: Pytest fixture used to patch the connection entry points.
    """

    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unit tests must not touch Postgres; mock the DB layer")

    monkeypatch.setattr(psycopg.Connection, "connect", _blocked)
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _blocked)
    monkeypatch.setattr(AsyncConnectionPool, "open", _blocked)
