"""ClickHouse adapter, built on the official clickhouse-connect client."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import clickhouse_connect

from datatalk.warehouse.base import (
    BaseWarehouse,
    Column,
    Dialect,
    QueryResult,
    Table,
    WarehouseError,
    WarehouseSpec,
)

# ClickHouse system databases we never expose to the LLM.
_SYSTEM_DATABASES = {
    "system",
    "information_schema",
    "INFORMATION_SCHEMA",
    "default_INFORMATION_SCHEMA",
}

CLICKHOUSE_DIALECT = Dialect(
    name="clickhouse",
    extra_forbidden=frozenset(
        {
            "ATTACH", "DETACH", "OPTIMIZE", "SYSTEM", "KILL", "MOVE",
            "EXCHANGE", "UNDROP",
        }
    ),
    supports_dollar_quoting=False,
    identifier_quote="`",
    param_style="curly",
    prompt_hint=(
        "ClickHouse dialect: toStartOfMonth(), toDate(), count(), uniqExact(), "
        "etc. Qualify tables as database.table."
    ),
)


class ClickHouseWarehouse(BaseWarehouse):
    """One ClickHouse endpoint.

    The underlying clickhouse-connect client is thread-safe for concurrent
    queries *provided* it does not pin a server session -- see the
    ``autogenerate_session_id`` note in :meth:`_connect`.
    """

    dialect = CLICKHOUSE_DIALECT

    def __init__(self, spec: WarehouseSpec) -> None:
        self.spec = spec
        self._client = self._connect(spec)

    @staticmethod
    def _connect(spec: WarehouseSpec):
        try:
            return clickhouse_connect.get_client(
                host=spec.host,
                port=spec.port,
                username=spec.username,
                password=spec.password,
                database=spec.database,
                secure=spec.secure,
                query_limit=0,  # we enforce our own limits in the executor
                # Without this the driver pins a server-side session id per
                # client, and ClickHouse serializes concurrent queries on one
                # session (SESSION_IS_LOCKED). Agents run in parallel worker
                # threads sharing a cached client, so concurrency is normal.
                autogenerate_session_id=False,
            )
        except Exception as exc:  # noqa: BLE001 - one error type for all engines
            raise WarehouseError(str(exc)) from exc

    # --- protocol -------------------------------------------------------------

    def ping(self) -> dict[str, Any]:
        try:
            version = self._client.command("SELECT version()")
            current_db = self._client.command("SELECT currentDatabase()")
        except Exception as exc:  # noqa: BLE001
            raise WarehouseError(str(exc)) from exc
        return {"version": str(version), "database": str(current_db)}

    def query(
        self,
        sql: str,
        *,
        timeout_s: int,
        max_rows: int,
        parameters: Mapping[str, Any] | None = None,
    ) -> QueryResult:
        try:
            result = self._client.query(
                sql,
                # `{name:Type}` placeholders, bound by the driver -- the same
                # mechanism the introspection queries below already use.
                parameters=dict(parameters) if parameters else None,
                settings={
                    "max_execution_time": timeout_s,
                    # Server-side cap as a second layer over our row slicing.
                    "max_result_rows": max_rows,
                    "result_overflow_mode": "break",
                },
            )
        except Exception as exc:  # noqa: BLE001
            raise WarehouseError(str(exc)) from exc

        all_rows = [list(r) for r in result.result_rows]
        return QueryResult(
            columns=list(result.column_names),
            rows=all_rows[:max_rows],
            row_count=min(len(all_rows), max_rows),
            truncated=len(all_rows) > max_rows,
            sql=sql,
        )

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - close is best-effort
            pass

    # --- introspection --------------------------------------------------------

    def _list_namespaces(self) -> list[str]:
        rows = self._client.query("SHOW DATABASES").result_rows
        return [r[0] for r in rows if r[0] not in _SYSTEM_DATABASES]

    def _fetch_tables(self, namespace: str) -> list[Table]:
        result = self._client.query(
            """
            SELECT name, engine, comment, total_rows
            FROM system.tables
            WHERE database = {db:String} AND NOT is_temporary
            ORDER BY name
            """,
            parameters={"db": namespace},
        )
        return [
            Table(
                database=namespace,
                name=name,
                engine=engine or "",
                comment=comment or "",
                total_rows=total_rows,
            )
            for name, engine, comment, total_rows in result.result_rows
        ]

    def _fetch_columns(self, namespace: str, table: str) -> list[Column]:
        result = self._client.query(
            """
            SELECT name, type, comment
            FROM system.columns
            WHERE database = {db:String} AND table = {tbl:String}
            ORDER BY position
            """,
            parameters={"db": namespace, "tbl": table},
        )
        return [
            Column(name=n, type=t, comment=c or "") for n, t, c in result.result_rows
        ]

    def _fetch_sample_rows(
        self, namespace: str, table: str, limit: int
    ) -> list[dict[str, Any]]:
        qualified = self.dialect.qualify(namespace, table)
        try:
            result = self._client.query(f"SELECT * FROM {qualified} LIMIT {int(limit)}")
        except Exception:  # noqa: BLE001
            # Some engines (e.g. views with broken deps) can't be sampled; skip.
            return []
        cols = result.column_names
        return [dict(zip(cols, row)) for row in result.result_rows]
