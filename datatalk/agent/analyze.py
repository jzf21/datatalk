"""Report analysis (Milestone 4).

Analyzes a report — either one DataTalk generated earlier, or an external report
the user pastes/uploads — and returns a Markdown critique/summary.
"""

from __future__ import annotations

from datatalk.agent.blocks import Document, document_to_text
from datatalk.agent.report import build_memory_block
from datatalk.config import Settings, get_settings
from datatalk.llm.client import get_openai
from datatalk.llm.prompts import ANALYZE_SYSTEM, DASHBOARD_ANALYZE_SYSTEM


def analyze_report(
    report_text: str,
    *,
    source: str = "external",
    focus: str | None = None,
    memory_suggestions: list[str] | None = None,
    settings: Settings | None = None,
) -> str:
    """Analyze ``report_text`` and return a Markdown analysis.

    ``source`` is "own" (a DataTalk-generated report) or "external" (provided by
    the user); it only shapes the framing. ``focus`` optionally narrows what the
    analysis should concentrate on.
    """
    settings = settings or get_settings()
    if not (report_text or "").strip():
        raise ValueError("No report text provided to analyze.")

    client = get_openai()
    system = ANALYZE_SYSTEM.format(memory_block=build_memory_block(memory_suggestions))

    origin = (
        "This report was generated earlier by DataTalk from the database."
        if source == "own"
        else "This is an external report provided by the user."
    )
    focus_line = f"\n\nFocus especially on: {focus}" if focus else ""
    user_content = f"{origin}{focus_line}\n\n=== REPORT ===\n{report_text}"

    resp = client.chat.completions.create(
        model=settings.openai_model,
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
    focus: str | None = None,
    memory_suggestions: list[str] | None = None,
    settings: Settings | None = None,
) -> str:
    """Analyze a materialized dashboard and return Markdown findings.

    Reads only the numbers already on the dashboard (flattened via
    ``document_to_text``); runs no SQL. Returns even when the dashboard is empty.
    """
    settings = settings or get_settings()
    text = document_to_text(document)
    client = get_openai()
    system = DASHBOARD_ANALYZE_SYSTEM.format(
        memory_block=build_memory_block(memory_suggestions)
    )
    focus_line = f"\n\nFocus especially on: {focus}" if focus else ""
    user_content = f"{focus_line}\n\n=== DASHBOARD ===\n{text}"
    resp = client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
    )
    return resp.choices[0].message.content or ""
