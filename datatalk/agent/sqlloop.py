"""Shared agentic ``run_sql`` capture loop.

Both the Analyst (report generation) and the Q&A agent drive the same loop: the
model calls the ``run_sql`` tool to explore ClickHouse, and every successful
query is captured as an addressable **dataset** (``q1``, ``q2``, …) that later
blocks can reference. This module owns that loop so the two agents stay in sync.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from datatalk.agent.executor import QueryResult, UnsafeSQLError, run_sql

if TYPE_CHECKING:
    from datatalk.context import TenantContext

# Progress callback (used by the web layer for NDJSON streaming).
EventFn = Callable[[str, dict[str, Any]], None]

# How many result rows to actually show the model per query (token control).
_ROWS_TO_MODEL = 50

RUN_SQL_TOOL = {
    "type": "function",
    "function": {
        "name": "run_sql",
        "description": (
            "Execute a single read-only ClickHouse SQL statement "
            "(SELECT/WITH/SHOW/DESCRIBE only) and return the rows. Each "
            "successful call is captured as a dataset you can reference by id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "One read-only ClickHouse SQL statement.",
                }
            },
            "required": ["sql"],
        },
    },
}


@dataclass
class LoopResult:
    datasets: dict[str, QueryResult] = field(default_factory=dict)
    queries: list[dict[str, Any]] = field(default_factory=list)
    final_content: str = ""
    steps: int = 0


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _serialize_result(dataset_id: str, result: QueryResult) -> str:
    """Compact JSON payload of a captured query result for the model.

    Includes ``dataset_id`` so the model knows how to reference this data from
    an authoring block later.
    """
    shown = result.rows[:_ROWS_TO_MODEL]
    payload = {
        "dataset_id": dataset_id,
        "columns": result.columns,
        "rows": [[_json_safe(v) for v in row] for row in shown],
        "row_count": result.row_count,
        "rows_shown": len(shown),
        "truncated": result.truncated or result.row_count > len(shown),
        "sql_executed": result.sql,
    }
    return json.dumps(payload, default=str)


def run_capture_loop(
    messages: list[dict[str, Any]],
    *,
    ctx: "TenantContext",
    max_steps: int = 8,
    on_event: EventFn | None = None,
    start_index: int = 1,
) -> LoopResult:
    """Drive the tool-calling loop, capturing each successful query as a dataset.

    ``messages`` is the seeded conversation (system + user, plus any history);
    it is mutated in place. Datasets are keyed ``q{n}`` starting at
    ``start_index``. When the model stops calling tools, its final message text
    is returned in :attr:`LoopResult.final_content`.

    Both the OpenAI client and the ClickHouse client come from ``ctx``, so a
    loop can only ever touch the data of the org it was started for.
    """
    openai = ctx.openai
    model = ctx.model

    def emit(kind: str, data: dict[str, Any]) -> None:
        if on_event:
            on_event(kind, data)

    datasets: dict[str, QueryResult] = {}
    queries: list[dict[str, Any]] = []
    idx = start_index

    for step in range(1, max_steps + 1):
        emit("status", {"message": f"Querying (step {step})…"})
        resp = openai.chat.completions.create(
            model=model,
            messages=messages,
            tools=[RUN_SQL_TOOL],
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
                sql = args.get("sql", "")
            except json.JSONDecodeError:
                sql = ""

            emit("sql", {"sql": sql})
            try:
                result = run_sql(sql, ctx=ctx)
                dataset_id = f"q{idx}"
                idx += 1
                datasets[dataset_id] = result
                queries.append(
                    {
                        "dataset_id": dataset_id,
                        "sql": result.sql,
                        "row_count": result.row_count,
                        "columns": result.columns,
                    }
                )
                tool_content = _serialize_result(dataset_id, result)
                emit(
                    "result",
                    {
                        "dataset_id": dataset_id,
                        "row_count": result.row_count,
                        "columns": result.columns,
                    },
                )
            except UnsafeSQLError as exc:
                tool_content = json.dumps({"error": f"Rejected: {exc}"})
                emit("error", {"message": f"Rejected SQL: {exc}"})
            except Exception as exc:  # noqa: BLE001 - feed DB errors back to the model
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
    resp = openai.chat.completions.create(model=model, messages=messages, temperature=0)
    return LoopResult(
        datasets=datasets,
        queries=queries,
        final_content=resp.choices[0].message.content or "",
        steps=max_steps,
    )


def dataset_previews(datasets: dict[str, QueryResult], sample_rows: int = 5) -> str:
    """A compact, token-cheap description of captured datasets for the Reporter.

    Shows each dataset's id, columns, a few sample rows, and total row count —
    enough for the Reporter to choose columns without re-typing numbers.
    """
    parts: list[str] = []
    for dataset_id, result in datasets.items():
        sample = [[_json_safe(v) for v in row] for row in result.rows[:sample_rows]]
        parts.append(
            json.dumps(
                {
                    "dataset_id": dataset_id,
                    "sql": result.sql,
                    "columns": result.columns,
                    "sample_rows": sample,
                    "row_count": result.row_count,
                },
                default=str,
            )
        )
    return "\n".join(parts) if parts else "(no datasets were captured)"
