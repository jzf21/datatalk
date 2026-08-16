"""The evaluation fixture: a deterministic synthetic business.

Two logical sources, mirroring how a real workspace is shaped:

    sales   -- customers, products, orders, order_items, refunds
    events  -- page_views, marketing_spend

They are separate **sources**, not separate schemas, on purpose. DataTalk's
distinctive claim is that the agent routes each statement to the right warehouse
and composes across them without a join engine; a single-source suite can score
SQL correctness but cannot score that. Keeping ``marketing_spend`` away from
``orders`` is what makes "cost per order by channel" a genuine two-source
question rather than a join.

Determinism is the whole point of this module -- every golden answer in the
suite is a function of these exact bytes -- so nothing here touches
``random``. :class:`Rng` is a Park-Miller LCG we own outright. ``random.Random``
is only *documented* as reproducible for ``random()``; the derived helpers
(``randint``, ``choice``, ``shuffle``) carry no such promise across releases,
and a silent reshuffle would not fail loudly, it would quietly change every
expected number in the suite. :data:`FINGERPRINT` is the backstop: the seeder
verifies it before writing a row, so a generator that drifts for any reason at
all is a hard error rather than a mysterious drop in accuracy.

Dates are fixed to the first half of 2025 and every golden question names an
absolute window. A case phrased "last month" would decay with the calendar, and
a suite whose pass rate rots on its own is worse than no suite.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

# --- reproducible randomness --------------------------------------------------


class Rng:
    """Park-Miller minimal-standard LCG: ``s = s * 48271 mod (2**31 - 1)``.

    Ten lines of arithmetic we control, chosen over the stdlib precisely because
    the stdlib's cross-version guarantees are narrower than this fixture needs.
    """

    _M = 2**31 - 1
    _A = 48271

    def __init__(self, seed: int) -> None:
        self._s = seed % self._M or 1

    def next(self) -> int:
        self._s = (self._s * self._A) % self._M
        return self._s

    def rand(self) -> float:
        """Uniform in [0, 1)."""
        return (self.next() - 1) / (self._M - 1)

    def randint(self, low: int, high: int) -> int:
        """Uniform integer in [low, high], inclusive."""
        return low + self.next() % (high - low + 1)

    def choice(self, seq: Sequence[Any]) -> Any:
        return seq[self.next() % len(seq)]

    def weighted(self, pairs: Sequence[tuple[Any, int]]) -> Any:
        """Pick from ``(value, weight)`` pairs. Integer weights only, so the
        selection is exact arithmetic rather than float comparison."""
        total = sum(w for _, w in pairs)
        roll = self.next() % total
        for value, weight in pairs:
            if roll < weight:
                return value
            roll -= weight
        return pairs[-1][0]


# --- the business -------------------------------------------------------------

SEED = 20250101

PERIOD_START = date(2025, 1, 1)
PERIOD_END = date(2025, 6, 30)

COUNTRIES = [("US", 40), ("DE", 20), ("GB", 15), ("FR", 15), ("JP", 10)]
SEGMENTS = [("SMB", 55), ("Mid-Market", 30), ("Enterprise", 15)]
CHANNELS = ["organic", "paid_search", "email", "social"]
STATUSES = [("delivered", 70), ("shipped", 12), ("pending", 8), ("cancelled", 10)]
DEVICES = [("desktop", 55), ("mobile", 38), ("tablet", 7)]
REFUND_REASONS = ["damaged", "wrong_item", "changed_mind", "late_delivery"]
PATHS = [("/", 30), ("/pricing", 18), ("/product", 26), ("/docs", 16), ("/checkout", 10)]

# Orders per month. Deliberately non-monotonic, with March a clear peak: a
# "which month was strongest" case has to have one unambiguous answer, and a
# smooth ramp would make "the trend" a matter of opinion.
ORDERS_PER_MONTH = {1: 260, 2: 280, 3: 420, 4: 330, 5: 360, 6: 350}

# Products: (category, name, unit_price). Prices are distinct enough per
# category that a category-level aggregate cannot coincide by accident.
PRODUCT_CATALOG: list[tuple[str, str, float]] = [
    ("Audio", "Aurora Headphones", 199.00),
    ("Audio", "Aurora Headphones Pro", 299.00),
    ("Audio", "Pebble Earbuds", 89.00),
    ("Audio", "Pebble Earbuds Mini", 69.00),
    ("Audio", "Cadence Speaker", 149.00),
    ("Audio", "Cadence Soundbar", 329.00),
    ("Audio", "Studio Microphone", 179.00),
    ("Computers", "Meridian Laptop 13", 1099.00),
    ("Computers", "Meridian Laptop 15", 1399.00),
    ("Computers", "Meridian Laptop Pro", 1899.00),
    ("Computers", "Atlas Desktop", 899.00),
    ("Computers", "Atlas Desktop Plus", 1249.00),
    ("Computers", "Nimbus Tablet", 549.00),
    ("Computers", "Nimbus Tablet Pro", 799.00),
    ("Home Office", "Vantage Monitor 24", 249.00),
    ("Home Office", "Vantage Monitor 27", 379.00),
    ("Home Office", "Vantage Monitor 32", 599.00),
    ("Home Office", "Ridge Standing Desk", 649.00),
    ("Home Office", "Ridge Desk Mat", 39.00),
    ("Home Office", "Harbor Task Chair", 429.00),
    ("Home Office", "Harbor Lumbar Chair", 689.00),
    ("Home Office", "Beacon Desk Lamp", 79.00),
    ("Wearables", "Pulse Watch", 249.00),
    ("Wearables", "Pulse Watch Sport", 299.00),
    ("Wearables", "Pulse Band", 99.00),
    ("Wearables", "Summit Fitness Tracker", 129.00),
    ("Wearables", "Summit Heart Monitor", 89.00),
    ("Accessories", "Drift Mouse", 59.00),
    ("Accessories", "Drift Mouse Pro", 89.00),
    ("Accessories", "Keystone Keyboard", 119.00),
    ("Accessories", "Keystone Keyboard Mini", 89.00),
    ("Accessories", "Anchor USB Hub", 49.00),
    ("Accessories", "Anchor Dock", 189.00),
    ("Accessories", "Tether Cable Kit", 29.00),
    ("Accessories", "Shield Laptop Sleeve", 45.00),
    ("Cameras", "Lumen Webcam", 129.00),
    ("Cameras", "Lumen Webcam 4K", 219.00),
    ("Cameras", "Vista Action Camera", 349.00),
    ("Cameras", "Vista Action Camera Pro", 499.00),
    ("Cameras", "Prism Ring Light", 69.00),
]

CUSTOMER_FIRST = [
    "Ada", "Bram", "Chen", "Dara", "Eli", "Fen", "Gita", "Hugo", "Ines", "Jonas",
    "Kira", "Liam", "Mira", "Noor", "Omar", "Pia", "Quinn", "Rosa", "Sami", "Tova",
]
CUSTOMER_LAST = [
    "Alvarez", "Brandt", "Cho", "Dubois", "Eriksen", "Farid", "Grover", "Haas",
    "Iyer", "Jansen", "Kowalski", "Lindqvist", "Moreau", "Novak", "Okafor",
    "Petrov", "Rahman", "Silva", "Tanaka", "Volkov",
]

N_CUSTOMERS = 300
N_PAGE_VIEWS = 12000

# Marketing spend per channel per day, in whole cents of variation around a
# fixed base, so cost-per-order questions have a stable answer.
CHANNEL_SPEND_BASE = {"organic": 180, "paid_search": 940, "email": 260, "social": 520}


@dataclass
class Fixture:
    """Every table of the fixture, as plain rows ready for parameter binding."""

    customers: list[tuple] = field(default_factory=list)
    products: list[tuple] = field(default_factory=list)
    orders: list[tuple] = field(default_factory=list)
    order_items: list[tuple] = field(default_factory=list)
    refunds: list[tuple] = field(default_factory=list)
    page_views: list[tuple] = field(default_factory=list)
    marketing_spend: list[tuple] = field(default_factory=list)

    def table(self, source: str, name: str) -> list[tuple]:
        return getattr(self, name)

    @property
    def row_counts(self) -> dict[str, int]:
        return {
            name: len(getattr(self, name))
            for name in (
                "customers",
                "products",
                "orders",
                "order_items",
                "refunds",
                "page_views",
                "marketing_spend",
            )
        }


def _month_days(month: int) -> int:
    nxt = date(2025, month + 1, 1) if month < 12 else date(2026, 1, 1)
    return (nxt - date(2025, month, 1)).days


def build_fixture() -> Fixture:
    """Generate the whole fixture. Pure: same bytes on every machine, forever."""
    rng = Rng(SEED)
    fx = Fixture()

    # --- products ---------------------------------------------------------
    for pid, (category, name, price) in enumerate(PRODUCT_CATALOG, start=1):
        fx.products.append((pid, name, category, round(price, 2)))

    # --- customers --------------------------------------------------------
    for cid in range(1, N_CUSTOMERS + 1):
        first = CUSTOMER_FIRST[(cid * 7) % len(CUSTOMER_FIRST)]
        last = CUSTOMER_LAST[(cid * 13) % len(CUSTOMER_LAST)]
        country = rng.weighted(COUNTRIES)
        segment = rng.weighted(SEGMENTS)
        # Signups spread over 2024 so "customers who signed up before 2025" is a
        # real filter rather than everyone.
        signup = date(2024, 1, 1) + timedelta(days=rng.randint(0, 365))
        fx.customers.append((cid, f"{first} {last}", country, segment, signup))

    segment_of = {c[0]: c[3] for c in fx.customers}
    price_of = {p[0]: p[3] for p in fx.products}

    # --- orders and their items ------------------------------------------
    order_id = 0
    item_id = 0
    order_totals: dict[int, float] = {}
    for month, count in ORDERS_PER_MONTH.items():
        days = _month_days(month)
        for _ in range(count):
            order_id += 1
            customer_id = rng.randint(1, N_CUSTOMERS)
            order_date = date(2025, month, rng.randint(1, days))
            status = rng.weighted(STATUSES)
            channel = rng.choice(CHANNELS)
            fx.orders.append((order_id, customer_id, order_date, status, channel))

            # Enterprise buys deeper baskets; that gradient is what makes a
            # segment question worth asking.
            segment = segment_of[customer_id]
            max_lines = {"SMB": 2, "Mid-Market": 3, "Enterprise": 5}[segment]
            total = 0.0
            for _line in range(rng.randint(1, max_lines)):
                item_id += 1
                product_id = rng.randint(1, len(fx.products))
                quantity = rng.randint(1, 3 if segment == "SMB" else 6)
                discount = rng.weighted(
                    [(0.0, 55), (0.05, 20), (0.10, 15), (0.15, 7), (0.20, 3)]
                )
                unit_price = price_of[product_id]
                fx.order_items.append(
                    (item_id, order_id, product_id, quantity, unit_price, discount)
                )
                total += quantity * unit_price * (1 - discount)
            order_totals[order_id] = round(total, 2)

    # --- refunds ----------------------------------------------------------
    # Only delivered orders can be refunded, and only a partial amount, so
    # "gross vs net revenue" is a real distinction the context model defines.
    refund_id = 0
    for oid, _cid, odate, status, _channel in fx.orders:
        if status != "delivered" or rng.rand() > 0.06:
            continue
        refund_id += 1
        share = rng.weighted([(0.25, 30), (0.5, 30), (1.0, 40)])
        amount = round(order_totals[oid] * share, 2)
        refund_date = odate + timedelta(days=rng.randint(3, 21))
        fx.refunds.append(
            (refund_id, oid, refund_date, amount, rng.choice(REFUND_REASONS))
        )

    # --- events source ----------------------------------------------------
    span_days = (PERIOD_END - PERIOD_START).days
    for i in range(N_PAGE_VIEWS):
        day_offset = rng.randint(0, span_days)
        event_time = datetime.combine(
            PERIOD_START + timedelta(days=day_offset), datetime.min.time()
        ) + timedelta(seconds=rng.randint(0, 86399))
        path = rng.weighted(PATHS)
        if path == "/product":
            path = f"/product/{rng.randint(1, len(fx.products))}"
        # Two thirds of traffic is attributable to a known customer; the rest is
        # anonymous, so "sessions per customer" has to cope with NULLs.
        customer_id = rng.randint(1, N_CUSTOMERS) if rng.rand() < 0.66 else None
        referrer = rng.weighted(
            [("organic", 42), ("paid_search", 24), ("email", 14), ("social", 12), ("direct", 8)]
        )
        fx.page_views.append(
            (
                event_time,
                f"s{i // 3:05d}",
                customer_id,
                path,
                rng.weighted(DEVICES),
                rng.weighted(COUNTRIES),
                referrer,
            )
        )

    for day_offset in range(span_days + 1):
        spend_date = PERIOD_START + timedelta(days=day_offset)
        for channel in CHANNELS:
            base = CHANNEL_SPEND_BASE[channel]
            amount = round(base + rng.randint(-base // 5, base // 5), 2)
            fx.marketing_spend.append((spend_date, channel, amount))

    return fx


# --- drift detection ----------------------------------------------------------


def fingerprint(fx: Fixture) -> str:
    """A hash of every generated value.

    Cheap insurance on the one property this whole package rests on. If a
    refactor, a Python upgrade or an edit to the weights above changes a single
    cell, the seeder refuses to run instead of quietly invalidating every
    reference query's answer.
    """
    h = hashlib.sha256()
    for name in (
        "customers",
        "products",
        "orders",
        "order_items",
        "refunds",
        "page_views",
        "marketing_spend",
    ):
        h.update(name.encode())
        for row in getattr(fx, name):
            h.update("|".join(str(v) for v in row).encode())
            h.update(b"\n")
    return h.hexdigest()


# Pinned by `python -m datatalk.evals.dataset`, which prints the current value.
FINGERPRINT = "9373ad249491e437b5ea8b4e3b78643a8bfa104962ed42a77486488f5f987bfc"


class FixtureDriftError(RuntimeError):
    """The generator no longer produces the data the golden cases were written against."""


def build_verified_fixture() -> Fixture:
    fx = build_fixture()
    actual = fingerprint(fx)
    if actual != FINGERPRINT:
        raise FixtureDriftError(
            "The evaluation fixture has changed.\n"
            f"  expected {FINGERPRINT}\n"
            f"  actual   {actual}\n"
            "Every reference query in the suite is written against the old data, "
            "so accuracy numbers from before and after this change are not "
            "comparable. If the change is intentional, update FINGERPRINT in "
            "datatalk/evals/dataset.py and re-baseline the suite."
        )
    return fx


# --- schema -------------------------------------------------------------------

SOURCE_TABLES: dict[str, tuple[str, ...]] = {
    "sales": ("customers", "products", "orders", "order_items", "refunds"),
    "events": ("page_views", "marketing_spend"),
}

COLUMNS: dict[str, tuple[str, ...]] = {
    "customers": ("customer_id", "name", "country", "segment", "signup_date"),
    "products": ("product_id", "name", "category", "unit_price"),
    "orders": ("order_id", "customer_id", "order_date", "status", "channel"),
    "order_items": (
        "order_item_id",
        "order_id",
        "product_id",
        "quantity",
        "unit_price",
        "discount",
    ),
    "refunds": ("refund_id", "order_id", "refund_date", "amount", "reason"),
    "page_views": (
        "event_time",
        "session_id",
        "customer_id",
        "path",
        "device",
        "country",
        "referrer",
    ),
    "marketing_spend": ("spend_date", "channel", "amount"),
}

POSTGRES_DDL: dict[str, str] = {
    "customers": """
        CREATE TABLE customers (
            customer_id  INTEGER PRIMARY KEY,
            name         TEXT NOT NULL,
            country      TEXT NOT NULL,
            segment      TEXT NOT NULL,
            signup_date  DATE NOT NULL
        )""",
    "products": """
        CREATE TABLE products (
            product_id   INTEGER PRIMARY KEY,
            name         TEXT NOT NULL,
            category     TEXT NOT NULL,
            unit_price   NUMERIC(10, 2) NOT NULL
        )""",
    "orders": """
        CREATE TABLE orders (
            order_id     INTEGER PRIMARY KEY,
            customer_id  INTEGER NOT NULL REFERENCES customers(customer_id),
            order_date   DATE NOT NULL,
            status       TEXT NOT NULL,
            channel      TEXT NOT NULL
        )""",
    "order_items": """
        CREATE TABLE order_items (
            order_item_id INTEGER PRIMARY KEY,
            order_id      INTEGER NOT NULL REFERENCES orders(order_id),
            product_id    INTEGER NOT NULL REFERENCES products(product_id),
            quantity      INTEGER NOT NULL,
            unit_price    NUMERIC(10, 2) NOT NULL,
            discount      NUMERIC(4, 3) NOT NULL
        )""",
    "refunds": """
        CREATE TABLE refunds (
            refund_id     INTEGER PRIMARY KEY,
            order_id      INTEGER NOT NULL REFERENCES orders(order_id),
            refund_date   DATE NOT NULL,
            amount        NUMERIC(10, 2) NOT NULL,
            reason        TEXT NOT NULL
        )""",
    "page_views": """
        CREATE TABLE page_views (
            event_time   TIMESTAMP NOT NULL,
            session_id   TEXT NOT NULL,
            customer_id  INTEGER,
            path         TEXT NOT NULL,
            device       TEXT NOT NULL,
            country      TEXT NOT NULL,
            referrer     TEXT NOT NULL
        )""",
    "marketing_spend": """
        CREATE TABLE marketing_spend (
            spend_date   DATE NOT NULL,
            channel      TEXT NOT NULL,
            amount       NUMERIC(10, 2) NOT NULL
        )""",
}

# ClickHouse gets its own DDL rather than a translation layer, for the same
# reason `warehouse/` keeps one adapter per engine: the differences are real
# (no foreign keys, an engine and sort key per table, explicit Nullable) and a
# generic emitter would hide them.
CLICKHOUSE_DDL: dict[str, str] = {
    "page_views": """
        CREATE TABLE page_views (
            event_time   DateTime,
            session_id   String,
            customer_id  Nullable(Int32),
            path         String,
            device       String,
            country      String,
            referrer     String
        ) ENGINE = MergeTree ORDER BY (event_time, session_id)""",
    "marketing_spend": """
        CREATE TABLE marketing_spend (
            spend_date   Date,
            channel      String,
            amount       Decimal(10, 2)
        ) ENGINE = MergeTree ORDER BY (spend_date, channel)""",
}

# Column comments. The catalog renders names only, so these reach the model
# through `describe_source` -- the same path a real warehouse's comments take.
COMMENTS: dict[tuple[str, str], str] = {
    ("orders", "status"): "delivered | shipped | pending | cancelled",
    ("orders", "channel"): "Acquisition channel the order came through",
    ("order_items", "discount"): "Fractional discount, 0.10 = 10% off",
    ("order_items", "unit_price"): "Price per unit at time of sale",
    ("refunds", "amount"): "Refunded amount in the order's currency",
    ("customers", "segment"): "SMB | Mid-Market | Enterprise",
    ("page_views", "customer_id"): "NULL for anonymous traffic",
    ("marketing_spend", "amount"): "Daily ad spend for the channel",
}


def describe() -> str:
    """One-line summary of the fixture, for the CLI banner."""
    fx = build_fixture()
    counts = ", ".join(f"{n}={c}" for n, c in fx.row_counts.items())
    return f"{counts} (fingerprint {fingerprint(fx)[:12]}…)"


def _main(argv: Iterable[str] | None = None) -> int:
    fx = build_fixture()
    print(f"fingerprint = {fingerprint(fx)}")
    for name, count in fx.row_counts.items():
        print(f"  {name:<16} {count:>6}")
    return 0


if __name__ == "__main__":  # pragma: no cover - developer utility
    raise SystemExit(_main())
