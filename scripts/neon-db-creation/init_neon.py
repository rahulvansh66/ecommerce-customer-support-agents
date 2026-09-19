"""Create the three agent schemas in Neon Postgres and seed a small, coherent demo dataset.

Replaces the per-agent SQLite files:
  order_management.db         -> schema order_management
  product_recommendation.db   -> schema product_recommendation
  personalization.db          -> schema personalization

Role in the system: one-off setup script (not part of the running agents). It owns the
DDL and the demo data that every agent's tools query.

Design:
  * Shared IDs across schemas: customers cust001..005, products prod001..008.
  * Foreign keys only inside a schema (each agent stays independent); cross-schema
    consistency is verified by check_consistency() instead of enforced by the database.
  * purchase_history is derived from delivered orders so the two cannot drift apart.

Idempotent: safe to re-run; a table is seeded only when empty.
  python scripts/neon-db-creation/init_neon.py            create + seed what is missing
  python scripts/neon-db-creation/init_neon.py --reset    drop the three demo schemas first
  python scripts/neon-db-creation/init_neon.py --test     same, but on the separate test database

The test database (``neondb_test``, same Neon endpoint as the main one) lets the
integration tests write freely without touching the real dataset. ``--test`` creates it
on first use and reads its DSN from NEON_POSTGRES_TEST_CONNECTION_STRING; if that
variable is missing it is derived from the main DSN and appended to ``.env``.

Reads NEON_POSTGRES_CONNECTION_STRING from the environment or the repo-root .env file.
"""

import argparse
import logging
import os
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import psycopg.sql

logger = logging.getLogger(__name__)

ENV_VAR = "NEON_POSTGRES_CONNECTION_STRING"
TEST_ENV_VAR = "NEON_POSTGRES_TEST_CONNECTION_STRING"
TEST_DB_NAME = "neondb_test"
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
SCHEMAS = ("order_management", "product_recommendation", "personalization")

# A seed row is a tuple of column values in INSERT order.
Row = tuple[Any, ...]


def load_connection_string(env_var: str = ENV_VAR, required: bool = True) -> str | None:
    """Resolve a connection string from the environment or the repo-root ``.env``.

    The process environment wins; the ``.env`` file is only a fallback so the script
    also works when run outside a configured shell.

    Args:
        env_var: Name of the variable to read (main or test DSN).
        required: If True, a missing value aborts the script; if False, return None.

    Returns:
        str | None: The variable's value, or None when missing and not required.

    Raises:
        SystemExit: If ``required`` and the variable is set in neither place.
    """
    value = os.environ.get(env_var)
    if not value and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            key, _, val = line.partition("=")
            if key.strip() == env_var:
                value = val.strip().strip("\"'")
    if not value and required:
        raise SystemExit(f"{env_var} is not set (environment or .env)")
    return value or None


def with_database(dsn: str, database: str) -> str:
    """Return ``dsn`` pointing at a different database name on the same server.

    Args:
        dsn: A ``postgresql://user:pass@host/dbname?params`` connection string.
        database: The database name to substitute.

    Returns:
        str: The DSN with only the database path segment replaced.
    """
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path=f"/{database}"))


def ensure_test_database() -> str:
    """Create the test database if missing and return its DSN (persisted to ``.env``).

    Connects to the main database in autocommit mode (``CREATE DATABASE`` cannot run
    inside a transaction). The main database's data is never modified.

    Returns:
        str: The test database connection string.

    Raises:
        SystemExit: If the main DSN is missing, or the configured test DSN points at
            the same database as the main one (would defeat the isolation).
    """
    main_dsn = load_connection_string()
    test_dsn = load_connection_string(TEST_ENV_VAR, required=False) or with_database(
        main_dsn, TEST_DB_NAME
    )
    if urlsplit(test_dsn).path == urlsplit(main_dsn).path and urlsplit(
        test_dsn
    ).hostname == urlsplit(main_dsn).hostname:
        raise SystemExit(f"{TEST_ENV_VAR} must point at a different database than {ENV_VAR}")

    # Create the database only when absent, so re-runs are harmless
    name = urlsplit(test_dsn).path.lstrip("/")
    with psycopg.connect(main_dsn, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if not exists:
            conn.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(name)))
            logger.info("step=create_test_db status=created database=%s", name)

    # Persist the DSN so tests can find it (never printed: it holds credentials)
    if load_connection_string(TEST_ENV_VAR, required=False) is None:
        with ENV_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"\n# Separate database for tests (created by init_neon.py --test)\n{TEST_ENV_VAR}={test_dsn}\n")
        logger.info("step=persist_test_dsn status=complete env_var=%s", TEST_ENV_VAR)
    return test_dsn


SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS order_management;
CREATE SCHEMA IF NOT EXISTS product_recommendation;
CREATE SCHEMA IF NOT EXISTS personalization;

CREATE TABLE IF NOT EXISTS order_management.customers (
    customer_id VARCHAR(50) PRIMARY KEY,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    phone VARCHAR(20),
    address TEXT,
    city VARCHAR(100),
    state VARCHAR(50),
    zip_code VARCHAR(20),
    created_date DATE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_management.inventory (
    product_id VARCHAR(50) PRIMARY KEY,
    product_name VARCHAR(255) NOT NULL,
    category VARCHAR(100) NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    in_stock BOOLEAN NOT NULL,
    reorder_threshold INTEGER DEFAULT 10,
    reorder_quantity INTEGER DEFAULT 50,
    last_restock_date DATE,
    price_per_unit DECIMAL(10,2) CHECK (price_per_unit >= 0),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT inventory_in_stock_matches_quantity CHECK (in_stock = (quantity > 0))
);

CREATE TABLE IF NOT EXISTS order_management.orders (
    order_id VARCHAR(50) PRIMARY KEY,
    customer_id VARCHAR(50) NOT NULL REFERENCES order_management.customers(customer_id),
    product_id VARCHAR(50) NOT NULL REFERENCES order_management.inventory(product_id),
    product_name VARCHAR(255) NOT NULL,
    order_status VARCHAR(50) NOT NULL
        CHECK (order_status IN ('processing', 'shipped', 'delivered', 'cancelled')),
    shipping_status VARCHAR(50)
        CHECK (shipping_status IN ('preparing', 'in_transit', 'delivered', 'cancelled')),
    return_exchange_status VARCHAR(50)
        CHECK (return_exchange_status IN ('return_requested', 'exchange_completed')),
    order_date DATE NOT NULL,
    delivery_date DATE,
    quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
    price_per_unit DECIMAL(10,2) NOT NULL CHECK (price_per_unit >= 0),
    total_amount DECIMAL(10,2) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT orders_total_matches_quantity CHECK (total_amount = quantity * price_per_unit),
    CONSTRAINT orders_cancelled_has_no_delivery CHECK (order_status <> 'cancelled' OR delivery_date IS NULL),
    CONSTRAINT orders_return_only_when_delivered CHECK (return_exchange_status IS NULL OR order_status = 'delivered')
);

CREATE INDEX IF NOT EXISTS idx_orders_customer_id ON order_management.orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON order_management.orders(order_status);
CREATE INDEX IF NOT EXISTS idx_orders_date ON order_management.orders(order_date);
CREATE INDEX IF NOT EXISTS idx_inventory_category ON order_management.inventory(category);
CREATE INDEX IF NOT EXISTS idx_inventory_in_stock ON order_management.inventory(in_stock);

CREATE TABLE IF NOT EXISTS product_recommendation.product_catalog (
    product_id TEXT PRIMARY KEY,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL,
    price DOUBLE PRECISION NOT NULL CHECK (price >= 0),
    description TEXT,
    rating DOUBLE PRECISION DEFAULT 0.0,
    popularity TEXT DEFAULT 'medium'
);

CREATE TABLE IF NOT EXISTS product_recommendation.purchase_history (
    id SERIAL PRIMARY KEY,
    customer_id TEXT NOT NULL,
    product_id TEXT NOT NULL REFERENCES product_recommendation.product_catalog(product_id),
    purchase_date DATE NOT NULL,
    quantity INTEGER DEFAULT 1 CHECK (quantity > 0),
    purchase_amount DOUBLE PRECISION NOT NULL,
    payment_method TEXT DEFAULT 'credit_card'
);

CREATE TABLE IF NOT EXISTS personalization.personalization (
    customer_id TEXT PRIMARY KEY,
    age INTEGER,
    gender TEXT,
    income TEXT,
    location TEXT,
    marital_status TEXT,
    preferred_category TEXT,
    price_range TEXT,
    preferred_brand TEXT,
    loyalty_tier TEXT
);
"""

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

CUSTOMERS: list[Row] = [
    ("cust001", "John", "Smith", "john.smith@email.com", "555-0123", "123 Main St", "New York", "NY", "10001", "2024-01-15"),
    ("cust002", "Sarah", "Johnson", "sarah.j@email.com", "555-0456", "456 Oak Ave", "Los Angeles", "CA", "90210", "2024-02-20"),
    ("cust003", "Mike", "Chen", "mike.chen@email.com", "555-0789", "789 Pine Rd", "Chicago", "IL", "60601", "2024-03-10"),
    ("cust004", "Emma", "Davis", "emma.d@email.com", "555-0321", "321 Elm St", "Miami", "FL", "33101", "2024-04-05"),
    ("cust005", "Alex", "Wilson", "alex.w@email.com", "555-0654", "654 Maple Dr", "Seattle", "WA", "98101", "2024-05-12"),
]

# (product_id, name, category, price, description, rating, popularity)
PRODUCTS: list[Row] = [
    ("prod001", "ZenSound Wireless Headphones", "headphones", Decimal("199.99"), "Premium wireless headphones with noise cancellation", 4.5, "high"),
    ("prod002", "SportBeat Wireless Earbuds", "headphones", Decimal("129.99"), "Waterproof wireless earbuds for sports", 4.3, "high"),
    ("prod003", "VitaFit Smartwatch", "watch", Decimal("249.99"), "Advanced fitness tracking smartwatch", 4.4, "high"),
    ("prod004", "ThunderSound Bluetooth Speaker", "speaker", Decimal("89.99"), "Portable bluetooth speaker with rich bass", 4.3, "high"),
    ("prod005", "ProMax Laptop", "computer", Decimal("1299.99"), "High-performance laptop for professionals", 4.5, "medium"),
    ("prod006", "StudentBook Budget Laptop", "computer", Decimal("499.99"), "Affordable laptop for students", 4.0, "high"),
    ("prod007", "SmartConnect Phone", "phone", Decimal("799.99"), "Latest smartphone with advanced camera", 4.4, "high"),
    ("prod008", "QuickCharge Wireless Charger", "charger", Decimal("39.99"), "Fast wireless charging pad", 4.2, "medium"),
]

# product_id -> (quantity, reorder_threshold, reorder_quantity, days since last restock)
# prod003 is in stock but below its reorder threshold; prod004 is out of stock.
STOCK: dict[str, tuple[int, int, int, int]] = {
    "prod001": (25, 10, 50, 20),
    "prod002": (18, 8, 40, 14),
    "prod003": (4, 5, 30, 35),
    "prod004": (0, 5, 25, 50),
    "prod005": (6, 3, 10, 30),
    "prod006": (14, 5, 20, 12),
    "prod007": (12, 5, 25, 18),
    "prod008": (35, 15, 60, 8),
}

# (order_id, customer, product, order_status, shipping_status, return/exchange,
#  order days ago, delivery days from today (negative = past, None = no delivery), quantity, payment)
ORDERS: list[Row] = [
    ("ORD-1001", "cust001", "prod001", "processing", "preparing", None, 2, 4, 1, "credit_card"),
    ("ORD-1002", "cust001", "prod003", "shipped", "in_transit", None, 6, 2, 1, "credit_card"),
    ("ORD-1003", "cust001", "prod008", "delivered", "delivered", None, 25, -22, 2, "debit_card"),
    ("ORD-1004", "cust002", "prod002", "delivered", "delivered", "return_requested", 20, -16, 1, "credit_card"),
    ("ORD-1005", "cust002", "prod004", "shipped", "in_transit", None, 5, 3, 1, "credit_card"),
    ("ORD-1006", "cust003", "prod007", "delivered", "delivered", "exchange_completed", 28, -24, 1, "credit_card"),
    ("ORD-1007", "cust004", "prod005", "cancelled", "cancelled", None, 12, None, 1, "credit_card"),
    ("ORD-1008", "cust005", "prod006", "delivered", "delivered", None, 15, -11, 1, "debit_card"),
]

# customer_id, age, gender, income, location, marital_status, preferred_category, price_range, brand, tier
PERSONALIZATION: list[Row] = [
    ("cust001", 28, "male", "70000-90000", "new york", "single", "headphones", "high", "zensound", "gold"),
    ("cust002", 32, "female", "50000-70000", "los angeles", "married", "watch", "medium", "vitafit", "silver"),
    ("cust003", 24, "male", "30000-50000", "chicago", "single", "phone", "medium", "smartconnect", "bronze"),
    ("cust004", 45, "female", "100000+", "miami", "married", "computer", "high", "promax", "platinum"),
    ("cust005", 20, "female", "20000-30000", "seattle", "single", "computer", "low", "studentbook", "bronze"),
]


def build_customers(today: date) -> list[Row]:
    """Return the customer seed rows.

    Args:
        today (date): Unused; present so every seed builder shares one signature.

    Returns:
        list[Row]: Rows for ``order_management.customers``.
    """
    return CUSTOMERS


def build_products(today: date) -> list[Row]:
    """Return the product catalog seed rows (prices as floats to match DOUBLE PRECISION).

    Args:
        today (date): Unused; present so every seed builder shares one signature.

    Returns:
        list[Row]: Rows for ``product_recommendation.product_catalog``.
    """
    return [(pid, name, cat, float(price), desc, rating, pop)
            for pid, name, cat, price, desc, rating, pop in PRODUCTS]


def build_inventory(today: date) -> list[Row]:
    """Build one inventory row per product, reusing catalog name/category/price.

    ``in_stock`` is derived from quantity so it can never contradict the table's CHECK.

    Args:
        today (date): Reference date; restock dates are offsets back from it.

    Returns:
        list[Row]: Rows for ``order_management.inventory``.
    """
    catalog = {p[0]: (p[1], p[2], p[3]) for p in PRODUCTS}
    rows: list[Row] = []
    for product_id, (qty, threshold, reorder_qty, restock_days) in STOCK.items():
        name, category, price = catalog[product_id]
        rows.append((product_id, name, category, qty, qty > 0, threshold, reorder_qty,
                     today - timedelta(days=restock_days), price))
    return rows


def build_orders(today: date) -> list[Row]:
    """Build order rows with catalog prices and totals computed from quantity.

    Args:
        today (date): Reference date; order and delivery dates are offsets from it.

    Returns:
        list[Row]: Rows for ``order_management.orders``.
    """
    catalog = {p[0]: (p[1], p[3]) for p in PRODUCTS}
    rows: list[Row] = []
    for (order_id, cust, prod, status, shipping, ret, ordered_ago, delivery_offset, qty, _pay) in ORDERS:
        name, price = catalog[prod]
        delivery = today + timedelta(days=delivery_offset) if delivery_offset is not None else None
        rows.append((order_id, cust, prod, name, status, shipping, ret,
                     today - timedelta(days=ordered_ago), delivery, qty, price, qty * price))
    return rows


def build_purchase_history(today: date) -> list[Row]:
    """Derive purchase history from delivered orders (same customer, product, date, qty, amount).

    Args:
        today (date): Reference date; must match the one used by ``build_orders``.

    Returns:
        list[Row]: Rows for ``product_recommendation.purchase_history``.
    """
    prices = {p[0]: p[3] for p in PRODUCTS}
    return [
        (cust, prod, today - timedelta(days=ordered_ago), qty, float(qty * prices[prod]), pay)
        for (_id, cust, prod, status, _s, _r, ordered_ago, _d, qty, pay) in ORDERS
        if status == "delivered"
    ]


def build_personalization(today: date) -> list[Row]:
    """Return the customer personalization seed rows.

    Args:
        today (date): Unused; present so every seed builder shares one signature.

    Returns:
        list[Row]: Rows for ``personalization.personalization``.
    """
    return PERSONALIZATION


# (table, INSERT statement, row builder). Order matters: parents before children.
SEEDS: list[tuple[str, str, Callable[[date], list[Row]]]] = [
    ("order_management.customers",
     "INSERT INTO order_management.customers (customer_id, first_name, last_name, email, phone, address, city, state, zip_code, created_date) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
     build_customers),
    ("product_recommendation.product_catalog",
     "INSERT INTO product_recommendation.product_catalog (product_id, product_name, category, price, description, rating, popularity) VALUES (%s,%s,%s,%s,%s,%s,%s)",
     build_products),
    ("order_management.inventory",
     "INSERT INTO order_management.inventory (product_id, product_name, category, quantity, in_stock, reorder_threshold, reorder_quantity, last_restock_date, price_per_unit) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
     build_inventory),
    ("order_management.orders",
     "INSERT INTO order_management.orders (order_id, customer_id, product_id, product_name, order_status, shipping_status, return_exchange_status, order_date, delivery_date, quantity, price_per_unit, total_amount) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
     build_orders),
    ("product_recommendation.purchase_history",
     "INSERT INTO product_recommendation.purchase_history (customer_id, product_id, purchase_date, quantity, purchase_amount, payment_method) VALUES (%s,%s,%s,%s,%s,%s)",
     build_purchase_history),
    ("personalization.personalization",
     "INSERT INTO personalization.personalization (customer_id, age, gender, income, location, marital_status, preferred_category, price_range, preferred_brand, loyalty_tier) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
     build_personalization),
]

# Each query returns offending rows; every one must return nothing.
CONSISTENCY_CHECKS: dict[str, str] = {
    "inventory product not in catalog": """
        SELECT i.product_id FROM order_management.inventory i
        LEFT JOIN product_recommendation.product_catalog c ON c.product_id = i.product_id
        WHERE c.product_id IS NULL""",
    "inventory name/category/price differs from catalog": """
        SELECT i.product_id FROM order_management.inventory i
        JOIN product_recommendation.product_catalog c ON c.product_id = i.product_id
        WHERE i.product_name <> c.product_name OR i.category <> c.category
           OR i.price_per_unit::float8 <> c.price""",
    "order price/name differs from catalog": """
        SELECT o.order_id FROM order_management.orders o
        JOIN product_recommendation.product_catalog c ON c.product_id = o.product_id
        WHERE o.product_name <> c.product_name OR o.price_per_unit::float8 <> c.price""",
    "purchase_history customer not in customers": """
        SELECT h.id FROM product_recommendation.purchase_history h
        LEFT JOIN order_management.customers c ON c.customer_id = h.customer_id
        WHERE c.customer_id IS NULL""",
    "personalization customer not in customers": """
        SELECT p.customer_id FROM personalization.personalization p
        LEFT JOIN order_management.customers c ON c.customer_id = p.customer_id
        WHERE c.customer_id IS NULL""",
    "purchase_history row without a matching delivered order": """
        SELECT h.id FROM product_recommendation.purchase_history h
        LEFT JOIN order_management.orders o
          ON o.customer_id = h.customer_id AND o.product_id = h.product_id
         AND o.order_date = h.purchase_date AND o.quantity = h.quantity
         AND o.total_amount::float8 = h.purchase_amount AND o.order_status = 'delivered'
        WHERE o.order_id IS NULL""",
    "delivered order without a purchase_history row": """
        SELECT o.order_id FROM order_management.orders o
        LEFT JOIN product_recommendation.purchase_history h
          ON o.customer_id = h.customer_id AND o.product_id = h.product_id
         AND o.order_date = h.purchase_date
        WHERE o.order_status = 'delivered' AND h.id IS NULL""",
}


def check_consistency(conn: psycopg.Connection) -> None:
    """Verify the cross-schema rules the database cannot enforce (no cross-schema FKs).

    Runs every query in ``CONSISTENCY_CHECKS``; each must return zero rows.

    Args:
        conn (psycopg.Connection): Open connection (same transaction as the seed).

    Raises:
        SystemExit: If any check returns offending rows; the message lists them.
    """
    failures: list[str] = []
    for name, sql in CONSISTENCY_CHECKS.items():
        bad = conn.execute(sql).fetchall()
        if bad:
            failures.append(f"{name}: {[r[0] for r in bad]}")
    if failures:
        raise SystemExit("Consistency check failed:\n  " + "\n  ".join(failures))
    logger.info("step=consistency_check status=passed checks=%d", len(CONSISTENCY_CHECKS))


def main() -> None:
    """Create schemas/tables, seed empty tables, run consistency checks, and commit.

    Entry point: configures logging, parses ``--reset`` and ``--test``, and runs everything in one
    transaction so a failed check leaves the database untouched.

    Raises:
        SystemExit: If the connection string is missing or a consistency check fails.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="drop the three demo schemas before creating them")
    parser.add_argument("--test", action="store_true", help=f"target the separate {TEST_DB_NAME} database instead of the main one")
    args = parser.parse_args()

    today = date.today()
    dsn = ensure_test_database() if args.test else load_connection_string()
    with psycopg.connect(dsn) as conn:
        # Optional wipe of the demo schemas (destructive, demo data only)
        if args.reset:
            for schema in SCHEMAS:
                conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            logger.info("step=reset status=complete schemas=%s", ",".join(SCHEMAS))
        conn.execute(SCHEMA_SQL)

        # Seed only empty tables so re-runs never duplicate rows
        for table, insert_sql, build_rows in SEEDS:
            (count,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            if count:
                logger.info("table=%s status=skipped rows=%d", table, count)
                continue
            data = build_rows(today)
            with conn.cursor() as cur:
                cur.executemany(insert_sql, data)
            logger.info("table=%s status=seeded rows=%d", table, len(data))

        check_consistency(conn)
        conn.commit()

        # Final row counts as a quick sanity summary
        total = 0
        for table, _, _ in SEEDS:
            (n,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            total += n
            logger.info("table=%s rows=%d", table, n)
        logger.info("total_rows=%d", total)


if __name__ == "__main__":
    main()
