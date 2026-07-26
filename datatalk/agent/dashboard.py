"""Dashboard generation orchestrator (multi-agent pipeline).

``generate_dashboard()`` reuses Planner → Analyst unchanged, then runs a
dashboard-authoring agent that emits a *grid* Document (KPI stat-tiles, charts,
tables in rows). ``materialize()`` fills concrete values from the datasets the
Analyst captured, so no number is ever transcribed by the model.

``on_event(kind, data)`` mirrors report generation: ``status``/``plan``/``sql``/
``result``/``error`` during planning + the Analyst loop, then ``dashboard`` with
the materialized Document.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from datatalk.agent import analyst as analyst_mod
from datatalk.agent import planner as planner_mod
from datatalk.agent.blocks import Document, materialize, parse_json_object
from datatalk.agent.context_block import build_context_block
from datatalk.agent.planner import Section, plan_to_text
from datatalk.agent.report import build_memory_block
from datatalk.agent.sqlloop import EventFn, dataset_previews
from datatalk.warehouse.catalog import build_catalog
from datatalk.llm.prompts import (
    DASHBOARD_BLOCK_SCHEMA_DOC,
    DASHBOARD_SYSTEM,
    _ANTI_FABRICATION,
)

if TYPE_CHECKING:
    from datatalk.context import TenantContext


@dataclass
class DashboardResult:
    request: str
    document: Document
    queries: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0


def author_dashboard(
    request: str,
    sections: list[Section],
    datasets: dict,
    *,
    ctx: "TenantContext",
    sources: dict[str, str] | None = None,
) -> Document:
    """Return an *authoring* grid Document referencing the captured datasets."""
    system = DASHBOARD_SYSTEM.format(
        block_schema=DASHBOARD_BLOCK_SCHEMA_DOC,
        anti_fabrication=_ANTI_FABRICATION,
        context_block=build_context_block(ctx),
    )
    user = (
        f"User request:\n{request}\n\n"
        f"Plan:\n{plan_to_text(sections)}\n\n"
        f"Captured datasets (reference these by dataset_id):\n"
        f"{dataset_previews(datasets, sources=sources)}"
    )
    resp = ctx.openai.chat.completions.create(
        model=ctx.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0,
    )
    obj = parse_json_object(resp.choices[0].message.content or "")
    return Document.from_dict(obj)


def generate_dashboard(
    request: str,
    *,
    ctx: "TenantContext",
    memory_suggestions: list[str] | None = None,
    on_event: EventFn | None = None,
    max_steps: int = 8,
) -> DashboardResult:
    """Run Planner → Analyst → Dashboard-author and return a materialized grid."""

    def emit(kind: str, data: dict[str, Any]) -> None:
        if on_event:
            on_event(kind, data)

    emit("status", {"message": "Loading schema…"})
    schema_context = build_catalog(ctx)
    memory_block = build_memory_block(memory_suggestions)

    emit("status", {"message": "Planning the dashboard…"})
    sections = planner_mod.plan_report(
        request,
        ctx=ctx,
        schema_context=schema_context,
        memory_block=memory_block,
    )
    emit("plan", {"sections": [s.to_dict() for s in sections]})

    emit("status", {"message": "Gathering data…"})
    loop = analyst_mod.gather_data(
        request,
        sections,
        ctx=ctx,
        schema_context=schema_context,
        memory_block=memory_block,
        on_event=on_event,
        max_steps=max_steps,
    )

    emit("status", {"message": "Building the dashboard…"})
    authoring = author_dashboard(
        request, sections, loop.datasets, ctx=ctx, sources=loop.dataset_sources
    )

    document = materialize(authoring, loop.datasets)
    emit("dashboard", {"document": document.to_dict()})

    return DashboardResult(
        request=request,
        document=document,
        queries=loop.queries,
        steps=loop.steps,
    )
