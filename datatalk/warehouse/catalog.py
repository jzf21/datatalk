"""Schema discovery across all of an org's sources.

Nothing about a tenant's schema is hardcoded: each adapter reads its own
catalog tables and this module turns the result into text the LLM can act on.

Two renderings, deliberately different in cost:

* :func:`build_catalog` -- every source, every table, column *names* only. This
  goes into every Planner and Analyst prompt, so it has to stay small enough
  that adding a fourth warehouse does not blow the context window.
* :func:`describe_table` -- one table in full: types, comments, sample rows.
  Fetched on demand through the ``describe_source`` tool when the model has
  decided which table it actually needs.

Sources are introspected concurrently and independently: with several
warehouses, doing it serially would dominate report latency, and one dead
credential must not take down every report. A source that fails renders as
UNAVAILABLE and the rest of the catalog is still useful -- the same
degrade-never-raise rule the block layer follows.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from datatalk.warehouse.base import Table

if TYPE_CHECKING:  # avoid a circular import: context -> clients -> warehouse
    from datatalk.context import SourceRef, TenantContext

# Introspecting N warehouses at once. Bounded: each worker holds a warehouse
# connection, and an org with twenty sources should not open twenty at once.
_MAX_PARALLEL_SOURCES = 4

_SCHEMA_TTL = 3600.0


def _format_sample_value(value: Any, max_len: int = 60) -> str:
    text = "NULL" if value is None else str(value)
    text = text.replace("\n", " ")
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def render_table_summary(tables: list[Table]) -> str:
    """Column names only -- the catalog line for each table."""
    lines: list[str] = []
    for t in tables:
        cols = ", ".join(c.name for c in t.columns)
        line = f"  {t.qualified_name} ({cols})"
        if t.total_rows is not None:
            line += f"  rows≈{t.total_rows}"
        if t.comment:
            line += f"  -- {t.comment}"
        lines.append(line)
    return "\n".join(lines)


def render_table_detail(tables: list[Table]) -> str:
    """Full detail: types, comments, sample rows. Used by ``describe_source``."""
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


# --- per-source cache ---------------------------------------------------------
#
# Keyed by (org_id, source fingerprint). The org id is redundant with the
# fingerprint for isolation purposes (identical credentials produce identical
# schemas), but including it makes invalidate_schema(org_id) trivial and makes
# "is this cross-tenant safe?" answerable without reasoning about hash
# collisions.


@dataclass
class _CachedTables:
    tables: list[Table]
    built_at: float


_SCHEMA_CACHE: dict[tuple[Any, str], _CachedTables] = {}
_SCHEMA_LOCK = threading.Lock()


def source_tables(
    ctx: "TenantContext", ref: "SourceRef", *, force_refresh: bool = False
) -> list[Table]:
    """Introspect one source, with sample rows, cached for ``_SCHEMA_TTL``."""
    key = (ctx.org_id, ref.fingerprint)

    if not force_refresh:
        with _SCHEMA_LOCK:
            hit = _SCHEMA_CACHE.get(key)
        if hit is not None and (time.monotonic() - hit.built_at) < _SCHEMA_TTL:
            return hit.tables

    # Deliberately outside the lock: this is a multi-second round trip, and
    # holding the lock would serialize every source behind the slowest.
    tables = ctx.warehouse(ref.name).introspect(with_samples=True)

    with _SCHEMA_LOCK:
        _SCHEMA_CACHE[key] = _CachedTables(tables, time.monotonic())
    return tables


def invalidate_schema(org_id: Any, fingerprint: str | None = None) -> None:
    """Drop cached schema for an org (all sources unless one fingerprint given).

    Called when an org adds, edits or deletes a source.
    """
    with _SCHEMA_LOCK:
        stale = [
            k
            for k in _SCHEMA_CACHE
            if k[0] == org_id and (fingerprint is None or k[1] == fingerprint)
        ]
        for k in stale:
            _SCHEMA_CACHE.pop(k, None)


# --- the catalog --------------------------------------------------------------


def _source_block(ctx: "TenantContext", ref: "SourceRef", force_refresh: bool) -> str:
    # The name is quoted because it is the single thing models most reliably
    # get wrong here: it is a run_sql argument, not an identifier, and unquoted
    # beside `database.table` lines it reads like one more schema name.
    header = f'SOURCE "{ref.name}" [{_label(ref.type)}]'
    if ref.description:
        header += f" — {ref.description}"

    try:
        tables = source_tables(ctx, ref, force_refresh=force_refresh)
    except Exception as exc:  # noqa: BLE001 - one bad source must not kill the rest
        return f"{header}\n  UNAVAILABLE: {exc}"

    if not tables:
        return f"{header}\n  (no tables visible)"

    lines = [header]
    scope = _scope_note(ref, tables)
    if scope:
        lines.append(scope)
    lines.append(render_table_summary(tables))
    return "\n".join(lines)


def _label(type_: str) -> str:
    from datatalk.warehouse import dialect_for

    try:
        return dialect_for(type_).label or type_
    except Exception:  # noqa: BLE001 - an unknown type still gets a header
        return type_


def _scope_note(ref: "SourceRef", tables: list[Table]) -> str:
    """One line naming the scope, when this workspace has narrowed the source.

    Without it a model that has seen a table name elsewhere -- in the context
    model, in a past report, in a user's question -- keeps trying to reach a
    database this source deliberately does not expose, and reads the resulting
    error as a transient failure worth retrying.
    """
    if not ref.spec.introspect_databases:
        return ""

    shown = sorted({t.database for t in tables})
    if not shown:
        return ""
    noun = "schema" if ref.type in ("postgres", "jira") else "database"
    if len(shown) > 1:
        noun += "s"
    return (
        f"  (this workspace scopes this source to the {noun} "
        f"{', '.join(shown)}; nothing else on this server is reachable here, "
        f"and the tables listed below are all of it)"
    )


def build_catalog(ctx: "TenantContext", *, force_refresh: bool = False) -> str:
    """Every source and its tables, column names only.

    This is what the Planner and Analyst see. It ends with the SQL rules that
    depend on the sources actually present -- one dialect hint per engine in
    use, plus the constraint the model most needs to hear.

    When the org has a context model, its *tree* is prepended in a separate
    fence -- paths and one-line summaries, never a body. Curated meaning and
    introspected fact must not read as the same kind of evidence, and keeping
    bodies out is what stops the documentation recreating the context pressure
    it exists to relieve.
    """
    if not ctx.sources:
        return "(no data sources are configured for this workspace)"

    refs = list(ctx.sources)
    if len(refs) == 1:
        blocks = [_source_block(ctx, refs[0], force_refresh)]
    else:
        with ThreadPoolExecutor(
            max_workers=min(_MAX_PARALLEL_SOURCES, len(refs)),
            thread_name_prefix="introspect",
        ) as pool:
            blocks = list(
                pool.map(lambda r: _source_block(ctx, r, force_refresh), refs)
            )

    tree = ctx.context_model.render_tree()  # "" when the org has none
    parts = ([tree] if tree else []) + [*blocks, "", _sql_rules(ctx)]
    return "\n\n".join(parts).strip()


def _sql_rules(ctx: "TenantContext") -> str:
    """The dialect hints and cross-source rule, built from the sources present."""
    from datatalk.warehouse import dialect_for

    lines = [
        "SQL RULES",
        '- Every run_sql call must name a source in its "source" argument.',
        # The failure this prevents: the model reads the SOURCE header as a
        # schema name and writes `FROM analytics.events` against a source called
        # analytics whose tables actually live in a database of another name.
        "- A source name is a connection handle, NOT a database or schema name. "
        "Never write it inside SQL. Every table below is already written "
        "exactly as it must appear in a query against that source.",
    ]
    if len(ctx.sources) > 1:
        lines.append(
            "- One statement queries ONE source. You cannot join across "
            "sources: run a separate query against each and reference both "
            "captured datasets."
        )
    lines.append(
        "- Call describe_source before writing SQL against a table you have "
        "not inspected -- the catalog lists column names only."
    )
    if not ctx.context_model.is_empty:
        lines.append(
            "- Call read_context on the relevant ontology or playbook file "
            "before writing SQL for a metric or entity it covers -- this "
            "workspace's own definitions override your assumptions about what "
            "a column means."
        )

    # One line per distinct *dialect hint*, not per type: a synced Jira source
    # speaks PostgreSQL and shares its hint, which must not print twice. Type
    # notes follow the dialect hints they depend on.
    hints: list[str] = []
    notes: list[str] = []
    for ref in ctx.sources:
        try:
            dialect = dialect_for(ref.type)
        except Exception:  # noqa: BLE001 - an unknown type is not fatal here
            continue
        if dialect.prompt_hint and dialect.prompt_hint not in hints:
            hints.append(dialect.prompt_hint)
        if dialect.notes and dialect.notes not in notes:
            notes.append(dialect.notes)
    lines.extend(f"- {h}" for h in hints + notes)
    return "\n".join(lines)


def describe_table(ctx: "TenantContext", source: str | None, table: str) -> str:
    """Full detail for one table in one source.

    ``table`` may be ``database.table`` or a bare name; a bare name matches on
    any namespace, since the model often has only what the catalog line showed
    it. Raises :class:`~datatalk.context.UnknownSourceError` for a bad source
    name -- the tool loop turns that into a recoverable tool error.
    """
    ref = ctx.source(source)
    tables = source_tables(ctx, ref)

    wanted = table.strip().strip('`"')
    matches = [
        t
        for t in tables
        if t.qualified_name.lower() == wanted.lower() or t.name.lower() == wanted.lower()
    ]
    if not matches:
        known = ", ".join(t.qualified_name for t in tables[:40]) or "(none)"
        return f"No table {table!r} in source {ref.name!r}. Tables: {known}"
    return render_table_detail(matches)
