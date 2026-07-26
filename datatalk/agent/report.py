"""Report generation orchestrator (multi-agent pipeline).

``generate_report()`` runs Planner → Analyst → Reporter and materializes the
Reporter's dataset-referencing Document against the datasets the Analyst
captured, so every chart and table is backed by real query data and no number is
ever transcribed by the model.

The ``on_event(kind, data)`` progress mechanism is preserved; new phase events:
``plan`` (the plan) plus the existing ``status``/``sql``/``result``/``error``
during the Analyst loop, and ``report`` now carries the materialized Document.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from datatalk.agent import analyst as analyst_mod
from datatalk.agent import planner as planner_mod
from datatalk.agent import reporter as reporter_mod
from datatalk.agent.blocks import Document, materialize
from datatalk.agent.sqlloop import EventFn
from datatalk.warehouse.catalog import build_catalog
from datatalk.llm.prompts import MEMORY_BLOCK_TEMPLATE

if TYPE_CHECKING:
    from datatalk.context import TenantContext


@dataclass
class ReportResult:
    request: str
    document: Document
    queries: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0


def build_memory_block(suggestions: list[str] | None) -> str:
    if not suggestions:
        return ""
    joined = "\n".join(f"- {s}" for s in suggestions)
    return MEMORY_BLOCK_TEMPLATE.format(suggestions=joined)


def generate_report(
    request: str,
    *,
    ctx: "TenantContext",
    memory_suggestions: list[str] | None = None,
    on_event: EventFn | None = None,
    max_steps: int = 8,
) -> ReportResult:
    """Run the Planner → Analyst → Reporter pipeline and return a Document.

    ``on_event(kind, data)`` is called as work progresses with kinds:
    ``"status"``, ``"plan"``, ``"sql"``, ``"result"``, ``"error"``, ``"report"``.

    Every LLM and ClickHouse call resolves through ``ctx``, so a run can only
    reach the data of the org it was started for.
    """

    def emit(kind: str, data: dict[str, Any]) -> None:
        if on_event:
            on_event(kind, data)

    emit("status", {"message": "Loading schema…"})
    schema_context = build_catalog(ctx)
    memory_block = build_memory_block(memory_suggestions)

    # 1. Planner
    emit("status", {"message": "Planning the report…"})
    sections = planner_mod.plan_report(
        request,
        ctx=ctx,
        schema_context=schema_context,
        memory_block=memory_block,
    )
    emit("plan", {"sections": [s.to_dict() for s in sections]})

    # 2. Analyst — the only agent that touches the warehouses.
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

    # 3. Reporter — authors a dataset-referencing Document (no numbers typed).
    emit("status", {"message": "Writing the report…"})
    authoring = reporter_mod.write_report(
        request, sections, loop.datasets, ctx=ctx, sources=loop.dataset_sources
    )

    # Materialize dataset references into concrete values.
    document = materialize(authoring, loop.datasets)
    emit("report", {"document": document.to_dict()})

    return ReportResult(
        request=request,
        document=document,
        queries=loop.queries,
        steps=loop.steps,
    )
