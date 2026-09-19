"""Integration tests: real queries against the seeded Neon database.

Skipped (via the ``db`` fixture) when ``NEON_POSTGRES_TEST_CONNECTION_STRING`` is not
configured. Runs against the separate test database (``neondb_test``), never the main one;
requires the demo data from
``scripts/neon-db-creation/init_neon.py --test``.
"""

import pytest

from ecommerce_agents.db import queries
from ecommerce_agents.db.pool import Database

pytestmark = pytest.mark.integration


async def test_customer_orders(db: Database) -> None:
    """cust001 has three seeded orders."""
    rows = await queries.list_customer_orders(db, "cust001", 10)
    assert {r["order_id"] for r in rows} == {"ORD-1001", "ORD-1002", "ORD-1003"}


async def test_order_belongs_to_customer(db: Database) -> None:
    """ORD-1004 is cust002's; cust001 cannot read it."""
    assert await queries.get_order(db, "cust002", "ORD-1004") is not None
    assert await queries.get_order(db, "cust001", "ORD-1004") is None


async def test_inventory_out_of_stock(db: Database) -> None:
    """prod004 (speaker) is seeded as out of stock."""
    rows = await queries.search_inventory(db, "thundersound", None, 5)
    assert rows and rows[0]["in_stock"] is False


async def test_inventory_matches_words_separately(db: Database) -> None:
    """A partial, reordered name still finds the product."""
    rows = await queries.search_inventory(db, "thundersound speaker", None, 5)
    assert [r["product_id"] for r in rows] == ["prod004"]
