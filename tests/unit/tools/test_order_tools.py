"""Unit tests for the order tools (queries are faked; no database)."""

from typing import Any

import pytest
from langchain_core.tools import BaseTool

from ecommerce_agents.config import Settings
from ecommerce_agents.db import queries
from ecommerce_agents.tools.order_tools import NO_CUSTOMER_MSG, TOOL_ERROR_MSG, get_order_tools

ORDER = {
    "order_id": "ORD-1001",
    "product_name": "Headphones",
    "quantity": 1,
    "total_amount": "199.99",
    "order_status": "processing",
    "shipping_status": "preparing",
    "return_exchange_status": None,
    "order_date": "2026-09-17",
    "delivery_date": "2026-09-23",
}


@pytest.fixture
def tools(settings: Settings) -> dict[str, BaseTool]:
    """Tools keyed by name, bound to a dummy DB object."""
    return {t.name: t for t in get_order_tools(object(), settings)}  # type: ignore[arg-type]


def cfg(customer_id: str | None) -> dict[str, Any]:
    """Build a run config carrying the trusted customer id."""
    return {"configurable": {"customer_id": customer_id}}


def test_six_tools_registered(tools: dict[str, BaseTool]) -> None:
    """The reference's six tools exist, and customer_id is hidden from the model schema."""
    assert set(tools) == {
        "query_order_by_id",
        "query_customer_orders",
        "check_product_inventory",
        "check_shipping_status",
        "check_return_status",
        "get_order_summary",
    }
    for t in tools.values():
        assert "customer_id" not in t.args


async def test_order_lookup_is_scoped_to_config_customer(
    tools: dict[str, BaseTool], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The customer id passed to the query comes from config, never from tool args."""
    seen: dict[str, str] = {}

    async def fake_get_order(db: Any, customer_id: str, order_id: str) -> dict[str, Any]:
        seen["customer_id"] = customer_id
        return ORDER

    monkeypatch.setattr(queries, "get_order", fake_get_order)
    out = await tools["query_order_by_id"].ainvoke({"order_id": "ORD-1001"}, config=cfg("cust001"))
    assert seen["customer_id"] == "cust001"
    assert "ORD-1001" in out and "processing" in out


async def test_missing_customer_asks_for_id(tools: dict[str, BaseTool]) -> None:
    """Customer-specific tools refuse to run without a customer id."""
    out = await tools["query_customer_orders"].ainvoke({}, config=cfg(None))
    assert out == NO_CUSTOMER_MSG


async def test_not_found_message(
    tools: dict[str, BaseTool], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another customer's order looks identical to a missing one."""

    async def none(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(queries, "get_order", none)
    out = await tools["query_order_by_id"].ainvoke({"order_id": "ORD-1004"}, config=cfg("cust001"))
    assert out.startswith("No order ORD-1004")


async def test_db_error_returns_safe_message(
    tools: dict[str, BaseTool], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Query failures are mapped to a generic message with no exception text."""

    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("secret dsn leaked")

    monkeypatch.setattr(queries, "order_status_summary", boom)
    out = await tools["get_order_summary"].ainvoke({}, config=cfg("cust001"))
    assert out == TOOL_ERROR_MSG


async def test_inventory_formats_stock(
    tools: dict[str, BaseTool], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inventory output distinguishes in-stock from out-of-stock."""

    async def rows(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return [
            {
                "product_name": "A",
                "category": "speaker",
                "quantity": 0,
                "in_stock": False,
                "price_per_unit": "89.99",
            },
            {
                "product_name": "B",
                "category": "speaker",
                "quantity": 4,
                "in_stock": True,
                "price_per_unit": "10.00",
            },
        ]

    monkeypatch.setattr(queries, "search_inventory", rows)
    out = await tools["check_product_inventory"].ainvoke({"category": "speaker"}, config=cfg(None))
    assert "out of stock" in out and "4 units" in out
