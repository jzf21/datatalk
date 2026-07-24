"""Analyst agent — the only agent that queries ClickHouse during generation.

Runs the shared agentic ``run_sql`` loop to gather data covering every section
of the plan, capturing each successful query as an addressable dataset.
"""

from __future__ import annotations

from datatalk.agent.planner import Section, plan_to_text
from datatalk.agent.sqlloop import EventFn, LoopResult, run_capture_loop
from datatalk.config import Settings, get_settings
from datatalk.llm.client import get_openai
from datatalk.llm.prompts import ANALYST_SYSTEM, _ANTI_FABRICATION


def gather_data(
    request: str,
    sections: list[Section],
    *,
    schema_context: str,
    memory_block: str = "",
    on_event: EventFn | None = None,
    max_steps: int = 8,
    settings: Settings | None = None,
) -> LoopResult:
    """Query ClickHouse to cover the plan; return captured datasets + history."""
    settings = settings or get_settings()
    client = get_openai()
    system = ANALYST_SYSTEM.format(
        anti_fabrication=_ANTI_FABRICATION,
        schema_context=schema_context,
        memory_block=memory_block,
    )
    user = (
        f"User request:\n{request}\n\n"
        f"Report plan (gather data for every section):\n{plan_to_text(sections)}"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    return run_capture_loop(
        client,
        settings.openai_model,
        messages,
        max_steps=max_steps,
        on_event=on_event,
        settings=settings,
    )
