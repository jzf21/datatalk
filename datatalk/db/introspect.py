"""Runtime schema discovery.

Nothing about the target schema is hardcoded. We read ClickHouse's own
``system`` tables plus a few sample rows and build a compact text description
("schema context") that the LLM uses to write SQL.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from clickhouse_connect.driver.client import Client

from datatalk.config import Settings

if TYPE_CHECKING:  # avoid a circular import: context -> clients -> db.clickhouse
    from datatalk.context import TenantContext


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


def list_databases(client: Client, settings: Settings) -> list[str]:
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
    client: Client,
    settings: Settings,
    *,
    with_samples: bool = True,
) -> list[Table]:
    """Discover all user tables with columns and (optionally) sample rows."""
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
#
# Keyed by (org_id, connection fingerprint). This used to be a dict with a
# single hardcoded "context" key, which under multi-tenancy leaks one org's
# table names, column names AND sample row values into another org's prompts.
#
# The org id is redundant with the fingerprint for isolation purposes (identical
# credentials produce identical schemas), but including it makes
# invalidate_schema(org_id) trivial and makes "is this cross-tenant safe?"
# answerable without reasoning about hash collisions.


@dataclass
class _CachedSchema:
    context: str
    built_at: float


_SCHEMA_CACHE: dict[tuple[Any, str], _CachedSchema] = {}
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_TTL = 3600.0


def get_schema_context(ctx: "TenantContext", *, force_refresh: bool = False) -> str:
    """Return this org's schema-context string, introspecting on a miss.

    The agent calls this on every report and introspection is comparatively
    expensive, so results are cached per org for ``_SCHEMA_TTL`` seconds.
    """
    key = (ctx.org_id, ctx.fingerprint)

    if not force_refresh:
        with _SCHEMA_LOCK:
            hit = _SCHEMA_CACHE.get(key)
        if hit is not None and (time.monotonic() - hit.built_at) < _SCHEMA_TTL:
            return hit.context

    # Deliberately outside the lock: this is a multi-second ClickHouse round
    # trip, and holding the lock would serialize every org behind the slowest.
    tables = introspect(ctx.clickhouse, ctx.settings, with_samples=True)
    context = build_schema_context(tables)

    with _SCHEMA_LOCK:
        _SCHEMA_CACHE[key] = _CachedSchema(context, time.monotonic())
    return context


def invalidate_schema(org_id: Any, fingerprint: str | None = None) -> None:
    """Drop cached schema for an org (all fingerprints unless one is given).

    Called when an org edits its ClickHouse connection.
    """
    with _SCHEMA_LOCK:
        stale = [
            k
            for k in _SCHEMA_CACHE
            if k[0] == org_id and (fingerprint is None or k[1] == fingerprint)
        ]
        for k in stale:
            _SCHEMA_CACHE.pop(k, None)
