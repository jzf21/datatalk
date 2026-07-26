"""Warehouse adapters: the only place in DataTalk that names a SQL engine.

Callers hold a :class:`~datatalk.warehouse.base.Warehouse` obtained from a
:class:`~datatalk.context.TenantContext` and never import an adapter directly.
"""

from datatalk.warehouse.base import (
    ALLOWED_LEADERS,
    CORE_FORBIDDEN,
    NO_LIMIT_LEADERS,
    Column,
    Dialect,
    QueryResult,
    Table,
    Warehouse,
    WarehouseError,
    WarehouseSpec,
)
from datatalk.warehouse.registry import (
    DEFAULT_DATABASES,
    DEFAULT_PORTS,
    DEFAULT_USERS,
    WAREHOUSE_TYPES,
    UnknownWarehouseTypeError,
    create,
    dialect_for,
    fingerprint,
)

__all__ = [
    "ALLOWED_LEADERS",
    "CORE_FORBIDDEN",
    "NO_LIMIT_LEADERS",
    "DEFAULT_DATABASES",
    "DEFAULT_PORTS",
    "DEFAULT_USERS",
    "WAREHOUSE_TYPES",
    "Column",
    "Dialect",
    "QueryResult",
    "Table",
    "Warehouse",
    "WarehouseError",
    "WarehouseSpec",
    "UnknownWarehouseTypeError",
    "create",
    "dialect_for",
    "fingerprint",
]
