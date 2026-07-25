"""Report analysis (Milestone 4).

Analyzes a report — either one DataTalk generated earlier, or an external report
the user pastes/uploads — and returns a Markdown critique/summary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from datatalk.agent.blocks import Document, document_to_text
from datatalk.agent.report import build_memory_block
from datatalk.llm.prompts import ANALYZE_SYSTEM, DASHBOARD_ANALYZE_SYSTEM

if TYPE_CHECKING:
    from datatalk.context import TenantContext


def analyze_report(
    report_text: str,
    *,
    ctx: "TenantContext",
    source: str = "external",
    focus: str | None = None,
    memory_suggestions: list[str] | None = None,
) -> str:
    """Analyze ``report_text`` and return a Markdown analysis.

    ``source`` is "own" (a DataTalk-generated report) or "external" (provided by
    the user); it only shapes the framing. ``focus`` optionally narrows what the
    analysis should concentrate on.
    """
    if not (report_text or "").strip():
        raise ValueError("No report text provided to analyze.")

    system = ANALYZE_SYSTEM.format(memory_block=build_memory_block(memory_suggestions))

    origin = (
        "This report was generated earlier by DataTalk from the database."
        if source == "own"
        else "This is an external report provided by the user."
    )
    focus_line = f"\n\nFocus especially on: {focus}" if focus else ""
    user_content = f"{origin}{focus_line}\n\n=== REPORT ===\n{report_text}"

    resp = ctx.openai.chat.completions.create(
        model=ctx.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
    )
    return resp.choices[0].message.content or ""


def analyze_dashboard(
    document: Document,
    *,
    ctx: "TenantContext",
    focus: str | None = None,
    memory_suggestions: list[str] | None = None,
) -> str:
    """Analyze a materialized dashboard and return Markdown findings.

    Reads only the numbers already on the dashboard (flattened via
    ``document_to_text``); runs no SQL. Returns even when the dashboard is empty.
    """
    text = document_to_text(document)
    system = DASHBOARD_ANALYZE_SYSTEM.format(
        memory_block=build_memory_block(memory_suggestions)
    )
    focus_line = f"\n\nFocus especially on: {focus}" if focus else ""
    user_content = f"{focus_line}\n\n=== DASHBOARD ===\n{text}"
    resp = ctx.openai.chat.completions.create(
        model=ctx.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
    )
    return resp.choices[0].message.content or ""
