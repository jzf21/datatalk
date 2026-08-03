"""Dashboard generation orchestrator (multi-agent pipeline).

``generate_dashboard()`` reuses Planner → Analyst unchanged, runs the insight
pass (:mod:`datatalk.agent.insight`) over the full captured data, then a
dashboard-authoring agent that emits a *grid* Document (KPI stat-tiles, charts,
tables in rows) steered by those findings. ``materialize()`` fills concrete
values from the datasets the Analyst captured, so no number is ever transcribed
by the model.

``on_event(kind, data)`` mirrors report generation: ``status``/``plan``/``sql``/
``result``/``error`` during planning + the Analyst loop, then ``insights`` with
the structured findings, then ``dashboard`` with the materialized Document.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from datatalk import observability as obs
from datatalk.agent import analyst as analyst_mod
from datatalk.agent import planner as planner_mod
from datatalk.agent.blocks import (
    Document,
    Heading,
    Paragraph,
    RefError,
    count_data_blocks,
    document_to_text,
    materialize,
    parse_json_object,
    validate_references,
)
from datatalk.agent.context_block import (
    build_context_block,
    build_planner_context_block,
)
from datatalk.agent.insight import synthesize_insights
from datatalk.agent.planner import Section, plan_to_text
from datatalk.agent.report import build_memory_block, start_memory_fetch
from datatalk.agent.sqlloop import EventFn, dataset_previews
from datatalk.warehouse.catalog import build_catalog
from datatalk.llm.prompts import (
    DASHBOARD_ANALYST_SYSTEM,
    DASHBOARD_BLOCK_SCHEMA_DOC,
    DASHBOARD_EMPTY_TEMPLATE,
    DASHBOARD_PLANNER_SYSTEM,
    DASHBOARD_REPAIR_TEMPLATE,
    DASHBOARD_SYSTEM,
    ANTI_FABRICATION,
)

if TYPE_CHECKING:
    from datatalk.context import TenantContext


# Soft wall-clock budget for the gather loop. 18 turns against a slow warehouse
# can stretch far past what anyone watches a progress bar for; past this the
# loop finalizes with whatever it has captured, which the author can still use.
GATHER_DEADLINE_S = 300.0


@dataclass
class DashboardResult:
    request: str
    document: Document
    queries: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    # The insight pass's structured findings, verbatim. Persisted with the
    # dashboard so the grid's "lead with"/"do not show" reasoning survives.
    insights: dict[str, Any] = field(default_factory=dict)


def _empty_document(reason: str) -> Document:
    """What a dashboard with no usable data looks like.

    An *explained* empty dashboard beats both a silently-saved blank one and a
    500: the request is already persisted, and the user needs to know the run
    finished and why it is empty.
    """
    return Document(
        blocks=[
            Heading(text="No dashboard could be built", level=2),
            Paragraph(text=reason),
        ]
    )


def _dataset_map(datasets: dict) -> str:
    """``q1 -> [col, col]`` per line, for the repair turn."""
    return "\n".join(
        f"{dataset_id}: {list(result.columns)}"
        for dataset_id, result in datasets.items()
    ) or "(none)"


def author_dashboard(
    request: str,
    sections: list[Section],
    datasets: dict,
    *,
    ctx: "TenantContext",
    sources: dict[str, str] | None = None,
    memory_block: str = "",
    analyst_notes: str = "",
    insights_block: str = "",
    queries: list[dict[str, Any]] | None = None,
    context_block: str | None = None,
    on_event: EventFn | None = None,
) -> tuple[Document, list[RefError], str]:
    """Author a grid Document referencing the captured datasets.

    Returns ``(authoring document, remaining reference errors, finish_reason)``.

    ``analyst_notes`` is the Analyst's closing manifest (which dataset serves
    which widget, which were reconnaissance); ``insights_block`` is the insight
    pass's rendered findings; ``queries`` selects which context-model bodies
    are injected — the ones covering the tables the SQL actually named.
    ``context_block`` lets the orchestrator share one computed block with the
    insight pass; ``None`` computes it here for standalone use.

    The model gets ONE repair turn. Authoring a whole grid as a single JSON
    object is the most structurally demanding call in the pipeline, and its two
    failure modes are both silent: an unparsable reply degrades to zero blocks,
    and a hallucinated column degrades to an italic note. Both are recoverable
    if the model is simply shown what it got wrong, so we show it rather than
    shipping the degradation.
    """

    def emit(kind: str, data: dict[str, Any]) -> None:
        if on_event:
            on_event(kind, data)

    if context_block is None:
        context_block = build_context_block(ctx, queries=queries)
    system = DASHBOARD_SYSTEM.format(
        block_schema=DASHBOARD_BLOCK_SCHEMA_DOC,
        anti_fabrication=ANTI_FABRICATION,
        context_block=context_block,
        memory_block=memory_block,
    )
    user = (
        f"User request:\n{request}\n\n"
        f"Plan:\n{plan_to_text(sections)}\n\n"
        f"Captured datasets (reference these by dataset_id):\n"
        f"{dataset_previews(datasets, sources=sources)}"
    )
    if analyst_notes.strip():
        user += f"\n\nAnalyst notes:\n{analyst_notes.strip()}"
    if insights_block.strip():
        user += f"\n\n{insights_block.strip()}"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    def attempt(label: str) -> tuple[Document, list[RefError], str, str]:
        kwargs: dict[str, Any] = dict(
            model=ctx.author_model,
            messages=messages,
            temperature=0,
        )
        max_tokens = ctx.settings.openai_author_max_tokens
        if max_tokens > 0:
            kwargs["max_tokens"] = max_tokens
        # The label distinguishes the first attempt from the repair turn in
        # metadata rather than in the name: a name that varies per execution
        # detaches every evaluator and dashboard that targets it.
        kwargs.update(obs.llm_kwargs("author-dashboard", {"attempt": label}))
        resp = ctx.author_openai.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        raw = choice.message.content or ""
        doc = Document.from_dict(parse_json_object(raw))
        return doc, validate_references(doc, datasets), raw, (
            getattr(choice, "finish_reason", "") or ""
        )

    with obs.observe(
        "author-dashboard",
        as_type=obs.AGENT,
        input={"request": request, "dataset_ids": list(datasets)},
    ) as span:
        doc, errors, raw, finish_reason = attempt("first")
        if doc.blocks and not errors:
            span.update(
                output=doc.to_dict(),
                metadata={"block_count": len(doc.blocks), "repaired": False},
            )
            return doc, errors, finish_reason

        # One repair turn, on whichever failure we actually saw.
        if not doc.blocks:
            followup = DASHBOARD_EMPTY_TEMPLATE
            if finish_reason == "length":
                # Surfaced as it happens; OPENAI_AUTHOR_MAX_TOKENS is the durable fix.
                emit("status", {"message": "The dashboard reply was cut off; retrying more compactly…"})
            else:
                emit("status", {"message": "Rebuilding the dashboard…"})
        else:
            followup = DASHBOARD_REPAIR_TEMPLATE.format(
                n=len(errors),
                errors="\n".join(f"- {e}" for e in errors),
                dataset_map=_dataset_map(datasets),
            )
            emit("status", {"message": "Repairing dashboard references…"})

        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": followup})

        retry_doc, retry_errors, _, retry_finish = attempt("repair")

        # Keep the better attempt: any blocks beats none, then fewer bad references.
        if not retry_doc.blocks:
            kept: tuple[Document, list[RefError], str] = (doc, errors, finish_reason)
        elif not doc.blocks or len(retry_errors) <= len(errors):
            kept = (retry_doc, retry_errors, retry_finish)
        else:
            kept = (doc, errors, finish_reason)
        span.update(
            output=kept[0].to_dict(),
            metadata={
                "block_count": len(kept[0].blocks),
                "repaired": True,
                "reference_errors": len(kept[1]),
                "finish_reason": kept[2],
            },
        )
        return kept


def generate_dashboard(
    request: str,
    *,
    ctx: "TenantContext",
    memory_suggestions: list[str] | None = None,
    memory_suggestions_fn: Callable[[], list[str]] | None = None,
    on_event: EventFn | None = None,
    max_steps: int = 18,
) -> DashboardResult:
    """Run Planner → Analyst → insight pass → Dashboard-author.

    ``max_steps`` is higher than the report loop's default: a dashboard needs one
    clean dataset per widget (and the planner now aims for 10-16 widgets), a step
    is one assistant *turn* (not one query — the Analyst is told to batch several
    widget queries per turn), and the Analyst is licensed to spend a few early
    turns on reconnaissance. Overrunning is benign here — the loop's
    forced-finalize reply stands in for the manifest, so it costs one wasted call
    and never data.
    """

    def emit(kind: str, data: dict[str, Any]) -> None:
        if on_event:
            on_event(kind, data)

    # Opened here, not at the endpoint: generation runs on a worker thread and
    # OpenTelemetry's active context does not cross one. See generate_report.
    with obs.agent_run(
        "generate-dashboard",
        ctx,
        feature="dashboard",
        input={"request": request},
        metadata={"max_steps": max_steps},
    ) as root:
        return _generate_dashboard(
            request,
            ctx=ctx,
            memory_suggestions=memory_suggestions,
            memory_suggestions_fn=memory_suggestions_fn,
            on_event=on_event,
            max_steps=max_steps,
            emit=emit,
            root=root,
        )


def _generate_dashboard(
    request: str,
    *,
    ctx: "TenantContext",
    memory_suggestions: list[str] | None,
    memory_suggestions_fn: Callable[[], list[str]] | None,
    on_event: EventFn | None,
    max_steps: int,
    emit: Callable[[str, dict[str, Any]], None],
    root: Any,
) -> DashboardResult:
    """The body of :func:`generate_dashboard`, inside its trace.

    Split out only so the pipeline below is not indented under two context
    managers; ``root`` is the trace's root observation, whose output is the
    dashboard a reviewer wants to read first.
    """

    def empty_result(
        reason: str,
        queries: list[dict[str, Any]] | None = None,
        steps: int = 0,
    ) -> DashboardResult:
        """An explained empty dashboard, keeping whatever provenance exists.

        The captured queries are carried through even though no block uses them:
        they are what lets the user see what was actually asked of the warehouse,
        which is the whole diagnosis when a dashboard comes back empty.
        """
        emit("error", {"message": reason})
        document = _empty_document(reason)
        emit("dashboard", {"document": document.to_dict()})
        # An empty dashboard is a real outcome, not a crash — but it is the one
        # a user complains about, so it must be findable: WARNING level plus the
        # reason as the trace output.
        root.update(
            output={"dashboard": reason},
            level="WARNING",
            status_message=reason[:500],
            metadata={"empty": True, "steps": steps},
        )
        return DashboardResult(
            request=request,
            document=document,
            queries=queries or [],
            steps=steps,
        )

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

    emit("status", {"message": "Planning the dashboard…"})
    sections = planner_mod.plan_report(
        request,
        ctx=ctx,
        schema_context=schema_context,
        memory_block=memory_block,
        context_block=build_planner_context_block(ctx, request),
        system_prompt=DASHBOARD_PLANNER_SYSTEM,
    )
    if not sections:
        # An empty plan starves the Analyst of every data_question. The request
        # itself is a serviceable one-widget plan, and beats gathering nothing.
        sections = [
            Section(
                id="overview",
                title=request[:60] or "Overview",
                goal="",
                data_questions=[request],
            )
        ]
    emit("plan", {"sections": [s.to_dict() for s in sections]})

    emit("status", {"message": "Gathering data…"})
    loop = analyst_mod.gather_data(
        request,
        sections,
        ctx=ctx,
        schema_context=schema_context,
        memory_block=memory_block,
        system_prompt=DASHBOARD_ANALYST_SYSTEM,
        plan_label="Dashboard plan (produce one clean dataset per widget)",
        on_event=on_event,
        max_steps=max_steps,
        deadline_s=GATHER_DEADLINE_S,
    )

    if not loop.datasets:
        # Asking the author to build a dashboard from "(no datasets were
        # captured)" reliably yields invented dataset ids that all degrade to
        # notes. Skip the call entirely and say what happened.
        return empty_result(
            "No data was captured, so there is nothing to build a dashboard "
            "from. The queries either failed or returned no rows.",
            steps=loop.steps,
        )

    # Computed once for the two post-loop passes: same ctx, same queries,
    # identical output — and an O(files × queries) regex scan each time.
    context_block = build_context_block(ctx, queries=loop.queries)

    # The insight pass reviews the full captured data (the author sees only
    # previews) so the grid can lead with findings. Best-effort: on failure the
    # author simply gets no insights block.
    emit("status", {"message": "Analyzing the data…"})
    insight = synthesize_insights(
        request,
        sections,
        loop,
        ctx=ctx,
        memory_block=memory_block,
        context_block=context_block,
        on_event=on_event,
    )

    emit("status", {"message": "Building the dashboard…"})
    authoring, errors, finish_reason = author_dashboard(
        request,
        sections,
        loop.datasets,
        ctx=ctx,
        sources=loop.dataset_sources,
        memory_block=memory_block,
        analyst_notes=loop.final_content,
        insights_block=insight.text_block,
        queries=loop.queries,
        context_block=context_block,
        on_event=on_event,
    )

    document = materialize(authoring, loop.datasets)

    if count_data_blocks(document) == 0:
        reason = (
            "The dashboard author did not produce any usable blocks, so no data "
            "could be shown."
        )
        if finish_reason == "length":
            reason += " Its reply was cut off before it finished."
        elif errors:
            reason += f" {len(errors)} block reference(s) did not resolve."
        return empty_result(reason, queries=loop.queries, steps=loop.steps)

    if errors:
        emit(
            "error",
            {"message": f"{len(errors)} dashboard block(s) could not be built."},
        )

    emit("dashboard", {"document": document.to_dict()})

    root.update(
        output={"dashboard": document_to_text(document)},
        metadata={
            "blocks": len(document.blocks),
            "data_blocks": count_data_blocks(document),
            "datasets": len(loop.datasets),
            "steps": loop.steps,
            "reference_errors": len(errors),
            "insights": len(insight.raw.get("insights", []) or []),
        },
    )

    return DashboardResult(
        request=request,
        document=document,
        queries=loop.queries,
        steps=loop.steps,
        insights=insight.raw,
    )
