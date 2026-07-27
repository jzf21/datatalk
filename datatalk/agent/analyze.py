"""Report analysis (Milestone 4).

Analyzes a report — either one DataTalk generated earlier, or an external report
the user pastes/uploads — and returns a Markdown critique/summary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from datatalk.agent.blocks import Document, document_to_text
from datatalk.agent.context_block import build_context_block
from datatalk.agent.report import build_memory_block
from datatalk.agent.sqlloop import clip_text
from datatalk.llm.prompts import ANALYZE_SYSTEM, DASHBOARD_ANALYZE_SYSTEM

if TYPE_CHECKING:
    from datatalk.context import TenantContext

# A report or flattened dashboard goes into the prompt verbatim, and nothing
# upstream bounds it — an external paste can be arbitrarily large, and a
# dashboard's tables can hold thousands of rows. ~6k tokens of input is more
# than a critique needs; the clip marker tells the model the cut happened.
_MAX_INPUT_CHARS = 24_000


def _completion_kwargs(ctx: "TenantContext") -> dict[str, Any]:
    kwargs: dict[str, Any] = {"model": ctx.model, "temperature": 0.2}
    max_tokens = ctx.settings.openai_analyze_max_tokens
    if max_tokens > 0:
        kwargs["max_tokens"] = max_tokens
    return kwargs


def analyze_report(
    report_text: str,
    *,
    ctx: "TenantContext",
    source: str = "external",
    focus: str | None = None,
    memory_suggestions: list[str] | None = None,
    queries: list[dict[str, Any]] | None = None,
) -> str:
    """Analyze ``report_text`` and return a Markdown analysis.

    ``source`` is "own" (a DataTalk-generated report) or "external" (provided by
    the user); it only shapes the framing. ``focus`` optionally narrows what the
    analysis should concentrate on. ``queries`` — the saved report's captured
    queries, when analyzing our own — selects which context-model bodies inform
    the critique; a reviewer without the workspace's definitions judges numbers
    it does not understand.
    """
    if not (report_text or "").strip():
        raise ValueError("No report text provided to analyze.")

    system = ANALYZE_SYSTEM.format(
        context_block=build_context_block(ctx, queries=queries),
        memory_block=build_memory_block(memory_suggestions),
    )

    origin = (
        "This report was generated earlier by DataTalk from the database."
        if source == "own"
        else "This is an external report provided by the user."
    )
    focus_line = f"\n\nFocus especially on: {focus}" if focus else ""
    body = clip_text(report_text, _MAX_INPUT_CHARS)
    user_content = f"{origin}{focus_line}\n\n=== REPORT ===\n{body}"

    resp = ctx.openai.chat.completions.create(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        **_completion_kwargs(ctx),
    )
    return resp.choices[0].message.content or ""


def analyze_dashboard(
    document: Document,
    *,
    ctx: "TenantContext",
    focus: str | None = None,
    memory_suggestions: list[str] | None = None,
    queries: list[dict[str, Any]] | None = None,
) -> str:
    """Analyze a materialized dashboard and return Markdown findings.

    Reads only the numbers already on the dashboard (flattened via
    ``document_to_text``); runs no SQL. Returns even when the dashboard is
    empty. ``queries`` (the saved dashboard's captured queries) selects which
    context-model bodies inform the critique.
    """
    text = clip_text(document_to_text(document), _MAX_INPUT_CHARS)
    system = DASHBOARD_ANALYZE_SYSTEM.format(
        context_block=build_context_block(ctx, queries=queries),
        memory_block=build_memory_block(memory_suggestions),
    )
    focus_line = f"\n\nFocus especially on: {focus}" if focus else ""
    user_content = f"{focus_line}\n\n=== DASHBOARD ===\n{text}"
    resp = ctx.openai.chat.completions.create(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        **_completion_kwargs(ctx),
    )
    return resp.choices[0].message.content or ""
