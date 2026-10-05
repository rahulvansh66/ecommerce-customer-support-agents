"""Order management tools for the LangGraph agent.

Six read-only tools mirror the reference notebook's use case: order lookup,
customer order history, inventory check, shipping status, return/exchange
status and an order summary. They return short, compact strings so the model
never receives large payloads.

Authorization lives here, not in the prompt: the customer identity is read from
``config["configurable"]["customer_id"]`` (set by the API/CLI from the request),
never from an argument the model can fill in. Tools that need a customer and do
not find one return a "customer id required" message so the agent asks for it.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from ecommerce_agents.config import Settings
from ecommerce_agents.db import queries
from ecommerce_agents.db.pool import Database

logger = logging.getLogger(__name__)

NO_CUSTOMER_MSG = (
    "Customer identity is not available for this session. "
    "Ask the customer to provide their customer id."
)
TOOL_ERROR_MSG = "The order system is temporarily unavailable. Please try again shortly."


class OrderIdArgs(BaseModel):
    """Arguments for ``query_order_by_id``."""

    order_id: str = Field(description="Order identifier, e.g. ORD-1001.")


class NoArgs(BaseModel):
    """Arguments for tools that take no model-supplied input."""


class OptionalOrderArgs(BaseModel):
    """Arguments for shipping/return status tools."""

    order_id: str | None = Field(
        default=None,
        description="Order identifier (e.g. ORD-1001). Omit to check all of the customer's orders.",
    )


class InventoryArgs(BaseModel):
    """Arguments for ``check_product_inventory``."""

    product_name: str | None = Field(
        default=None, description="Full or partial product name, e.g. 'headphones'."
    )
    category: str | None = Field(
        default=None,
        description="Category: headphones, watch, speaker, computer, phone or charger.",
    )


def _customer_id(config: RunnableConfig) -> str | None:
    """Read the trusted customer id from the run config.

    Args:
        config: Runnable config injected by LangGraph into the tool call.

    Returns:
        The customer id, or None if the caller did not set one.
    """
    value = (config.get("configurable") or {}).get("customer_id")
    return str(value) if value else None


def _fmt(value: Any) -> str:
    """Render a DB value for the model (None becomes ``n/a``).

    Args:
        value: A column value (str, date, Decimal, None, ...).

    Returns:
        A short string.
    """
    return "n/a" if value is None else str(value)


async def _guarded(name: str, run: Callable[[], Awaitable[str]]) -> str:
    """Run a tool body with timing, logging and error mapping.

    Errors are logged with a traceback but only a generic message goes back to
    the model, so raw exceptions never reach the customer.

    Args:
        name: Tool name for log fields.
        run: Zero-argument coroutine function producing the tool's text result.

    Returns:
        The tool result, or a safe error message if the query failed.
    """
    started = time.perf_counter()
    try:
        result = await run()
        logger.info(
            "tool=%s status=success duration_s=%.2f", name, time.perf_counter() - started
        )
        return result
    except Exception:
        logger.exception(
            "tool=%s status=error duration_s=%.2f", name, time.perf_counter() - started
        )
        return TOOL_ERROR_MSG


def get_order_tools(db: Database, settings: Settings) -> list[BaseTool]:
    """Build the order management tools bound to a database and settings.

    Tools are created per application (closures over ``db``) so the pool is not
    passed through graph state. Reads nothing from graph state; the customer id
    comes from the run config.

    Args:
        db: Opened database wrapper used for all queries.
        settings: Settings supplying the row cap (``database.max_rows``).

    Returns:
        The six tools, ready for ``bind_tools`` and ``ToolNode``.
    """
    limit = settings.database.max_rows

    @tool("query_order_by_id", args_schema=OrderIdArgs)
    async def query_order_by_id(order_id: str, config: RunnableConfig) -> str:
        """Get full details of one specific order by its order id (e.g. ORD-1001).

        Use when the customer names an order. Not for listing all orders.
        """
        customer_id = _customer_id(config)
        if not customer_id:
            return NO_CUSTOMER_MSG

        async def run() -> str:
            row = await queries.get_order(db, customer_id, order_id)
            if not row:
                return f"No order {order_id} found for this customer."
            return (
                f"Order {row['order_id']}: {row['product_name']} x{row['quantity']}, "
                f"total ${row['total_amount']}. Order status: {row['order_status']}; "
                f"shipping: {_fmt(row['shipping_status'])}; "
                f"return/exchange: {_fmt(row['return_exchange_status'])}; "
                f"ordered {row['order_date']}; delivery date: {_fmt(row['delivery_date'])}."
            )

        return await _guarded("query_order_by_id", run)

    @tool("query_customer_orders", args_schema=NoArgs)
    async def query_customer_orders(config: RunnableConfig) -> str:
        """List the current customer's most recent orders.

        Use when the customer asks about their orders in general or order history.
        """
        customer_id = _customer_id(config)
        if not customer_id:
            return NO_CUSTOMER_MSG

        async def run() -> str:
            rows = await queries.list_customer_orders(db, customer_id, limit)
            if not rows:
                return "No orders found for this customer."
            lines = [
                f"- {r['order_id']}: {r['product_name']} ({r['order_status']}, "
                f"ordered {r['order_date']})"
                for r in rows
            ]
            return f"Most recent {len(rows)} orders:\n" + "\n".join(lines)

        return await _guarded("query_customer_orders", run)

    @tool("check_product_inventory", args_schema=InventoryArgs)
    async def check_product_inventory(
        product_name: str | None = None, category: str | None = None
    ) -> str:
        """Check whether products are in stock, by product name and/or category.

        Use for availability and stock questions. Not customer-specific.
        """

        async def run() -> str:
            rows = await queries.search_inventory(db, product_name, category, limit)
            if not rows:
                return "No matching products found in inventory."
            lines = [
                f"- {r['product_name']} ({r['category']}): "
                f"{'in stock, ' + str(r['quantity']) + ' units' if r['in_stock'] else 'out of stock'}"
                f", ${r['price_per_unit']}"
                for r in rows
            ]
            return "Inventory results:\n" + "\n".join(lines)

        return await _guarded("check_product_inventory", run)

    @tool("check_shipping_status", args_schema=OptionalOrderArgs)
    async def check_shipping_status(config: RunnableConfig, order_id: str | None = None) -> str:
        """Check shipping status and expected delivery date for one order or all orders.

        Use for 'where is my order' or delivery date questions.
        """
        customer_id = _customer_id(config)
        if not customer_id:
            return NO_CUSTOMER_MSG

        async def run() -> str:
            rows = await queries.get_shipping(db, customer_id, order_id, limit)
            if not rows:
                return "No shipping information found."
            lines = [
                f"- {r['order_id']} ({r['product_name']}): order {r['order_status']}, "
                f"shipping {_fmt(r['shipping_status'])}, delivery date {_fmt(r['delivery_date'])}"
                for r in rows
            ]
            return "Shipping status:\n" + "\n".join(lines)

        return await _guarded("check_shipping_status", run)

    @tool("check_return_status", args_schema=OptionalOrderArgs)
    async def check_return_status(config: RunnableConfig, order_id: str | None = None) -> str:
        """Check return/exchange status for one order or all of the customer's orders.

        Use for questions about returns, refunds in progress, or exchanges.
        """
        customer_id = _customer_id(config)
        if not customer_id:
            return NO_CUSTOMER_MSG

        async def run() -> str:
            rows = await queries.get_returns(db, customer_id, order_id, limit)
            if not rows:
                return "No return or exchange found."
            lines = [
                f"- {r['order_id']} ({r['product_name']}): {r['return_exchange_status']}"
                for r in rows
            ]
            return "Return/exchange status:\n" + "\n".join(lines)

        return await _guarded("check_return_status", run)

    @tool("get_order_summary", args_schema=NoArgs)
    async def get_order_summary(config: RunnableConfig) -> str:
        """Summarise the current customer's orders by status (counts per status).

        Use for overview questions such as 'how many orders have shipped'.
        """
        customer_id = _customer_id(config)
        if not customer_id:
            return NO_CUSTOMER_MSG

        async def run() -> str:
            rows = await queries.order_status_summary(db, customer_id)
            if not rows:
                return "No orders found for this customer."
            lines = [f"- {r['order_status']}: {r['total_orders']}" for r in rows]
            return "Order status summary:\n" + "\n".join(lines)

        return await _guarded("get_order_summary", run)

    return [
        query_order_by_id,
        query_customer_orders,
        check_product_inventory,
        check_shipping_status,
        check_return_status,
        get_order_summary,
    ]
