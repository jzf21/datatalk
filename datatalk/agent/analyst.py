"""Analyst agent — the only agent that queries ClickHouse during generation.

Runs the shared agentic ``run_sql`` loop to gather data covering every section
of the plan, capturing each successful query as an addressable dataset.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from datatalk import observability as obs
from datatalk.agent.planner import Section, plan_to_text
from datatalk.agent.sqlloop import EventFn, LoopResult, run_capture_loop
from datatalk.llm.prompts import ANALYST_SYSTEM, ANTI_FABRICATION

if TYPE_CHECKING:
    from datatalk.context import TenantContext


def gather_data(
    request: str,
    sections: list[Section],
    *,
    ctx: "TenantContext",
    schema_context: str,
    memory_block: str = "",
    system_prompt: str = ANALYST_SYSTEM,
    plan_label: str = "Report plan (gather data for every section)",
    on_event: EventFn | None = None,
    max_steps: int = 8,
    deadline_s: float | None = None,
) -> LoopResult:
    """Query the org's warehouses to cover the plan; return captured datasets.

    ``system_prompt`` swaps the Analyst's instructions for a deliverable that
    needs a different dataset *shape* — the dashboard passes
    ``DASHBOARD_ANALYST_SYSTEM``, whose KPI tiles read a single row. It must
    carry the same ``{anti_fabrication}``/``{schema_context}``/``{memory_block}``
    slots. The loop itself is untouched, so Q&A and the documentation profiler
    cannot be affected by a change made here.
    """
    system = system_prompt.format(
        anti_fabrication=ANTI_FABRICATION,
        schema_context=schema_context,
        memory_block=memory_block,
    )
    user = (
        f"User request:\n{request}\n\n"
        f"{plan_label}:\n{plan_to_text(sections)}"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    # The Analyst's own `agent` node. Its generations and tool calls nest under
    # this, each tool a sibling of the generation that requested it — which is
    # what makes "which step was slow / which query failed" readable in the tree.
    with obs.observe(
        "gather-data",
        as_type=obs.AGENT,
        input={"request": request, "plan": plan_to_text(sections)},
        metadata={"max_steps": max_steps, "deadline_s": deadline_s},
    ) as span:
        result = run_capture_loop(
            messages,
            ctx=ctx,
            max_steps=max_steps,
            on_event=on_event,
            deadline_s=deadline_s,
            loop_name="analyst",
        )
        span.update(
            output={
                "notes": result.final_content,
                "datasets": [
                    {
                        "dataset_id": q["dataset_id"],
                        "source": q.get("source", ""),
                        "row_count": q.get("row_count"),
                        "columns": q.get("columns"),
                    }
                    for q in result.queries
                ],
            },
            metadata={"steps": result.steps, "dataset_count": len(result.datasets)},
        )
        return result
