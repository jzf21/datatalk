"""Warehouse construction and identity.

One place that knows the full set of engines, so adding a third is a new module
plus one line in :data:`ADAPTERS`.
"""

from __future__ import annotations

import hashlib

from datatalk.warehouse.base import Dialect, Warehouse, WarehouseSpec
from datatalk.warehouse.clickhouse import CLICKHOUSE_DIALECT, ClickHouseWarehouse
from datatalk.warehouse.jira import JIRA_DIALECT, JiraWarehouse
from datatalk.warehouse.postgres import POSTGRES_DIALECT, PostgresWarehouse

ADAPTERS = {
    "clickhouse": (ClickHouseWarehouse, CLICKHOUSE_DIALECT),
    "postgres": (PostgresWarehouse, POSTGRES_DIALECT),
    # Not a live engine: a synced copy in the sync store. See warehouse/jira.py.
    "jira": (JiraWarehouse, JIRA_DIALECT),
}

WAREHOUSE_TYPES = tuple(ADAPTERS)

# Per-engine connection defaults, shared by the API request models and the
# bootstrap path so the two cannot drift.
DEFAULT_PORTS = {"clickhouse": 8123, "postgres": 5432}
DEFAULT_USERS = {"clickhouse": "default", "postgres": "postgres"}
DEFAULT_DATABASES = {"clickhouse": "default", "postgres": "postgres"}


class UnknownWarehouseTypeError(ValueError):
    """Raised for a connection row whose ``type`` has no adapter."""


def dialect_for(type_: str) -> Dialect:
    try:
        return ADAPTERS[type_][1]
    except KeyError:
        raise UnknownWarehouseTypeError(type_) from None


def create(spec: WarehouseSpec) -> Warehouse:
    """Connect to ``spec``. Raises WarehouseError if the warehouse is unreachable.

    The caller owns the result. Prefer :func:`datatalk.clients.warehouse_for`,
    which caches one warehouse per fingerprint; use this directly only for
    throwaway connections (testing a candidate) that must not enter the
    registry.
    """
    try:
        adapter = ADAPTERS[spec.type][0]
    except KeyError:
        raise UnknownWarehouseTypeError(spec.type) from None
    return adapter(spec)


def fingerprint(spec: WarehouseSpec) -> str:
    """Identity of everything that changes what this warehouse returns.

    Includes the introspection-shaping fields, not just the connection tuple:
    two sources with identical credentials but different allowlists must not
    share a schema cache entry, or one sees the other's excluded tables. It also
    includes ``type``, so a ClickHouse and a Postgres source on the same
    host:port cannot collide onto one client.
    """
    raw = "|".join(
        [
            spec.type,
            spec.host,
            str(spec.port),
            spec.username,
            spec.password,
            spec.database,
            str(int(spec.secure)),
            spec.sslmode or "",
            ",".join(spec.introspect_databases),
            ",".join(spec.introspect_tables),
            ",".join(spec.introspect_exclude_patterns),
            str(spec.introspect_sample_rows),
            str(spec.introspect_max_tables),
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
