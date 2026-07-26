"""The warehouse abstraction: one protocol, one spec, one dialect per engine.

Everything engine-specific in DataTalk lives under :mod:`datatalk.warehouse`.
Outside this package no module may name ClickHouse or Postgres: callers hold a
:class:`Warehouse` and ask it to ping, query, or introspect.

The three value types here are deliberately plain and frozen so they can be
built once per request and handed to agent worker threads:

* :class:`WarehouseSpec` -- everything needed to *reach* one warehouse, plus the
  per-source guardrail and introspection limits. Replaces the old trick of
  overlaying an org's row onto ``Settings`` as ``CLICKHOUSE_*`` keys, which had
  no way to express a second source.
* :class:`Dialect` -- the per-engine knowledge the *generic* code needs: which
  extra keywords are unsafe, how identifiers quote, and the SQL hint the LLM
  gets. Keeping this as data means the executor and the prompts stay neutral.
* :class:`QueryResult` / :class:`Table` / :class:`Column` -- results, in a shape
  no engine leaks into.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# Statements the agent may run, whatever the engine. A leader outside this set
# is rejected before any dialect gets a say.
ALLOWED_LEADERS = frozenset(
    {"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}
)

# Never allowed anywhere in a statement, on any engine.
CORE_FORBIDDEN = frozenset(
    {
        "INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "CREATE", "TRUNCATE",
        "RENAME", "GRANT", "REVOKE", "SET", "USE", "CALL", "REPLACE",
    }
)

# Statements for which we do NOT auto-inject a LIMIT.
NO_LIMIT_LEADERS = frozenset({"SHOW", "DESCRIBE", "DESC", "EXPLAIN"})


class WarehouseError(RuntimeError):
    """A warehouse could not be reached or a query failed.

    Wraps the driver's own exception so callers never have to catch
    ``clickhouse_connect.driver.exceptions.*`` and ``psycopg.Error`` separately.
    The original is kept on ``__cause__``.
    """


@dataclass(frozen=True)
class Dialect:
    """Per-engine facts the generic layers need.

    Data rather than subclasses: the executor's guardrails and the prompt
    builder both consume this, and neither should have to import an adapter.
    """

    name: str
    # Keywords unsafe on this engine specifically, on top of CORE_FORBIDDEN.
    extra_forbidden: frozenset[str] = frozenset()
    # Postgres' $$...$$ literals. Scanning them as opaque is only correct where
    # the engine actually has them -- on an engine without dollar quoting, a
    # bare $ is just a character and treating it as a quote would let a keyword
    # hide behind it.
    supports_dollar_quoting: bool = False
    identifier_quote: str = '"'
    # Appended to the schema catalog so prompts stay engine-neutral.
    prompt_hint: str = ""

    @property
    def forbidden(self) -> frozenset[str]:
        return CORE_FORBIDDEN | self.extra_forbidden

    def quote_ident(self, name: str) -> str:
        q = self.identifier_quote
        return f"{q}{name.replace(q, q + q)}{q}"

    def qualify(self, database: str, table: str) -> str:
        return f"{self.quote_ident(database)}.{self.quote_ident(table)}"


@dataclass(frozen=True)
class WarehouseSpec:
    """Everything needed to reach one warehouse, plus its limits.

    Frozen and hashable-by-value, so :func:`datatalk.warehouse.registry.fingerprint`
    can hash it and the client registry can key on the result: change a
    credential and the cached client and schema are invalidated for free.
    """

    type: str  # clickhouse | postgres
    host: str
    port: int
    username: str
    password: str = ""
    database: str = "default"
    secure: bool = False
    # Postgres only: disable/allow/require/verify-ca/verify-full. None = driver
    # default (``prefer``).
    sslmode: str | None = None

    # Introspection scope. On ClickHouse ``introspect_databases`` is a database
    # allowlist; on Postgres one connection sees one database, so the same list
    # is a *schema* allowlist. Both render as ``<namespace>.<table>``, which is
    # what the model has to type either way.
    introspect_databases: tuple[str, ...] = ()
    introspect_exclude_patterns: tuple[str, ...] = ()
    introspect_sample_rows: int = 3
    introspect_max_tables: int = 0

    # Guardrails.
    sql_default_limit: int = 1000
    sql_max_rows: int = 5000
    sql_timeout_seconds: int = 30

    def __repr__(self) -> str:  # never render the password
        return (
            f"<WarehouseSpec {self.type} {self.username}@{self.host}:{self.port}"
            f"/{self.database}>"
        )


@dataclass
class Column:
    name: str
    type: str
    comment: str = ""


@dataclass
class Table:
    database: str  # ClickHouse database / Postgres schema
    name: str
    engine: str = ""
    comment: str = ""
    total_rows: int | None = None
    columns: list[Column] = field(default_factory=list)
    sample_rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def qualified_name(self) -> str:
        return f"{self.database}.{self.name}"


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    row_count: int
    truncated: bool
    sql: str

    def to_records(self) -> list[dict]:
        return [dict(zip(self.columns, row)) for row in self.rows]


@runtime_checkable
class Warehouse(Protocol):
    """One connected warehouse. Implementations live beside this file.

    Implementations must be safe to share across agent worker threads: the
    client registry hands the same object to every concurrent report for an org.
    """

    spec: WarehouseSpec
    dialect: Dialect

    def ping(self) -> dict[str, Any]:
        """Verify connectivity; return at least ``version`` and ``database``.

        Raises :class:`WarehouseError` on failure -- callers present it to the
        user rather than logging a driver traceback.
        """
        ...

    def query(self, sql: str, *, timeout_s: int, max_rows: int) -> QueryResult:
        """Run one already-validated read-only statement.

        ``max_rows`` is a cap, not a hint: implementations return at most that
        many rows and set ``truncated`` when more were available.
        """
        ...

    def introspect(self, *, with_samples: bool = True) -> list[Table]:
        """Discover user tables, honouring the spec's allowlist and caps."""
        ...

    def close(self) -> None:
        ...


class BaseWarehouse:
    """Shared introspection walk; adapters supply the four engine queries.

    The loop -- allowlist, exclude patterns, table cap, per-table columns and
    optional samples -- is identical on every engine and is easy to get subtly
    wrong per adapter (an exclude pattern silently not applied leaks a table
    name into a prompt). Keeping it here means each adapter only writes the
    catalog queries its engine actually needs.
    """

    spec: WarehouseSpec
    dialect: Dialect

    def _list_namespaces(self) -> list[str]:
        """Databases (ClickHouse) or schemas (Postgres), system ones removed."""
        raise NotImplementedError

    def _fetch_tables(self, namespace: str) -> list[Table]:
        raise NotImplementedError

    def _fetch_columns(self, namespace: str, table: str) -> list[Column]:
        raise NotImplementedError

    def _fetch_sample_rows(
        self, namespace: str, table: str, limit: int
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _allowed_namespaces(self) -> list[str]:
        names = self._list_namespaces()
        allow = {n for n in self.spec.introspect_databases if n}
        if allow:
            names = [n for n in names if n in allow]
        return names

    def introspect(self, *, with_samples: bool = True) -> list[Table]:
        exclude = [p.lower() for p in self.spec.introspect_exclude_patterns if p]
        cap = self.spec.introspect_max_tables

        tables: list[Table] = []
        for namespace in self._allowed_namespaces():
            for table in self._fetch_tables(namespace):
                if exclude and any(p in table.name.lower() for p in exclude):
                    continue
                table.columns = self._fetch_columns(namespace, table.name)
                if with_samples and self.spec.introspect_sample_rows > 0:
                    table.sample_rows = self._fetch_sample_rows(
                        namespace, table.name, self.spec.introspect_sample_rows
                    )
                tables.append(table)
                if cap and len(tables) >= cap:
                    return tables
        return tables
