"""Reporter agent — writes an authoring block Document from captured datasets.

No database access, no number transcription: it references datasets by id and
the orchestrator materializes those references into concrete values.
"""

from __future__ import annotations

from datatalk.agent.blocks import Document, parse_json_object
from datatalk.agent.planner import Section, plan_to_text
from datatalk.agent.sqlloop import dataset_previews
from datatalk.config import Settings, get_settings
from datatalk.llm.client import get_openai
from datatalk.llm.prompts import BLOCK_SCHEMA_DOC, REPORTER_SYSTEM, _ANTI_FABRICATION


def write_report(
    request: str,
    sections: list[Section],
    datasets: dict,
    *,
    settings: Settings | None = None,
) -> Document:
    """Return an *authoring* Document referencing the captured datasets by id."""
    settings = settings or get_settings()
    client = get_openai()
    system = REPORTER_SYSTEM.format(
        block_schema=BLOCK_SCHEMA_DOC, anti_fabrication=_ANTI_FABRICATION
    )
    user = (
        f"User request:\n{request}\n\n"
        f"Report plan:\n{plan_to_text(sections)}\n\n"
        f"Captured datasets (reference these by dataset_id):\n"
        f"{dataset_previews(datasets)}"
    )
    resp = client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0,
    )
    obj = parse_json_object(resp.choices[0].message.content or "")
    return Document.from_dict(obj)
