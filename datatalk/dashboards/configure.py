"""Configuring a dashboard's filters: rewrite, verify, probe, persist.

The expensive, model-touching half of the feature, run once per configuration
change so that refresh stays deterministic and cheap.

The verification step is the load-bearing one. A proposed template is executed
with its default values and its result columns compared against the captured
query's. Blocks reference columns *by name*, so a rewrite that renamed or dropped
one would turn every widget on that dataset into a degradation note at the next
refresh -- a failure that would look like "the dashboard broke" long after the
configuration that caused it. Rejecting the template instead costs that dataset
its filters and nothing else.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

from datatalk.agent.executor import run_sql
from datatalk.agent.templatize import propose_template
from datatalk.dashboards.filters import coerce_values
from datatalk.warehouse.binding import ParamSpec, TemplateError, bind

if TYPE_CHECKING:
    from datatalk.context import TenantContext
    from datatalk.memory.store import SavedDashboard

# Distinct-value probe for a dimension filter's dropdown.
DIMENSION_OPTIONS_LIMIT = 200

_RUNNING: set[tuple[UUID, int]] = set()
_RUNNING_LOCK = threading.Lock()


def try_acquire(org_id: UUID, dashboard_id: int) -> bool:
    with _RUNNING_LOCK:
        key = (org_id, dashboard_id)
        if key in _RUNNING:
            return False
        _RUNNING.add(key)
        return True


def release(org_id: UUID, dashboard_id: int) -> None:
    with _RUNNING_LOCK:
        _RUNNING.discard((org_id, dashboard_id))


@dataclass
class BindReport:
    """Which datasets a filter reached, and which it could not."""

    wired: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)


def _probe_options(
    defn: dict[str, Any], queries: list[dict[str, Any]], *, ctx: "TenantContext"
) -> tuple[list[str], bool]:
    """Distinct values for a dimension filter's dropdown.

    Runs over the captured query's *output*, which is already row-limited, so
    the result can be incomplete -- hence the truncated flag, which the UI shows
    rather than pretending the list is exhaustive. This only feeds the dropdown
    and the allowlist; "All" is a bound flag, so a short list never silently
    narrows a result.
    """
    column = defn.get("column")
    if not column:
        return [], False
    source = defn.get("source")
    base = next(
        (q for q in queries if q.get("source") == source and q.get("sql")), None
    )
    if base is None:
        return [], False

    dialect = ctx.warehouse(source).dialect
    quoted = dialect.quote_ident(column)
    sql = (
        f"SELECT DISTINCT {quoted} AS v FROM ({base['sql']}) _dt "
        f"WHERE {quoted} IS NOT NULL ORDER BY 1 LIMIT {DIMENSION_OPTIONS_LIMIT}"
    )
    try:
        result = run_sql(sql, ctx=ctx, source=source)
    except Exception:  # noqa: BLE001 - a filter without options is still usable
        return [], False

    values = [str(r[0]) for r in result.rows if r and r[0] is not None]
    return values, len(values) >= DIMENSION_OPTIONS_LIMIT


def _verify(
    proposal: dict[str, Any],
    query: dict[str, Any],
    defs: list[dict[str, Any]],
    *,
    ctx: "TenantContext",
) -> str | None:
    """Execute a proposed template once. Returns a reason to reject, or None."""
    source = query.get("source")
    try:
        dialect = ctx.warehouse(source).dialect
    except Exception as exc:  # noqa: BLE001
        return f"source unavailable: {exc}"

    filters_blob = {"filters": defs}
    try:
        values = coerce_values(filters_blob, {})
        params = [ParamSpec(p["name"], p["type"]) for p in proposal["params"]]
        bound = bind(proposal["sql"], params, values, dialect)
    except (TemplateError, KeyError, TypeError, ValueError) as exc:
        return f"template will not bind: {exc}"

    try:
        result = run_sql(bound.sql, ctx=ctx, source=source, parameters=bound.parameters)
    except Exception as exc:  # noqa: BLE001
        return f"template failed to run: {str(exc)[:200]}"

    expected = list(query.get("columns") or [])
    if expected and list(result.columns) != expected:
        # The check that matters: every block addresses its dataset's columns by
        # name, so a shifted column set breaks widgets rather than the query.
        return (
            f"rewrite changed the result columns: expected {expected}, "
            f"got {list(result.columns)}"
        )
    return None


def configure_filters(
    saved: "SavedDashboard",
    defs: list[dict[str, Any]],
    *,
    ctx: "TenantContext",
) -> tuple[dict[str, Any], BindReport]:
    """Build the ``filters`` blob for a dashboard: definitions plus templates.

    Every dataset is attempted independently. One that cannot be rewritten, or
    whose rewrite fails verification, simply has no template and refreshes
    unfiltered -- reported in the :class:`BindReport` so the UI can say which
    widgets a filter does not reach instead of leaving it to be discovered.
    """
    queries = [q for q in (saved.queries or []) if q.get("dataset_id") and q.get("sql")]

    enriched: list[dict[str, Any]] = []
    for defn in defs:
        defn = dict(defn)
        if defn.get("kind") == "dimension":
            options, truncated = _probe_options(defn, queries, ctx=ctx)
            defn["options"] = options
            defn["options_truncated"] = truncated
            defn.setdefault("default", {"all": True})
        else:
            defn.setdefault("default", {"preset": "last_30_days"})
        enriched.append(defn)

    templates: dict[str, Any] = {}
    report = BindReport()

    for query in queries:
        dataset_id = query["dataset_id"]
        try:
            hint = ctx.warehouse(query.get("source")).dialect.prompt_hint
        except Exception:  # noqa: BLE001
            hint = ""

        proposal = propose_template(query, enriched, ctx=ctx, dialect_hint=hint)
        if not proposal:
            report.skipped.append(
                {"dataset_id": dataset_id, "reason": "no filter could be placed"}
            )
            continue

        problem = _verify(proposal, query, enriched, ctx=ctx)
        if problem:
            report.skipped.append({"dataset_id": dataset_id, "reason": problem})
            continue

        templates[dataset_id] = {
            "sql": proposal["sql"],
            "params": proposal["params"],
            "columns": list(query.get("columns") or []),
            "filters": proposal.get("filters") or [],
        }
        report.wired.append(dataset_id)

    return {"version": 1, "filters": enriched, "templates": templates}, report
