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
from typing import Any

from datatalk.agent import analyst as analyst_mod
from datatalk.agent import planner as planner_mod
from datatalk.agent import reporter as reporter_mod
from datatalk.agent.blocks import Document, materialize
from datatalk.agent.sqlloop import EventFn
from datatalk.config import Settings, get_settings
from datatalk.db.introspect import get_schema_context
from datatalk.llm.prompts import MEMORY_BLOCK_TEMPLATE


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
    memory_suggestions: list[str] | None = None,
    on_event: EventFn | None = None,
    max_steps: int = 8,
    settings: Settings | None = None,
) -> ReportResult:
    """Run the Planner → Analyst → Reporter pipeline and return a Document.

    ``on_event(kind, data)`` is called as work progresses with kinds:
    ``"status"``, ``"plan"``, ``"sql"``, ``"result"``, ``"error"``, ``"report"``.
    """
    settings = settings or get_settings()

    def emit(kind: str, data: dict[str, Any]) -> None:
        if on_event:
            on_event(kind, data)

    emit("status", {"message": "Loading schema…"})
    schema_context = get_schema_context()
    memory_block = build_memory_block(memory_suggestions)

    # 1. Planner
    emit("status", {"message": "Planning the report…"})
    sections = planner_mod.plan_report(
        request,
        schema_context=schema_context,
        memory_block=memory_block,
        settings=settings,
    )
    emit("plan", {"sections": [s.to_dict() for s in sections]})

    # 2. Analyst — the only agent that touches ClickHouse.
    emit("status", {"message": "Gathering data…"})
    loop = analyst_mod.gather_data(
        request,
        sections,
        schema_context=schema_context,
        memory_block=memory_block,
        on_event=on_event,
        max_steps=max_steps,
        settings=settings,
    )

    # 3. Reporter — authors a dataset-referencing Document (no numbers typed).
    emit("status", {"message": "Writing the report…"})
    authoring = reporter_mod.write_report(
        request, sections, loop.datasets, settings=settings
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
