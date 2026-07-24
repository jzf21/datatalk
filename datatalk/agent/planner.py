"""Planner agent — designs the report outline. No database access."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from datatalk.agent.blocks import parse_json_object
from datatalk.config import Settings, get_settings
from datatalk.llm.client import get_openai
from datatalk.llm.prompts import PLANNER_SYSTEM


@dataclass
class Section:
    id: str
    title: str
    goal: str
    data_questions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "data_questions": list(self.data_questions),
        }


def plan_report(
    request: str,
    *,
    schema_context: str,
    memory_block: str = "",
    settings: Settings | None = None,
) -> list[Section]:
    """Return an ordered list of planned report sections."""
    settings = settings or get_settings()
    client = get_openai()
    system = PLANNER_SYSTEM.format(
        schema_context=schema_context, memory_block=memory_block
    )
    resp = client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": request},
        ],
        temperature=0,
    )
    obj = parse_json_object(resp.choices[0].message.content or "")
    sections: list[Section] = []
    for i, raw in enumerate(obj.get("sections", []), start=1):
        if not isinstance(raw, dict):
            continue
        sections.append(
            Section(
                id=str(raw.get("id") or f"section_{i}"),
                title=str(raw.get("title") or f"Section {i}"),
                goal=str(raw.get("goal") or ""),
                data_questions=[str(q) for q in raw.get("data_questions", [])],
            )
        )
    return sections


def plan_to_text(sections: list[Section]) -> str:
    """Human-readable plan used to seed the Analyst and Reporter prompts."""
    lines: list[str] = []
    for i, s in enumerate(sections, start=1):
        lines.append(f"{i}. [{s.id}] {s.title} — {s.goal}")
        for q in s.data_questions:
            lines.append(f"   - {q}")
    return "\n".join(lines)
