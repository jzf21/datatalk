"""Runtime schema discovery.

Nothing about the target schema is hardcoded. We read ClickHouse's own
``system`` tables plus a few sample rows and build a compact text description
("schema context") that the LLM uses to write SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from clickhouse_connect.driver.client import Client

from datatalk.config import Settings, get_settings
from datatalk.db.clickhouse import get_client


@dataclass
class Column:
    name: str
    type: str
    comment: str = ""


@dataclass
class Table:
    database: str
    name: str
    engine: str = ""
    comment: str = ""
    total_rows: int | None = None
    columns: list[Column] = field(default_factory=list)
    sample_rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def qualified_name(self) -> str:
        return f"{self.database}.{self.name}"


# ClickHouse system databases we never want to expose to the LLM.
_SYSTEM_DATABASES = {
    "system",
    "information_schema",
    "INFORMATION_SCHEMA",
    "default_INFORMATION_SCHEMA",
}


def list_databases(
    client: Client | None = None, settings: Settings | None = None
) -> list[str]:
    client = client or get_client()
    settings = settings or get_settings()
    rows = client.query("SHOW DATABASES").result_rows
    names = [r[0] for r in rows if r[0] not in _SYSTEM_DATABASES]

    allow = settings.introspect_database_list
    if allow:
        allow_set = set(allow)
        names = [n for n in names if n in allow_set]
    return names


def _fetch_tables(client: Client, database: str) -> list[Table]:
    result = client.query(
        """
        SELECT name, engine, comment, total_rows
        FROM system.tables
        WHERE database = {db:String} AND NOT is_temporary
        ORDER BY name
        """,
        parameters={"db": database},
    )
    tables: list[Table] = []
    for name, engine, comment, total_rows in result.result_rows:
        tables.append(
            Table(
                database=database,
                name=name,
                engine=engine or "",
                comment=comment or "",
                total_rows=total_rows,
            )
        )
    return tables


def _fetch_columns(client: Client, database: str, table: str) -> list[Column]:
    result = client.query(
        """
        SELECT name, type, comment
        FROM system.columns
        WHERE database = {db:String} AND table = {tbl:String}
        ORDER BY position
        """,
        parameters={"db": database, "tbl": table},
    )
    return [Column(name=n, type=t, comment=c or "") for n, t, c in result.result_rows]


def _fetch_sample_rows(
    client: Client, database: str, table: str, limit: int
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    try:
        result = client.query(f"SELECT * FROM `{database}`.`{table}` LIMIT {int(limit)}")
    except Exception:
        # Some engines (e.g. views with broken deps) can't be sampled; skip.
        return []
    cols = result.column_names
    return [dict(zip(cols, row)) for row in result.result_rows]


def introspect(
    client: Client | None = None,
    settings: Settings | None = None,
    *,
    with_samples: bool = True,
) -> list[Table]:
    """Discover all user tables with columns and (optionally) sample rows."""
    client = client or get_client()
    settings = settings or get_settings()
    exclude = settings.introspect_exclude_list

    tables: list[Table] = []
    for database in list_databases(client, settings):
        for table in _fetch_tables(client, database):
            if exclude and any(p in table.name.lower() for p in exclude):
                continue
            table.columns = _fetch_columns(client, database, table.name)
            if with_samples:
                rows = _fetch_sample_rows(
                    client, database, table.name, settings.introspect_sample_rows
                )
                table.sample_rows = rows
            tables.append(table)
            if (
                settings.introspect_max_tables
                and len(tables) >= settings.introspect_max_tables
            ):
                return tables
    return tables


def _format_sample_value(value: Any, max_len: int = 60) -> str:
    text = "NULL" if value is None else str(value)
    text = text.replace("\n", " ")
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def build_schema_context(tables: list[Table]) -> str:
    """Render tables into a compact text block for the LLM prompt."""
    lines: list[str] = []
    for t in tables:
        header = f"TABLE {t.qualified_name}"
        meta_bits = []
        if t.engine:
            meta_bits.append(f"engine={t.engine}")
        if t.total_rows is not None:
            meta_bits.append(f"rows≈{t.total_rows}")
        if meta_bits:
            header += f"  [{', '.join(meta_bits)}]"
        lines.append(header)
        if t.comment:
            lines.append(f"  -- {t.comment}")

        for c in t.columns:
            col_line = f"  {c.name} {c.type}"
            if c.comment:
                col_line += f"  -- {c.comment}"
            lines.append(col_line)

        if t.sample_rows:
            lines.append("  sample rows:")
            for row in t.sample_rows:
                rendered = ", ".join(
                    f"{k}={_format_sample_value(v)}" for k, v in row.items()
                )
                lines.append(f"    {rendered}")
        lines.append("")

    return "\n".join(lines).strip()


def schema_summary(tables: list[Table]) -> str:
    """Short one-line-per-table summary for health checks / UI."""
    return "\n".join(
        f"- {t.qualified_name}: {len(t.columns)} cols"
        + (f", rows≈{t.total_rows}" if t.total_rows is not None else "")
        for t in tables
    )


# --- cached schema context for the agent ---

_SCHEMA_CACHE: dict[str, str] = {}


def get_schema_context(force_refresh: bool = False) -> str:
    """Return the cached schema-context string, introspecting on first use.

    The agent calls this on every report; introspection is comparatively
    expensive, so the result is cached until ``force_refresh=True``.
    """
    if force_refresh or "context" not in _SCHEMA_CACHE:
        tables = introspect(with_samples=True)
        _SCHEMA_CACHE["context"] = build_schema_context(tables)
    return _SCHEMA_CACHE["context"]
