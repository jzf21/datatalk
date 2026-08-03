"""Insight synthesis pass — between the Analyst and the Dashboard author.

The author sees a few preview rows per dataset, which is enough to reference
columns but not enough to notice a trend, an outlier, or two datasets saying
the same thing. This pass reviews the captured data at full Analyst fidelity
(the same row cap the loop itself shows the model) and emits structured
findings that the author uses to decide what leads, what is dropped, and what
deserves a callout.

Best-effort by design: any failure — an unparsable reply, a dead endpoint —
degrades to an empty insight block, and the dashboard ships without it. An
insight pass that can kill the dashboard would cost more than it adds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from datatalk import observability as obs
from datatalk.agent.blocks import parse_json_object
from datatalk.agent.context_block import build_context_block
from datatalk.agent.planner import Section, plan_to_text
from datatalk.agent.sqlloop import (
    ROWS_TO_MODEL,
    EventFn,
    LoopResult,
    clip_text,
    dataset_previews,
)
from datatalk.llm.prompts import ANTI_FABRICATION, DASHBOARD_INSIGHT_SYSTEM

if TYPE_CHECKING:
    from datatalk.context import TenantContext

# A prose fallback longer than this stops being an insight block and starts
# being a second report.
_MAX_FALLBACK_CHARS = 2000


@dataclass
class InsightResult:
    """What the insight pass produced, in the two forms its consumers need."""

    text_block: str = ""  # rendered fence for the author's user message
    raw: dict[str, Any] = field(default_factory=dict)  # parsed JSON, {} on failure


def _render_text_block(raw: dict[str, Any]) -> str:
    lines = ["=== DATA INSIGHTS (from a review of the full captured data) ==="]
    for ins in raw.get("insights", []):
        if not isinstance(ins, dict):
            continue
        finding = str(ins.get("finding") or "").strip()
        if not finding:
            continue
        line = (
            f"[importance {ins.get('importance', '?')}] "
            f"({ins.get('dataset_id', '?')}, {ins.get('kind', 'finding')}) {finding}"
        )
        hint = str(ins.get("presentation_hint") or "").strip()
        if hint:
            line += f" — {hint}"
        lines.append(line)
    if raw.get("lead"):
        lines.append(f"Lead with: {', '.join(str(d) for d in raw['lead'])}")
    if raw.get("drop"):
        lines.append(f"Do not show: {', '.join(str(d) for d in raw['drop'])}")
    if raw.get("gaps"):
        lines.append(
            "Unanswered by this data: " + "; ".join(str(g) for g in raw["gaps"])
        )
    if len(lines) == 1:
        return ""
    lines.append("=== END DATA INSIGHTS ===")
    return "\n".join(lines)


def synthesize_insights(
    request: str,
    sections: list[Section],
    loop: LoopResult,
    *,
    ctx: "TenantContext",
    memory_block: str = "",
    context_block: str | None = None,
    on_event: EventFn | None = None,
) -> InsightResult:
    """Review the captured datasets and return findings for the author.

    Runs on the author model/client pair — it is the other half of the same
    quality-over-cost trade the author call already makes. Sees
    ``ROWS_TO_MODEL`` rows per dataset (what the Analyst itself saw), the
    plan, and the Analyst's closing manifest (``loop.final_content``).

    ``context_block`` lets the orchestrator compute the (identical) block once
    for this pass and the author; ``None`` computes it here for standalone use.
    """

    def emit(kind: str, data: dict[str, Any]) -> None:
        if on_event:
            on_event(kind, data)

    if context_block is None:
        context_block = build_context_block(ctx, queries=loop.queries)
    system = DASHBOARD_INSIGHT_SYSTEM.format(
        anti_fabrication=ANTI_FABRICATION,
        context_block=context_block,
        memory_block=memory_block,
    )
    user = (
        f"User request:\n{request}\n\n"
        f"Dashboard plan:\n{plan_to_text(sections)}\n\n"
        f"Captured datasets (full data, up to {ROWS_TO_MODEL} rows each):\n"
        f"{dataset_previews(loop.datasets, sample_rows=ROWS_TO_MODEL, sources=loop.dataset_sources)}\n\n"
        f"Analyst notes:\n{loop.final_content or '(none)'}"
    )

    with obs.observe(
        "synthesize-insights",
        as_type=obs.AGENT,
        input={"request": request, "dataset_ids": list(loop.datasets)},
    ) as span:
        try:
            kwargs: dict[str, Any] = dict(
                model=ctx.author_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0,
            )
            max_tokens = ctx.settings.openai_author_max_tokens
            if max_tokens > 0:
                kwargs["max_tokens"] = max_tokens
            kwargs.update(obs.llm_kwargs("synthesize-insights"))
            resp = ctx.author_openai.chat.completions.create(**kwargs)
            reply = resp.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001 - the dashboard must ship without insights
            emit("error", {"message": f"Insight pass failed: {exc}"})
            # Swallowed on purpose, so it must be visible somewhere: without
            # this the dashboard just quietly gets worse and the trace looks
            # clean.
            span.update(
                output={"error": str(exc)},
                level="WARNING",
                status_message=f"insight pass failed: {exc}"[:500],
            )
            return InsightResult()

        raw = parse_json_object(reply)
        if raw:
            emit("insights", raw)
            span.update(
                output=raw, metadata={"insight_count": len(raw.get("insights", []) or [])}
            )
            return InsightResult(text_block=_render_text_block(raw), raw=raw)

        if reply.strip():
            # Unparsable but non-empty: a prose insight still beats none.
            fenced = (
                "=== DATA INSIGHTS (from a review of the full captured data) ===\n"
                f"{clip_text(reply.strip(), _MAX_FALLBACK_CHARS)}\n"
                "=== END DATA INSIGHTS ==="
            )
            span.update(output=reply, metadata={"parsed": False})
            return InsightResult(text_block=fenced, raw={})
        span.update(output=None, metadata={"parsed": False, "empty": True})
        return InsightResult()
