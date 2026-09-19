"""Read-only, parameterised queries for the order management agent.

Tools call these functions and never build SQL themselves. Every query that
touches orders is scoped by ``customer_id`` so one customer can never read
another customer's data, regardless of what the model asks for. Tables live in
the ``order_management`` schema (see ``scripts/neon-db-creation/init_neon.py``).
"""

import logging
from typing import Any

from ecommerce_agents.db.pool import Database

logger = logging.getLogger(__name__)

Row = dict[str, Any]

_ORDER_COLUMNS = (
    "order_id, customer_id, product_id, product_name, order_status, shipping_status, "
    "return_exchange_status, order_date, delivery_date, quantity, price_per_unit, total_amount"
)


async def _fetch(db: Database, sql: str, params: tuple[Any, ...]) -> list[Row]:
    """Run a query and return all rows.

    Args:
        db: Database wrapper providing pooled connections.
        sql: SQL text with ``%s`` placeholders.
        params: Values bound to the placeholders.

    Returns:
        Rows as dicts keyed by column name.
    """
    async with db.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def get_order(db: Database, customer_id: str, order_id: str) -> Row | None:
    """Fetch one order belonging to a customer.

    Args:
        db: Database wrapper.
        customer_id: Owner of the order (from trusted context, not the model).
        order_id: Order identifier, matched case-insensitively (e.g. ``ORD-1001``).

    Returns:
        The order row, or None if it does not exist for this customer.
    """
    rows = await _fetch(
        db,
        f"SELECT {_ORDER_COLUMNS} FROM order_management.orders "
        "WHERE customer_id = %s AND upper(order_id) = upper(%s)",
        (customer_id, order_id),
    )
    return rows[0] if rows else None


async def list_customer_orders(db: Database, customer_id: str, limit: int) -> list[Row]:
    """List a customer's most recent orders.

    Args:
        db: Database wrapper.
        customer_id: Customer whose orders to list.
        limit: Maximum number of rows to return.

    Returns:
        Orders, newest first.
    """
    return await _fetch(
        db,
        f"SELECT {_ORDER_COLUMNS} FROM order_management.orders "
        "WHERE customer_id = %s ORDER BY order_date DESC, order_id DESC LIMIT %s",
        (customer_id, limit),
    )


async def get_shipping(
    db: Database, customer_id: str, order_id: str | None, limit: int
) -> list[Row]:
    """Fetch shipping status for one order or all of a customer's orders.

    Args:
        db: Database wrapper.
        customer_id: Owner of the orders.
        order_id: Restrict to this order, or None for all of the customer's orders.
        limit: Maximum number of rows to return.

    Returns:
        Rows with order id, product, order/shipping status and delivery date.
    """
    return await _fetch(
        db,
        "SELECT order_id, product_name, order_status, shipping_status, delivery_date "
        "FROM order_management.orders "
        "WHERE customer_id = %s AND (%s::text IS NULL OR upper(order_id) = upper(%s)) "
        "ORDER BY order_date DESC LIMIT %s",
        (customer_id, order_id, order_id, limit),
    )


async def get_returns(
    db: Database, customer_id: str, order_id: str | None, limit: int
) -> list[Row]:
    """Fetch return/exchange status for one order or all of a customer's orders.

    Args:
        db: Database wrapper.
        customer_id: Owner of the orders.
        order_id: Restrict to this order, or None for all of the customer's orders.
        limit: Maximum number of rows to return.

    Returns:
        Rows (only orders that have a return/exchange) with product and status.
    """
    return await _fetch(
        db,
        "SELECT order_id, product_name, order_status, return_exchange_status, order_date "
        "FROM order_management.orders "
        "WHERE customer_id = %s AND return_exchange_status IS NOT NULL "
        "AND (%s::text IS NULL OR upper(order_id) = upper(%s)) "
        "ORDER BY order_date DESC LIMIT %s",
        (customer_id, order_id, order_id, limit),
    )


async def search_inventory(
    db: Database, product_name: str | None, category: str | None, limit: int
) -> list[Row]:
    """Search product inventory by name and/or category.

    The name is split into words and every word must appear somewhere in the
    product name (case-insensitive), so "thundersound speaker" still matches
    "ThunderSound Bluetooth Speaker". Values are always bound as parameters;
    only the number of ``ILIKE`` clauses is dynamic (capped at 5 words).

    Args:
        db: Database wrapper.
        product_name: Words from the product name, or None.
        category: Case-insensitive category name, or None.
        limit: Maximum number of rows to return.

    Returns:
        Rows with product, category, quantity, stock flag and unit price.
    """
    clauses: list[str] = []
    params: list[Any] = []
    for word in (product_name or "").split()[:5]:
        clauses.append("product_name ILIKE %s")
        params.append(f"%{word}%")
    if category:
        clauses.append("category ILIKE %s")
        params.append(category)
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    return await _fetch(
        db,
        "SELECT product_id, product_name, category, quantity, in_stock, price_per_unit "
        f"FROM order_management.inventory {where}"
        "ORDER BY in_stock DESC, product_name LIMIT %s",
        (*params, limit),
    )


async def order_status_summary(db: Database, customer_id: str) -> list[Row]:
    """Count a customer's orders grouped by order status.

    Args:
        db: Database wrapper.
        customer_id: Customer to summarise.

    Returns:
        Rows of ``order_status`` and ``total_orders``, largest group first.
    """
    return await _fetch(
        db,
        "SELECT order_status, COUNT(*) AS total_orders FROM order_management.orders "
        "WHERE customer_id = %s GROUP BY order_status ORDER BY total_orders DESC",
        (customer_id,),
    )
