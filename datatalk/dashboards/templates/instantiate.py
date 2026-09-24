"""Turn a report template into a saved dashboard, and add widgets to one.

The output is exactly what the generation pipeline persists -- ``queries``,
``filters`` (definitions plus per-dataset templates) and an authoring document
-- and the first document is produced by :func:`refresh_dashboard` itself, with
the default selections. So a template dashboard's first render and every later
refresh are the same code path, and there is nothing template-specific to drift.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from datatalk.agent.blocks import Document
from datatalk.agent.executor import run_sql
from datatalk.dashboards.refresh import RefreshResult, refresh_dashboard
from datatalk.dashboards.templates.base import (
    FilterSpec,
    ReportTemplate,
    WidgetSpec,
    infer_params,
)

if TYPE_CHECKING:
    from datatalk.context import TenantContext
    from datatalk.memory.store import SavedDashboard

# Same cap as a probed dimension (configure.DIMENSION_OPTIONS_LIMIT).
OPTIONS_LIMIT = 200

_DATASET_ID_RE = re.compile(r"^q(\d+)$")


class TemplateSourceError(LookupError):
    """The named source does not exist for this org, or is the wrong type."""


class SourceNeedsSyncError(RuntimeError):
    """The source's synced tables predate what the template reads."""


def check_store_version(template: ReportTemplate, source: str, ctx: "TenantContext") -> None:
    """Refuse to build on a store that has not been synced since an upgrade.

    Building anyway would save a dashboard whose widgets all fail on missing
    tables, which reads as "the report is broken" rather than "sync first".
    """
    if template.min_store_version is None:
        return
    try:
        rows = run_sql("SELECT schema_version FROM _sync_meta", ctx=ctx, source=source).rows
        version = int(rows[0][0]) if rows and rows[0][0] is not None else 0
    except Exception:  # noqa: BLE001 - never synced: no _sync_meta at all
        version = 0
    if version < template.min_store_version:
        raise SourceNeedsSyncError(source)


@dataclass
class Instantiated:
    queries: list[dict[str, Any]]
    filters: dict[str, Any]
    authoring_document: Document
    template: dict[str, Any]
    title: str
    document: Document = field(default_factory=Document)


def resolve_source(template: ReportTemplate, source: str, ctx: "TenantContext") -> str:
    """The source's name, if the org has it and the template can run on it.

    Wrong type and unknown name are one error: neither should tell the caller
    anything about the org's other sources.
    """
    for ref in ctx.sources:
        if ref.name == source and ref.type in template.source_types:
            return ref.name
    raise TemplateSourceError(source)


def probe_options(spec: FilterSpec, source: str, ctx: "TenantContext") -> dict[str, Any]:
    """Options (and labels/groups) for one control, from the synced tables.

    A failed probe leaves the control with no options -- "All" still works, and
    the allowlist then refuses any specific value, which is the safe direction.
    """
    if spec.kind == "date_range" or not spec.options_sql:
        return {}
    try:
        result = run_sql(spec.options_sql, ctx=ctx, source=source)
        rows = result.rows
    except Exception:  # noqa: BLE001 - a dead probe must not block the dashboard
        rows = []
    values: list[str] = []
    labels: dict[str, str] = {}
    groups: dict[str, str] = {}
    for row in rows[:OPTIONS_LIMIT]:
        if not row or row[0] is None:
            continue
        value = str(row[0])
        if value in labels or value in values:
            continue
        values.append(value)
        if len(row) > 1 and row[1] is not None and str(row[1]) != value:
            labels[value] = str(row[1])
        if len(row) > 2 and row[2] is not None:
            groups[value] = str(row[2])
    out: dict[str, Any] = {
        "options": values,
        "options_truncated": len(rows) > OPTIONS_LIMIT,
    }
    if labels:
        out["option_labels"] = labels
    if groups:
        out["option_groups"] = groups
    return out


def _dataset_entry(widget: WidgetSpec, source: str, dataset_id: str) -> tuple[dict, dict]:
    """(captured-query entry, filter template) for one widget's dataset."""
    query = {
        "dataset_id": dataset_id,
        "source": source,
        "sql": widget.sql,
        "columns": list(widget.columns),
        "row_count": None,
        "widget": widget.key,
    }
    template = {
        "sql": widget.sql,
        "params": infer_params(widget.sql, set(widget.filters)),
        "columns": list(widget.columns),
        "filters": list(widget.filters),
    }
    return query, template


def _blocks(widget: WidgetSpec, dataset_id: str) -> list[dict[str, Any]]:
    return [{**b, "dataset_id": dataset_id} for b in widget.blocks]


def build(template: ReportTemplate, source: str, ctx: "TenantContext") -> Instantiated:
    """Everything a template dashboard persists, before its first execution."""
    source = resolve_source(template, source, ctx)
    check_store_version(template, source, ctx)

    definitions = []
    for spec in template.filters:
        definition = spec.definition()
        definition["source"] = source
        definition.update(probe_options(spec, source, ctx))
        definitions.append(definition)

    queries: list[dict[str, Any]] = []
    templates: dict[str, Any] = {}
    by_widget: dict[str, str] = {}
    by_sql: dict[str, str] = {}
    for widget in template.widgets:
        # Widgets over the same SQL (a table and a chart of one result) share a
        # dataset: one query per refresh, and one set of numbers on screen.
        dataset_id = by_sql.get(widget.sql)
        if dataset_id is None:
            dataset_id = f"q{len(queries) + 1}"
            query, tpl = _dataset_entry(widget, source, dataset_id)
            queries.append(query)
            templates[dataset_id] = tpl
            by_sql[widget.sql] = dataset_id
        by_widget[widget.key] = dataset_id

    blocks: list[dict[str, Any]] = []
    for section in template.layout:
        if section.heading:
            blocks.append({"type": "heading", "text": section.heading, "level": 2})
        children = [
            b
            for key in section.widgets
            for b in _blocks(template.widget(key), by_widget[key])  # type: ignore[arg-type]
        ]
        blocks.append({"type": "row", "children": children})

    return Instantiated(
        queries=queries,
        filters={"version": 1, "filters": definitions, "templates": templates},
        authoring_document=Document.from_dict({"blocks": blocks}),
        template={"id": template.id, "version": template.version, "source": source},
        title=template.title,
    )


def _transient(inst: Instantiated) -> "SavedDashboard":
    from datatalk.memory.store import SavedDashboard

    return SavedDashboard(
        id=0,
        request=inst.title,
        title=inst.title,
        created_at="",
        queries=inst.queries,
        authoring_document=inst.authoring_document,
        filters=inst.filters,
        template=inst.template,
    )


def execute(inst: Instantiated, ctx: "TenantContext") -> RefreshResult:
    """First render: a refresh with default selections, recording row counts."""
    result = refresh_dashboard(_transient(inst), ctx=ctx)
    counts = {d.dataset_id: d.row_count for d in result.datasets}
    for q in inst.queries:
        q["row_count"] = counts.get(q["dataset_id"])
    inst.document = result.document
    return result


# --- editing -----------------------------------------------------------------


def next_dataset_id(queries: list[dict[str, Any]]) -> str:
    taken = [
        int(m.group(1))
        for q in queries
        if (m := _DATASET_ID_RE.match(str(q.get("dataset_id") or "")))
    ]
    return f"q{max(taken, default=0) + 1}"


def add_widget(
    saved: "SavedDashboard", template: ReportTemplate, widget_key: str
) -> tuple[list[dict[str, Any]], dict[str, Any], Document]:
    """``(queries, filters, authoring)`` with one catalog widget appended.

    Always a new dataset, even if an existing one runs the same SQL: the new
    widget's wiring and overrides are then its own to edit.
    """
    widget = template.widget(widget_key)
    if widget is None:
        raise KeyError(widget_key)
    source = (saved.template or {}).get("source")
    dataset_id = next_dataset_id(saved.queries)
    query, tpl = _dataset_entry(widget, source, dataset_id)

    filters = dict(saved.filters or {})
    templates = dict(filters.get("templates") or {})
    templates[dataset_id] = tpl
    filters["templates"] = templates

    doc = saved.authoring_document.to_dict()
    doc.setdefault("blocks", []).append(
        {"type": "row", "children": _blocks(widget, dataset_id)}
    )
    return [*saved.queries, query], filters, Document.from_dict(doc)
