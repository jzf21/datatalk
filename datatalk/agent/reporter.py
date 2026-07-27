"""Reporter agent — writes an authoring block Document from captured datasets.

No database access, no number transcription: it references datasets by id and
the orchestrator materializes those references into concrete values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from datatalk.agent.blocks import Document, parse_json_object
from datatalk.agent.context_block import build_context_block
from datatalk.agent.planner import Section, plan_to_text
from datatalk.agent.sqlloop import dataset_previews
from datatalk.llm.prompts import BLOCK_SCHEMA_DOC, REPORTER_SYSTEM, ANTI_FABRICATION

if TYPE_CHECKING:
    from datatalk.context import TenantContext


def write_report(
    request: str,
    sections: list[Section],
    datasets: dict,
    *,
    ctx: "TenantContext",
    sources: dict[str, str] | None = None,
    memory_block: str = "",
    queries: list[dict] | None = None,
) -> Document:
    """Return an *authoring* Document referencing the captured datasets by id.

    ``sources`` maps dataset id to the source it came from, so the Reporter can
    say which warehouse a block's numbers are drawn from. ``memory_block``
    carries learned user guidance — it shapes wording and units, which are
    decided here rather than upstream. ``queries`` (the Analyst's captured
    query records) selects which ontology/playbook bodies are worth injecting:
    the ones covering the tables this report actually drew from.
    """
    system = REPORTER_SYSTEM.format(
        block_schema=BLOCK_SCHEMA_DOC,
        anti_fabrication=ANTI_FABRICATION,
        context_block=build_context_block(ctx, queries=queries),
        memory_block=memory_block,
    )
    user = (
        f"User request:\n{request}\n\n"
        f"Report plan:\n{plan_to_text(sections)}\n\n"
        f"Captured datasets (reference these by dataset_id):\n"
        f"{dataset_previews(datasets, sources=sources)}"
    )
    resp = ctx.openai.chat.completions.create(
        model=ctx.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0,
    )
    obj = parse_json_object(resp.choices[0].message.content or "")
    return Document.from_dict(obj)
