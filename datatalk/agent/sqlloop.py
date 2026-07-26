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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from datatalk.agent.executor import QueryResult, UnsafeSQLError, run_sql
from datatalk.context import UnknownSourceError
from datatalk.warehouse import catalog

if TYPE_CHECKING:
    from datatalk.context import TenantContext

# Progress callback (used by the web layer for NDJSON streaming).
EventFn = Callable[[str, dict[str, Any]], None]

# How many result rows to actually show the model per query (token control).
_ROWS_TO_MODEL = 50

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
            "Read one file from this workspace's context model: curated "
            "documentation of what the business's data means. The available "
            "paths are listed in the WORKSPACE CONTEXT MODEL section of your "
            "prompt. Read the ontology file for an entity before deciding which "
            "table holds it, and the playbook for a question type before "
            "writing SQL for it. These files are authoritative for definitions, "
            "metric formulas and exclusions; the catalog is authoritative for "
            "which columns exist."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "File path exactly as listed, e.g. 'ontology/orders.md' "
                        "or 'playbooks/churn.md'."
                    ),
                },
            },
            "required": ["path"],
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


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _serialize_result(dataset_id: str, source: str, result: QueryResult) -> str:
    """Compact JSON payload of a captured query result for the model.

    Includes ``dataset_id`` so the model knows how to reference this data from
    an authoring block later, and ``source`` so it can tell two similarly
    shaped datasets from different warehouses apart.
    """
    shown = result.rows[:_ROWS_TO_MODEL]
    payload = {
        "dataset_id": dataset_id,
        "source": source,
        "columns": result.columns,
        "rows": [[_json_safe(v) for v in row] for row in shown],
        "row_count": result.row_count,
        "rows_shown": len(shown),
        "truncated": result.truncated or result.row_count > len(shown),
        "sql_executed": result.sql,
    }
    return json.dumps(payload, default=str)


def _describe(
    ctx: "TenantContext", source: str | None, table: str, emit: EventFn
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
    covering = ctx.context_model.covering(ref.name, table)[:2]
    if covering:
        payload["context_files"] = [
            {"path": f.path, "body_md": f.body_md[:4000]} for f in covering
        ]
    return json.dumps(payload)


def _read_context(ctx: "TenantContext", path: str, emit: EventFn) -> str:
    """Handle a ``read_context`` call, as a tool-shaped string either way.

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
    return json.dumps({"path": found.path, "detail": found.body_md})


def run_capture_loop(
    messages: list[dict[str, Any]],
    *,
    ctx: "TenantContext",
    max_steps: int = 8,
    on_event: EventFn | None = None,
    start_index: int = 1,
    model: str | None = None,
    openai: Any | None = None,
) -> LoopResult:
    """Drive the tool-calling loop, capturing each successful query as a dataset.

    ``messages`` is the seeded conversation (system + user, plus any history);
    it is mutated in place. Datasets are keyed ``q{n}`` starting at
    ``start_index``. When the model stops calling tools, its final message text
    is returned in :attr:`LoopResult.final_content`.

    Both the OpenAI client and every warehouse come from ``ctx``, so a loop can
    only ever touch the data of the org it was started for.

    ``model`` and ``openai`` override the defaults together, for the
    documentation agent. They travel as a pair on purpose: with
    ``OPENAI_DOCS_BASE_URL`` pointing at a second provider, overriding the model
    name alone would send it to the wrong endpoint.
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

    for step in range(1, max_steps + 1):
        emit("status", {"message": f"Querying (step {step})…"})
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            temperature=0,
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

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            source = args.get("source") or None

            if tc.function.name == "describe_source":
                tool_content = _describe(ctx, source, args.get("table", ""), emit)
            elif tc.function.name == "read_context":
                tool_content = _read_context(ctx, args.get("path", ""), emit)
            else:
                sql = args.get("sql", "")
                emit("sql", {"sql": sql, "source": source})
                try:
                    result = run_sql(sql, ctx=ctx, source=source)
                    dataset_id = f"q{idx}"
                    idx += 1
                    resolved = ctx.source(source).name
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
                    emit(
                        "result",
                        {
                            "dataset_id": dataset_id,
                            "source": resolved,
                            "row_count": result.row_count,
                            "columns": result.columns,
                        },
                    )
                except UnsafeSQLError as exc:
                    tool_content = json.dumps({"error": f"Rejected: {exc}"})
                    emit("error", {"message": f"Rejected SQL: {exc}"})
                except UnknownSourceError as exc:
                    # The model invented a source name. Hand back the real list
                    # so it can retry rather than failing the whole report.
                    tool_content = json.dumps({"error": str(exc)})
                    emit("error", {"message": str(exc)})
                except Exception as exc:  # noqa: BLE001 - feed DB errors to the model
                    tool_content = json.dumps({"error": str(exc)})
                    emit("error", {"message": str(exc)})

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_content,
                }
            )

    # Ran out of steps: ask for the final answer with what was gathered.
    emit("status", {"message": "Finalizing…"})
    messages.append(
        {
            "role": "user",
            "content": "Stop querying and produce your final answer now with the data you have.",
        }
    )
    resp = client.chat.completions.create(model=model, messages=messages, temperature=0)
    return LoopResult(
        datasets=datasets,
        queries=queries,
        final_content=resp.choices[0].message.content or "",
        steps=max_steps,
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
