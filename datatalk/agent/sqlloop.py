"""Shared agentic ``run_sql`` capture loop.

Both the Analyst (report generation) and the Q&A agent drive the same loop: the
model explores the org's data sources with ``run_sql`` and ``describe_source``,
and every successful query is captured as an addressable **dataset** (``q1``,
``q2``, …) that later blocks can reference. This module owns that loop so the
two agents stay in sync.

An org has several sources, of different engines, so every ``run_sql`` call
names one. A statement cannot span two -- to relate data from two warehouses
the model queries each and the Reporter composes the resulting datasets. Each
captured query records the source it came from, which is what gives a stored
report its provenance.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from datatalk import jsonsafe
from datatalk import observability as obs
from datatalk.agent.executor import QueryResult, UnsafeSQLError, run_sql
from datatalk.context import UnknownSourceError
from datatalk.warehouse import catalog

if TYPE_CHECKING:
    from datatalk.context import TenantContext

# Progress callback (used by the web layer for NDJSON streaming).
EventFn = Callable[[str, dict[str, Any]], None]

# How many result rows to actually show the model per query (token control).
# Public: the insight pass deliberately reads at this same fidelity.
ROWS_TO_MODEL = 50
_ROWS_TO_MODEL = ROWS_TO_MODEL  # deprecated alias; prefer ROWS_TO_MODEL

# History compaction. The loop resends its whole history every turn, so a
# 50-row tool result is paid for on every remaining turn — quadratic in a long
# dashboard run. A result older than _COMPACT_AFTER_TURNS turns has served its
# purpose (the model read it and moved on), so it is rewritten ONCE to its
# first _COMPACT_KEEP_ROWS rows. Once, and never again: OpenAI-compatible
# providers prefix-cache the unchanged head of the conversation, and a message
# that keeps churning would forfeit that on every turn instead of one.
# Blocks reference datasets by id — the model never needs to transcribe old
# rows — so nothing downstream loses data.
_COMPACT_AFTER_TURNS = 2
_COMPACT_MIN_ROWS = 20
_COMPACT_KEEP_ROWS = 5

# How much of a context-model file body to hand back. Bodies are stored up to
# MAX_BODY_CHARS (16k) and the loop resends its whole history every turn, so an
# uncapped body is paid for once per remaining step. `describe_source` attaches
# covering files the model never asked for, so it stays at the tighter cap; an
# explicit `read_context` call is a request for the whole file, so it gets more.
_CONTEXT_BODY_CHARS = 4000
_READ_CONTEXT_BODY_CHARS = 8000

# How many files one read_context call may fetch. Each loop turn is a full API
# round trip that resends the whole history, so reading the ontology file and
# the playbook in ONE call instead of two halves the retrieval overhead.
_READ_CONTEXT_MAX_PATHS = 4


def clip_text(text: str, limit: int) -> str:
    """Clip to ``limit`` chars with a visible marker; a silent cut reads as a
    complete file and the model builds on the missing half."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n\n[… truncated at {limit} of {len(text)} chars]"

_SOURCE_PARAM = {
    "type": "string",
    "description": (
        "Name of the data source to query, exactly as it appears in the "
        "SOURCE headers of the schema catalog."
    ),
}

RUN_SQL_TOOL = {
    "type": "function",
    "function": {
        "name": "run_sql",
        "description": (
            "Execute a single read-only SQL statement "
            "(SELECT/WITH/SHOW/DESCRIBE only) against ONE data source and "
            "return the rows. The statement must use that source's SQL "
            "dialect. You cannot join across sources: to combine data from "
            "two, query each separately and reference both datasets. Each "
            "successful call is captured as a dataset you can reference by id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": _SOURCE_PARAM,
                "sql": {
                    "type": "string",
                    "description": "One read-only SQL statement.",
                },
            },
            "required": ["source", "sql"],
        },
    },
}

DESCRIBE_SOURCE_TOOL = {
    "type": "function",
    "function": {
        "name": "describe_source",
        "description": (
            "Show full detail for one table in one source: column types, "
            "comments, and a few sample rows. The schema catalog lists column "
            "names only, so call this before writing SQL against a table whose "
            "shape or values you are unsure of."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": _SOURCE_PARAM,
                "table": {
                    "type": "string",
                    "description": "Table name, qualified as namespace.table.",
                },
            },
            "required": ["source", "table"],
        },
    },
}

READ_CONTEXT_TOOL = {
    "type": "function",
    "function": {
        "name": "read_context",
        "description": (
            "Read files from this workspace's context model: curated "
            "documentation of what the business's data means. The available "
            "paths are listed in the WORKSPACE CONTEXT MODEL section of your "
            "prompt. Read the ontology file for an entity before deciding which "
            "table holds it, and the playbook for a question type before "
            "writing SQL for it. Batch every file you expect to need into ONE "
            "call (up to 4 paths) on your first step — each separate call "
            "costs a full turn. These files are authoritative for definitions, "
            "metric formulas and exclusions; the catalog is authoritative for "
            "which columns exist."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "File paths exactly as listed, e.g. "
                        "['ontology/orders.md', 'playbooks/churn.md']. "
                        f"Up to {_READ_CONTEXT_MAX_PATHS} per call."
                    ),
                },
            },
            "required": ["paths"],
        },
    },
}

TOOLS = [RUN_SQL_TOOL, DESCRIBE_SOURCE_TOOL]


def tools_for(ctx: "TenantContext") -> list[dict[str, Any]]:
    """The tool set for one org.

    ``read_context`` is offered only when there is something to read: a tool
    whose only possible answer is "no files" is a wasted slot and an invitation
    to burn a step discovering that.
    """
    if ctx.context_model.is_empty:
        return TOOLS
    return [*TOOLS, READ_CONTEXT_TOOL]


@dataclass
class LoopResult:
    datasets: dict[str, QueryResult] = field(default_factory=dict)
    queries: list[dict[str, Any]] = field(default_factory=list)
    final_content: str = ""
    steps: int = 0

    @property
    def dataset_sources(self) -> dict[str, str]:
        """dataset id -> the source it was captured from."""
        return {q["dataset_id"]: q.get("source", "") for q in self.queries}


# One JSON-safety implementation project-wide: the model's tool payloads get
# the same NaN→null / NUL-stripping treatment as the wire and the JSONB
# columns. The old local helper stringified NaN to "nan", which the model then
# read as a value. Types that merely lack an encoding (datetime, Decimal) are
# left for the surrounding dumps(default=str).
_json_safe = jsonsafe.json_safe


def _result_payload(
    dataset_id: str,
    source: str,
    result: QueryResult,
    rows_to_show: int = _ROWS_TO_MODEL,
    note: str | None = None,
) -> dict[str, Any]:
    """The captured query result as the model will see it.

    Kept as a dict rather than going straight to JSON so the trace can record
    the same structure the model got: a pre-serialized string reaches Langfuse
    as an opaque blob, where neither the mask nor the UI's renderer can see
    into it.
    """
    shown = result.rows[:rows_to_show]
    payload: dict[str, Any] = {
        "dataset_id": dataset_id,
        "source": source,
        "columns": result.columns,
        "rows": [[_json_safe(v) for v in row] for row in shown],
        "row_count": result.row_count,
        "rows_shown": len(shown),
        "truncated": result.truncated or result.row_count > len(shown),
        "sql_executed": result.sql,
    }
    if note:
        payload["note"] = note
    return payload


def _serialize_result(
    dataset_id: str,
    source: str,
    result: QueryResult,
    rows_to_show: int = _ROWS_TO_MODEL,
    note: str | None = None,
) -> str:
    """Compact JSON payload of a captured query result for the model.

    Includes ``dataset_id`` so the model knows how to reference this data from
    an authoring block later, and ``source`` so it can tell two similarly
    shaped datasets from different warehouses apart.
    """
    return json.dumps(
        _result_payload(dataset_id, source, result, rows_to_show, note), default=str
    )


def _traced(content: str) -> Any:
    """A tool payload as structure, for an observation's ``output``.

    The tool protocol is strings; a trace wants objects. Falls back to the raw
    string rather than dropping anything -- an unparsable payload is exactly
    the one worth seeing.
    """
    try:
        return json.loads(content)
    except (ValueError, TypeError):
        return content


def _describe(
    ctx: "TenantContext",
    source: str | None,
    table: str,
    emit: EventFn,
    seen_context: dict[str, int] | None = None,
) -> str:
    """Handle a ``describe_source`` call, as a tool-shaped string either way."""
    emit("status", {"message": f"Inspecting {source or 'default'}.{table}…"})
    try:
        ref = ctx.source(source)
    except UnknownSourceError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - a connectionless org is recoverable
        return json.dumps({"error": str(exc)})

    try:
        payload: dict[str, Any] = {"detail": catalog.describe_table(ctx, ref.name, table)}
    except Exception as exc:  # noqa: BLE001 - an unreachable source is recoverable
        return json.dumps({"error": str(exc)})

    # The model already calls describe_source before querying an unfamiliar
    # table, so the ontology file covering it is a free hit: no extra round
    # trip, no extra tool call. Kept under a SEPARATE key and never merged into
    # "detail" -- a curated human claim must not read as an introspected fact.
    # A body already delivered this run (the loop resends its whole history
    # every turn, so it is still in front of the model) collapses to a pointer:
    # describing three tables covered by one ontology file must not pay for
    # that file three times.
    covering = ctx.context_model.covering(ref.name, table)[:2]
    if covering:
        rendered = []
        for f in covering:
            entry = _context_payload(f, _CONTEXT_BODY_CHARS, seen_context)
            entry.pop("summary", None)  # describe already carries the real facts
            rendered.append(entry)
        payload["context_files"] = rendered
    return json.dumps(payload)


def _context_payload(
    found: Any, cap: int, seen_context: dict[str, int] | None
) -> dict[str, Any]:
    """One context file as a tool payload, deduplicated across the run.

    ``seen_context`` maps path -> chars already delivered this run. The loop
    resends its whole history every turn, so a body handed over once is still
    in front of the model; re-sending it is pure token cost. A repeat request
    collapses to a pointer — unless this request's cap would deliver MORE of
    the file than before (an explicit ``read_context`` after a clipped
    ``describe_source`` attachment), which is a real upgrade and goes through.
    """
    would_send = min(len(found.body_md), cap)
    if seen_context is not None:
        if seen_context.get(found.path, 0) >= would_send:
            return {
                "path": found.path,
                "summary": found.summary,
                "note": (
                    "body already provided in an earlier tool result this "
                    "run — re-read it there"
                ),
            }
        seen_context[found.path] = would_send
    return {"path": found.path, "body_md": clip_text(found.body_md, cap)}


def _read_context(
    ctx: "TenantContext",
    path: str,
    emit: EventFn,
    seen_context: dict[str, int] | None = None,
) -> str:
    """Handle a single-path ``read_context`` call, as a tool-shaped string.

    Reads only ``ctx.context_model`` -- a frozen tuple loaded on the request
    thread -- so this is safe on the daemon worker and touches no session.
    """
    emit("status", {"message": f"Reading context {path or '(empty)'}…"})
    model = ctx.context_model
    found = model.get(path or "")
    if found is None:
        # Hand back the real paths so the model self-corrects rather than
        # burning the rest of its steps guessing, exactly as describe_table does.
        known = ", ".join(model.paths[:40]) or "(none)"
        return json.dumps(
            {"error": f"No context file {path!r}. Available files: {known}"}
        )
    entry = _context_payload(found, _READ_CONTEXT_BODY_CHARS, seen_context)
    if "body_md" in entry:
        entry["detail"] = entry.pop("body_md")
    return json.dumps(entry)


def _read_context_batch(
    ctx: "TenantContext",
    paths: list[Any],
    emit: EventFn,
    seen_context: dict[str, int] | None = None,
) -> str:
    """Handle a multi-path ``read_context`` call in one tool result.

    One batched call replaces N sequential turns, and every turn is a full API
    round trip that resends the whole history — this is where the retrieval
    savings actually come from. Per-file misses are reported per file, so one
    typo does not void the files that did resolve.
    """
    named = [str(p) for p in paths if str(p).strip()]
    wanted = named[:_READ_CONTEXT_MAX_PATHS]
    emit("status", {"message": f"Reading context {', '.join(wanted) or '(empty)'}…"})
    model = ctx.context_model
    if not wanted:
        known = ", ".join(model.paths[:40]) or "(none)"
        return json.dumps({"error": f"No paths given. Available files: {known}"})
    files: list[dict[str, Any]] = []
    for p in wanted:
        found = model.get(p)
        if found is None:
            known = ", ".join(model.paths[:40]) or "(none)"
            files.append(
                {"path": p, "error": f"No context file {p!r}. Available files: {known}"}
            )
            continue
        entry = _context_payload(found, _READ_CONTEXT_BODY_CHARS, seen_context)
        if "body_md" in entry:
            entry["detail"] = entry.pop("body_md")
        files.append(entry)
    if len(named) > len(wanted):
        files.append(
            {"error": f"Only the first {_READ_CONTEXT_MAX_PATHS} paths were read."}
        )
    return json.dumps({"files": files})


def run_capture_loop(
    messages: list[dict[str, Any]],
    *,
    ctx: "TenantContext",
    max_steps: int = 8,
    on_event: EventFn | None = None,
    start_index: int = 1,
    model: str | None = None,
    openai: Any | None = None,
    deadline_s: float | None = None,
    error_budget: int = 3,
    loop_name: str = "sqlloop",
) -> LoopResult:
    """Drive the tool-calling loop, capturing each successful query as a dataset.

    ``messages`` is the seeded conversation (system + user, plus any history);
    it is mutated in place. Datasets are keyed ``q{n}`` starting at
    ``start_index``. When the model stops calling tools, its final message text
    is returned in :attr:`LoopResult.final_content`.

    ``deadline_s`` is a soft wall-clock budget: a step that would start past it
    jumps to the same forced-finalize path as running out of steps, so a slow
    warehouse bounds the run instead of stretching it. Data already captured is
    kept either way.

    ``error_budget`` keeps failed turns from starving the run of real work: a
    turn whose ``run_sql`` calls ALL errored is refunded — it burns one unit of
    this budget instead of a step — so a model recovering from a dialect
    misunderstanding still gets its ``max_steps`` of productive turns. Once the
    budget is gone, failed turns consume steps again, so a true failure loop is
    bounded by ``max_steps + error_budget`` turns total.

    Both the OpenAI client and every warehouse come from ``ctx``, so a loop can
    only ever touch the data of the org it was started for.

    ``model`` and ``openai`` override the defaults together, for the
    documentation agent. They travel as a pair on purpose: with
    ``OPENAI_DOCS_BASE_URL`` pointing at a second provider, overriding the model
    name alone would send it to the wrong endpoint.

    ``loop_name`` names this loop's observations in Langfuse (``analyst-step``,
    ``qa-step``, …). It must be a constant per *caller*, never per run: an
    observation name is what evaluators and dashboards target, so a name that
    varies per execution silently detaches every one of them.
    """
    client = openai if openai is not None else ctx.openai
    model = model or ctx.model
    tools = tools_for(ctx)

    def emit(kind: str, data: dict[str, Any]) -> None:
        if on_event:
            on_event(kind, data)

    datasets: dict[str, QueryResult] = {}
    queries: list[dict[str, Any]] = []
    idx = start_index
    started = time.monotonic()
    # ``step`` counts turns against max_steps (all-error turns are refunded);
    # ``turn`` counts every assistant turn, so compaction ages never stall.
    step = 0
    turn = 0
    errors_forgiven = 0
    # Large run_sql tool results eligible for one-time compaction (see the
    # _COMPACT_* constants): {index, turn, dataset_id, source, result}.
    compactable: list[dict[str, Any]] = []
    # path -> chars of that context body already delivered this run. History is
    # resent every turn, so a body sent once stays visible; repeats collapse to
    # a pointer instead of being paid for again (see _context_payload).
    seen_context: dict[str, int] = {}

    while step < max_steps:
        if deadline_s is not None and time.monotonic() - started > deadline_s:
            emit("status", {"message": "Time budget reached…"})
            break
        step += 1
        turn += 1
        # step/max_steps let the UI show an honest turn counter; steps count
        # assistant turns, not queries — a batched turn runs several queries.
        emit(
            "status",
            {
                "message": f"Querying (step {step})…",
                "step": step,
                "max_steps": max_steps,
            },
        )
        if compactable:
            still_fresh = []
            for entry in compactable:
                if turn - entry["turn"] <= _COMPACT_AFTER_TURNS:
                    still_fresh.append(entry)
                    continue
                messages[entry["index"]]["content"] = _serialize_result(
                    entry["dataset_id"],
                    entry["source"],
                    entry["result"],
                    rows_to_show=_COMPACT_KEEP_ROWS,
                    note=(
                        "older rows compacted — the full result is still "
                        f"captured as {entry['dataset_id']} and blocks can "
                        "reference it"
                    ),
                )
            # Dropped from the list, so a message is rewritten exactly once.
            compactable = still_fresh
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            temperature=0,
            **obs.llm_kwargs(f"{loop_name}-step", {"step": step, "max_steps": max_steps}),
        )
        msg = resp.choices[0].message

        if not msg.tool_calls:
            return LoopResult(
                datasets=datasets,
                queries=queries,
                final_content=msg.content or "",
                steps=step,
            )

        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
        )

        parsed: list[tuple[Any, dict[str, Any]]] = []
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            parsed.append((tc, args))

        # The analyst prompt tells the model to batch a whole row of widget
        # queries into one turn, and each statement targets one warehouse
        # through clients built for concurrent use — so a batched turn runs its
        # SQL concurrently. Dataset numbering stays what it always was: ids are
        # assigned after the batch, in tool_calls order, to successes only.
        # describe_source/read_context stay inline: they are cheap (catalog
        # cache, in-memory context model) and share the seen_context state.
        sql_positions = [
            i
            for i, (tc, _) in enumerate(parsed)
            if tc.function.name not in ("describe_source", "read_context")
        ]
        # Tool observations are opened HERE, on the loop thread, where the
        # enclosing agent span is still the active OTel context — the pool
        # threads below would parent them at the trace root instead. The whole
        # batch is submitted at once, so opening them together is also the
        # honest start time. Each is ended once its outcome is known.
        sql_spans: dict[int, Any] = {}
        for i in sql_positions:
            tc, args = parsed[i]
            emit(
                "sql",
                {
                    "query_id": tc.id,
                    "sql": args.get("sql", ""),
                    "source": args.get("source") or None,
                },
            )
            sql_spans[i] = obs.start(
                "run-sql",
                as_type=obs.TOOL,
                input={
                    "source": args.get("source") or None,
                    "sql": args.get("sql", ""),
                },
            )

        def _execute_sql(
            args: dict[str, Any],
        ) -> tuple[QueryResult | None, str, str, str]:
            """(result, resolved source, tool error, event error) for one call."""
            source = args.get("source") or None
            try:
                result = run_sql(args.get("sql", ""), ctx=ctx, source=source)
                return result, ctx.source(source).name, "", ""
            except UnsafeSQLError as exc:
                return None, "", f"Rejected: {exc}", f"Rejected SQL: {exc}"
            except UnknownSourceError as exc:
                # The model invented a source name. Hand back the real list
                # so it can retry rather than failing the whole report.
                return None, "", str(exc), str(exc)
            except Exception as exc:  # noqa: BLE001 - feed DB errors to the model
                return None, "", str(exc), str(exc)

        outcomes: dict[int, tuple[QueryResult | None, str, str, str]] = {}
        if len(sql_positions) > 1:
            with ThreadPoolExecutor(
                max_workers=min(4, len(sql_positions))
            ) as pool:
                futures = {
                    i: pool.submit(_execute_sql, parsed[i][1])
                    for i in sql_positions
                }
                for i, fut in futures.items():
                    outcomes[i] = fut.result()
        elif sql_positions:
            i = sql_positions[0]
            outcomes[i] = _execute_sql(parsed[i][1])

        for i, (tc, args) in enumerate(parsed):
            captured: tuple[str, str, QueryResult] | None = None
            if tc.function.name == "describe_source":
                with obs.observe(
                    "describe-source",
                    as_type=obs.TOOL,
                    input={
                        "source": args.get("source") or None,
                        "table": args.get("table", ""),
                    },
                ) as span:
                    tool_content = _describe(
                        ctx,
                        args.get("source") or None,
                        args.get("table", ""),
                        emit,
                        seen_context,
                    )
                    span.update(output=_traced(tool_content))
            elif tc.function.name == "read_context":
                raw_paths = args.get("paths")
                # `retriever`, not `tool`: this is a lookup into the workspace's
                # curated documentation, and typing it as such is what lets a
                # "did the agent read the right context?" filter exist at all.
                with obs.observe(
                    "read-context",
                    as_type=obs.RETRIEVER,
                    input={"paths": raw_paths if raw_paths is not None else args.get("path", "")},
                ) as span:
                    if isinstance(raw_paths, list):
                        tool_content = _read_context_batch(
                            ctx, raw_paths, emit, seen_context
                        )
                    else:
                        # Single-path forms: a bare string under "paths", or the
                        # older "path" argument some models keep emitting.
                        single = raw_paths if isinstance(raw_paths, str) else ""
                        tool_content = _read_context(
                            ctx, single or args.get("path", ""), emit, seen_context
                        )
                    span.update(output=_traced(tool_content))
            else:
                result, resolved, tool_err, event_err = outcomes[i]
                span = sql_spans.get(i, obs.NULL_SPAN)
                if result is not None:
                    dataset_id = f"q{idx}"
                    idx += 1
                    datasets[dataset_id] = result
                    queries.append(
                        {
                            "dataset_id": dataset_id,
                            "source": resolved,
                            "sql": result.sql,
                            "row_count": result.row_count,
                            "columns": result.columns,
                        }
                    )
                    tool_content = _serialize_result(dataset_id, resolved, result)
                    captured = (dataset_id, resolved, result)
                    # Record what the MODEL was handed, not the whole result
                    # set: that is the context it actually reasoned from, and
                    # the row cap keeps a 5000-row capture out of the trace.
                    traced = _result_payload(dataset_id, resolved, result)
                    if not obs.capture_row_values():
                        traced["rows"] = "<not captured>"
                    span.update(
                        output=traced,
                        metadata={
                            "dataset_id": dataset_id,
                            "source": resolved,
                            "row_count": result.row_count,
                            "truncated": result.truncated,
                        },
                    ).end()
                    emit(
                        "result",
                        {
                            "query_id": tc.id,
                            "dataset_id": dataset_id,
                            "source": resolved,
                            "row_count": result.row_count,
                            "columns": result.columns,
                        },
                    )
                else:
                    tool_content = json.dumps({"error": tool_err})
                    # A rejected or failing query is a first-class outcome here
                    # (the model reads the error and retries), but it should
                    # still surface as ERROR so a run full of them is findable.
                    span.update(
                        output={"error": tool_err},
                        level="ERROR",
                        status_message=tool_err[:500],
                    ).end()
                    emit("error", {"query_id": tc.id, "message": event_err})

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_content,
                }
            )
            if (
                captured is not None
                and len(captured[2].rows[:_ROWS_TO_MODEL]) > _COMPACT_MIN_ROWS
            ):
                compactable.append(
                    {
                        "index": len(messages) - 1,
                        "turn": turn,
                        "dataset_id": captured[0],
                        "source": captured[1],
                        "result": captured[2],
                    }
                )

        # A turn whose run_sql calls ALL failed taught the model something
        # (the errors went back as tool content) but produced nothing; refund
        # the step while the error budget lasts.
        sql_outcomes = [outcomes[i][0] for i in sql_positions]
        if (
            sql_outcomes
            and all(r is None for r in sql_outcomes)
            and errors_forgiven < error_budget
        ):
            errors_forgiven += 1
            step -= 1

    # Ran out of steps (or of wall-clock): ask for the final answer with what
    # was gathered.
    emit("status", {"message": "Finalizing…"})
    messages.append(
        {
            "role": "user",
            "content": "Stop querying and produce your final answer now with the data you have.",
        }
    )
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0,
        **obs.llm_kwargs(f"{loop_name}-finalize", {"steps_used": step}),
    )
    return LoopResult(
        datasets=datasets,
        queries=queries,
        final_content=resp.choices[0].message.content or "",
        steps=step,
    )


def dataset_previews(
    datasets: dict[str, QueryResult],
    sample_rows: int = 5,
    sources: dict[str, str] | None = None,
) -> str:
    """A compact, token-cheap description of captured datasets for the Reporter.

    Shows each dataset's id, source, columns, a few sample rows, and total row
    count — enough for the Reporter to choose columns without re-typing
    numbers, and to say which warehouse a block came from.
    """
    parts: list[str] = []
    for dataset_id, result in datasets.items():
        sample = [[_json_safe(v) for v in row] for row in result.rows[:sample_rows]]
        parts.append(
            json.dumps(
                {
                    "dataset_id": dataset_id,
                    "source": sources.get(dataset_id, "") if sources else "",
                    "sql": result.sql,
                    "columns": result.columns,
                    "sample_rows": sample,
                    "row_count": result.row_count,
                },
                default=str,
            )
        )
    return "\n".join(parts) if parts else "(no datasets were captured)"
