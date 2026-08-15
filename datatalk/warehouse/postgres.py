"""Postgres adapter, built on psycopg 3.

Two things here are load-bearing and easy to lose in a refactor:

* **A pool, not a connection.** One :class:`psycopg.Connection` serializes
  everything sent through it. The client registry hands a single warehouse
  object to every concurrent agent thread for an org, so a bare connection
  would turn parallel report queries into a queue.
* **``read_only`` on every session.** The regex guardrails in
  :mod:`datatalk.agent.executor` are the first line; this is the one that does
  not depend on our parser being right. A statement that slips past validation
  still cannot write, because the server refuses it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg import sql as pgsql
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

from datatalk.warehouse.base import (
    BaseWarehouse,
    Column,
    Dialect,
    QueryResult,
    Table,
    WarehouseError,
    WarehouseSpec,
)

# Schemas that are never user data. pg_toast, pg_catalog and friends all share
# the pg_ prefix; information_schema is named separately.
#
# left() rather than NOT LIKE 'pg\_%': psycopg parses % in a query string as a
# placeholder, so the LIKE form raises before it ever reaches the server.
_SYSTEM_SCHEMA_FILTER = (
    "left(nspname, 3) <> 'pg_' AND nspname <> 'information_schema'"
)

_POOL_MAX_SIZE = 4
_CONNECT_TIMEOUT_S = 10

POSTGRES_DIALECT = Dialect(
    name="postgres",
    extra_forbidden=frozenset(
        {
            # Writes and DDL that the shared core does not already cover.
            "COPY", "VACUUM", "ANALYZE", "REINDEX", "CLUSTER", "REFRESH",
            "COMMENT", "IMPORT", "LOCK",
            # `SELECT ... INTO newtable` is a write that passes the leader check.
            "INTO",
            # Procedural / session escapes.
            "DO", "PREPARE", "EXECUTE", "DEALLOCATE", "DISCARD", "RESET",
            "LISTEN", "NOTIFY", "UNLISTEN", "SECURITY",
            # Transaction control -- would let a statement escape the read-only
            # transaction we open around it.
            "BEGIN", "COMMIT", "ROLLBACK", "START", "SAVEPOINT", "ABORT", "END",
        }
    ),
    supports_dollar_quoting=True,
    identifier_quote='"',
    param_style="pyformat",
    prompt_hint=(
        "PostgreSQL dialect: date_trunc('month', ts), now(), count(*), "
        "count(DISTINCT x), etc. Qualify tables as schema.table."
    ),
)


class PostgresWarehouse(BaseWarehouse):
    """One Postgres database, reached through a small read-only pool."""

    dialect = POSTGRES_DIALECT

    def __init__(self, spec: WarehouseSpec) -> None:
        self.spec = spec
        self._pool = self._open_pool(spec)

    @staticmethod
    def _open_pool(spec: WarehouseSpec) -> ConnectionPool:
        params: dict[str, Any] = {
            "host": spec.host,
            "port": spec.port,
            "user": spec.username,
            "password": spec.password,
            "dbname": spec.database,
            "connect_timeout": _CONNECT_TIMEOUT_S,
        }
        if spec.sslmode:
            params["sslmode"] = spec.sslmode
        elif spec.secure:
            # The UI models TLS as one switch for both engines; on Postgres that
            # means "refuse to talk in the clear".
            params["sslmode"] = "require"

        pool = ConnectionPool(
            make_conninfo(**params),
            min_size=1,
            max_size=_POOL_MAX_SIZE,
            configure=_make_read_only,
            open=False,
            name=f"dt-{spec.host}-{spec.database}",
        )
        try:
            # Eager, so a bad host or password fails here rather than midway
            # through a report -- matching how the ClickHouse driver behaves.
            pool.open(wait=True, timeout=_CONNECT_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            try:
                pool.close()
            except Exception:  # noqa: BLE001
                pass
            raise WarehouseError(str(exc)) from exc
        return pool

    # --- protocol -------------------------------------------------------------

    def ping(self) -> dict[str, Any]:
        try:
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT version()")
                version = cur.fetchone()[0]
                cur.execute("SELECT current_database()")
                database = cur.fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            raise WarehouseError(str(exc)) from exc
        return {"version": str(version), "database": str(database)}

    def query(
        self,
        sql: str,
        *,
        timeout_s: int,
        max_rows: int,
        parameters: Mapping[str, Any] | None = None,
    ) -> QueryResult:
        try:
            with self._pool.connection() as conn:
                with conn.transaction(), conn.cursor() as cur:
                    # LOCAL, so it expires with this transaction and cannot
                    # leak onto the next borrower of this pooled connection.
                    cur.execute(
                        pgsql.SQL("SET LOCAL statement_timeout = {}").format(
                            pgsql.Literal(max(1, int(timeout_s)) * 1000)
                        )
                    )
                    # None, not {}: see _read below. An empty mapping still makes
                    # psycopg scan for placeholders, turning any literal % in an
                    # unparameterized statement into an error.
                    cur.execute(sql, parameters or None)
                    if cur.description is None:
                        # A statement that returns nothing got through
                        # validation; treat it as an empty result rather than
                        # raising on cur.fetchmany.
                        return QueryResult([], [], 0, False, sql)
                    columns = [d.name for d in cur.description]
                    # One extra row is how we know the cap actually bit.
                    fetched = cur.fetchmany(max_rows + 1)
        except Exception as exc:  # noqa: BLE001
            raise WarehouseError(str(exc)) from exc

        rows = [list(r) for r in fetched[:max_rows]]
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=len(fetched) > max_rows,
            sql=sql,
        )

    def close(self) -> None:
        try:
            self._pool.close()
        except Exception:  # noqa: BLE001 - close is best-effort
            pass

    # --- introspection --------------------------------------------------------

    def _read(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._pool.connection() as conn, conn.cursor() as cur:
            # None, not (): passing an empty tuple still makes psycopg scan the
            # query for placeholders, so any literal % becomes an error.
            cur.execute(sql, params or None)
            return cur.fetchall()

    def _list_namespaces(self) -> list[str]:
        rows = self._read(
            "SELECT nspname FROM pg_catalog.pg_namespace "
            f"WHERE {_SYSTEM_SCHEMA_FILTER} "
            "AND has_schema_privilege(oid, 'USAGE') "
            "ORDER BY nspname"
        )
        return [r[0] for r in rows]

    def _fetch_tables(self, namespace: str) -> list[Table]:
        rows = self._read(
            """
            SELECT c.relname,
                   CASE c.relkind
                       WHEN 'r' THEN 'table'
                       WHEN 'p' THEN 'partitioned table'
                       WHEN 'v' THEN 'view'
                       WHEN 'm' THEN 'materialized view'
                       WHEN 'f' THEN 'foreign table'
                   END,
                   COALESCE(obj_description(c.oid, 'pg_class'), ''),
                   -- reltuples is -1 until the table has been analyzed; a
                   -- negative row estimate in a prompt is worse than none.
                   CASE WHEN c.reltuples < 0 THEN NULL
                        ELSE c.reltuples::bigint END
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s
              AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND has_table_privilege(c.oid, 'SELECT')
            ORDER BY c.relname
            """,
            (namespace,),
        )
        return [
            Table(
                database=namespace,
                name=name,
                engine=kind or "",
                comment=comment or "",
                total_rows=total_rows,
            )
            for name, kind, comment, total_rows in rows
        ]

    def _fetch_columns(self, namespace: str, table: str) -> list[Column]:
        rows = self._read(
            """
            SELECT a.attname,
                   pg_catalog.format_type(a.atttypid, a.atttypmod),
                   COALESCE(col_description(a.attrelid, a.attnum), '')
            FROM pg_catalog.pg_attribute a
            JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s
              AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum
            """,
            (namespace, table),
        )
        return [Column(name=n, type=t, comment=c or "") for n, t, c in rows]

    def _fetch_sample_rows(
        self, namespace: str, table: str, limit: int
    ) -> list[dict[str, Any]]:
        stmt = pgsql.SQL("SELECT * FROM {} LIMIT {}").format(
            pgsql.Identifier(namespace, table), pgsql.Literal(int(limit))
        )
        try:
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.execute(stmt)
                cols = [d.name for d in cur.description or []]
                rows = cur.fetchall()
        except Exception:  # noqa: BLE001
            # Broken views and foreign tables with a dead server can't be
            # sampled; their columns are still useful, so skip only the rows.
            return []
        return [dict(zip(cols, row)) for row in rows]


def _make_read_only(conn: psycopg.Connection) -> None:
    """Pool ``configure`` hook: every session refuses writes.

    Set once per physical connection, outside a transaction, so it applies to
    every transaction that connection later serves.
    """
    conn.read_only = True
