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

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from datatalk import observability as obs
from datatalk.agent import analyst as analyst_mod
from datatalk.agent import planner as planner_mod
from datatalk.agent import reporter as reporter_mod
from datatalk.agent.blocks import Document, document_to_text, materialize
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


def start_memory_fetch(
    memory_suggestions: list[str] | None,
    memory_suggestions_fn: Callable[[], list[str]] | None,
) -> Callable[[], list[str]]:
    """Kick off memory retrieval so it overlaps schema introspection.

    Retrieval is one OpenAI embeddings round trip plus a Postgres read —
    independent of ``build_catalog``, so paying for them sequentially is pure
    latency. Returns a resolver to call once the catalog is built. Memory is
    best-effort everywhere: a failed fetch resolves to no suggestions.
    """
    if memory_suggestions_fn is None or memory_suggestions is not None:
        fixed = memory_suggestions or []
        return lambda: fixed
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(memory_suggestions_fn)
    pool.shutdown(wait=False)

    def resolve() -> list[str]:
        try:
            return future.result() or []
        except Exception:  # noqa: BLE001 - memory is best-effort
            return []

    return resolve


def generate_report(
    request: str,
    *,
    ctx: "TenantContext",
    memory_suggestions: list[str] | None = None,
    memory_suggestions_fn: Callable[[], list[str]] | None = None,
    on_event: EventFn | None = None,
    max_steps: int = 8,
) -> ReportResult:
    """Run the Planner → Analyst → Reporter pipeline and return a Document.

    ``on_event(kind, data)`` is called as work progresses with kinds:
    ``"status"``, ``"plan"``, ``"sql"``, ``"result"``, ``"error"``, ``"report"``.

    ``memory_suggestions_fn`` defers memory retrieval into the run so the
    embeddings round trip overlaps ``build_catalog`` instead of blocking the
    response before its first byte; a ``memory`` event is emitted when it
    resolves with anything. Passing ``memory_suggestions`` directly wins.

    Every LLM and ClickHouse call resolves through ``ctx``, so a run can only
    reach the data of the org it was started for.

    The whole run is one Langfuse trace, opened here rather than at the
    endpoint: this function is what the streaming endpoints hand to a worker
    thread, and OpenTelemetry's active context does not cross a thread boundary.
    """

    def emit(kind: str, data: dict[str, Any]) -> None:
        if on_event:
            on_event(kind, data)

    with obs.agent_run(
        "generate-report",
        ctx,
        feature="report",
        input={"request": request},
        metadata={"max_steps": max_steps, "use_memory": memory_suggestions_fn is not None},
    ) as root:
        emit("status", {"message": "Loading schema…"})
        resolve_memory = start_memory_fetch(memory_suggestions, memory_suggestions_fn)
        with obs.observe(
            "load-catalog", as_type=obs.RETRIEVER, input={"sources": list(ctx.source_names)}
        ) as span:
            schema_context = build_catalog(ctx)
            span.update(output={"chars": len(schema_context)})
        fetched = resolve_memory()
        if memory_suggestions is None and fetched:
            emit("memory", {"count": len(fetched), "suggestions": fetched})
        memory_block = build_memory_block(fetched)

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
            request,
            sections,
            loop.datasets,
            ctx=ctx,
            sources=loop.dataset_sources,
            memory_block=memory_block,
            queries=loop.queries,
        )

        # Materialize dataset references into concrete values.
        document = materialize(authoring, loop.datasets)
        emit("report", {"document": document.to_dict()})

        # The trace output is what a reviewer reads first, so it is the report
        # itself — flattened to text, the same shape a human sees — not the
        # dataset plumbing, which is already on the observations below.
        root.update(
            output={"report": document_to_text(document)},
            metadata={
                "blocks": len(document.blocks),
                "datasets": len(loop.datasets),
                "steps": loop.steps,
                "sections": len(sections),
            },
        )

        return ReportResult(
            request=request,
            document=document,
            queries=loop.queries,
            steps=loop.steps,
        )
