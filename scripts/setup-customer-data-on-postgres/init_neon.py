"""
Create the three agent schemas and tables in Neon Postgres and seed a small, coherent demo dataset.

Replaces the per-agent SQLite files:
  order_management.db         -> schema order_management
  product_recommendation.db   -> schema product_recommendation
  personalization.db          -> schema personalization

Design:
  * Shared IDs across schemas: customers cust001..005, products prod001..008.
  * Foreign keys only inside a schema (each agent stays independent); cross-schema
    consistency is verified by check_consistency() instead of enforced by the database.
  * purchase_history is derived from delivered orders so the two cannot drift apart.

Idempotent: safe to re-run; a table is seeded only when empty.
  python db/init_neon.py            create + seed what is missing
  python db/init_neon.py --reset    drop the three demo schemas first (demo data only)

Reads NEON_POSTGRES_CONNECTION_STRING from the environment or a .env file.
"""

import argparse
import os
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg

ENV_VAR = "NEON_POSTGRES_CONNECTION_STRING"
SCHEMAS = ("order_management", "product_recommendation", "personalization")


def load_connection_string() -> str:
    value = os.environ.get(ENV_VAR)
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not value and env_file.exists():
        for line in env_file.read_text().splitlines():
            key, _, val = line.partition("=")
            if key.strip() == ENV_VAR:
                value = val.strip().strip("\"'")
    if not value:
        raise SystemExit(f"{ENV_VAR} is not set (environment or .env)")
    return value


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

CUSTOMERS = [
    ("cust001", "John", "Smith", "john.smith@email.com", "555-0123", "123 Main St", "New York", "NY", "10001", "2024-01-15"),
    ("cust002", "Sarah", "Johnson", "sarah.j@email.com", "555-0456", "456 Oak Ave", "Los Angeles", "CA", "90210", "2024-02-20"),
    ("cust003", "Mike", "Chen", "mike.chen@email.com", "555-0789", "789 Pine Rd", "Chicago", "IL", "60601", "2024-03-10"),
    ("cust004", "Emma", "Davis", "emma.d@email.com", "555-0321", "321 Elm St", "Miami", "FL", "33101", "2024-04-05"),
    ("cust005", "Alex", "Wilson", "alex.w@email.com", "555-0654", "654 Maple Dr", "Seattle", "WA", "98101", "2024-05-12"),
]

# (product_id, name, category, price, description, rating, popularity)
PRODUCTS = [
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
STOCK = {
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
ORDERS = [
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
PERSONALIZATION = [
    ("cust001", 28, "male", "70000-90000", "new york", "single", "headphones", "high", "zensound", "gold"),
    ("cust002", 32, "female", "50000-70000", "los angeles", "married", "watch", "medium", "vitafit", "silver"),
    ("cust003", 24, "male", "30000-50000", "chicago", "single", "phone", "medium", "smartconnect", "bronze"),
    ("cust004", 45, "female", "100000+", "miami", "married", "computer", "high", "promax", "platinum"),
    ("cust005", 20, "female", "20000-30000", "seattle", "single", "computer", "low", "studentbook", "bronze"),
]


def build_inventory(today: date):
    prices = {p[0]: (p[1], p[2], p[3]) for p in PRODUCTS}
    rows = []
    for product_id, (qty, threshold, reorder_qty, restock_days) in STOCK.items():
        name, category, price = prices[product_id]
        rows.append((product_id, name, category, qty, qty > 0, threshold, reorder_qty,
                     today - timedelta(days=restock_days), price))
    return rows


def build_orders(today: date):
    prices = {p[0]: (p[1], p[3]) for p in PRODUCTS}
    rows = []
    for (order_id, cust, prod, status, shipping, ret, ordered_ago, delivery_offset, qty, _pay) in ORDERS:
        name, price = prices[prod]
        delivery = today + timedelta(days=delivery_offset) if delivery_offset is not None else None
        rows.append((order_id, cust, prod, name, status, shipping, ret,
                     today - timedelta(days=ordered_ago), delivery, qty, price, qty * price))
    return rows


def build_purchase_history(today: date):
    """One row per delivered order (same customer, product, date, quantity, amount)."""
    prices = {p[0]: p[3] for p in PRODUCTS}
    return [
        (cust, prod, today - timedelta(days=ordered_ago), qty, float(qty * prices[prod]), pay)
        for (_id, cust, prod, status, _s, _r, ordered_ago, _d, qty, pay) in ORDERS
        if status == "delivered"
    ]


def build_products():
    return [(pid, name, cat, float(price), desc, rating, pop) for pid, name, cat, price, desc, rating, pop in PRODUCTS]


SEEDS = [
    ("order_management.customers",
     "INSERT INTO order_management.customers (customer_id, first_name, last_name, email, phone, address, city, state, zip_code, created_date) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
     lambda t: CUSTOMERS),
    ("product_recommendation.product_catalog",
     "INSERT INTO product_recommendation.product_catalog (product_id, product_name, category, price, description, rating, popularity) VALUES (%s,%s,%s,%s,%s,%s,%s)",
     lambda t: build_products()),
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
     lambda t: PERSONALIZATION),
]

# Each query returns offending rows; every one must return nothing.
CONSISTENCY_CHECKS = {
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
    failures = []
    for name, sql in CONSISTENCY_CHECKS.items():
        bad = conn.execute(sql).fetchall()
        if bad:
            failures.append(f"{name}: {[r[0] for r in bad]}")
    if failures:
        raise SystemExit("Consistency check failed:\n  " + "\n  ".join(failures))
    print("Consistency checks passed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="drop the three demo schemas before creating them")
    args = parser.parse_args()

    today = date.today()
    with psycopg.connect(load_connection_string()) as conn:
        if args.reset:
            for schema in SCHEMAS:
                conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            print(f"Dropped schemas: {', '.join(SCHEMAS)}")
        conn.execute(SCHEMA_SQL)

        for table, insert_sql, rows in SEEDS:
            (count,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            if count:
                print(f"{table}: {count} rows, skipping seed")
                continue
            data = rows(today)
            with conn.cursor() as cur:
                cur.executemany(insert_sql, data)
            print(f"{table}: seeded {len(data)} rows")

        check_consistency(conn)
        conn.commit()

        print("\nRow counts:")
        total = 0
        for table, _, _ in SEEDS:
            (n,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            total += n
            print(f"  {table}: {n}")
        print(f"  total: {total}")


if __name__ == "__main__":
    main()
