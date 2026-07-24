"""Q&A agent — answers follow-up questions about a generated report.

Has its own agentic ``run_sql`` loop (re-queries ClickHouse, capturing new
datasets), then answers with a block Document. The returned Document is already
materialized against the datasets this agent captured.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from datatalk.agent.blocks import Document, materialize, parse_json_object
from datatalk.agent.sqlloop import EventFn, run_capture_loop
from datatalk.config import Settings, get_settings
from datatalk.llm.client import get_openai
from datatalk.llm.prompts import BLOCK_SCHEMA_DOC, QA_SYSTEM, _ANTI_FABRICATION


@dataclass
class QAResult:
    question: str
    answer_document: Document
    queries: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0


def answer_question(
    question: str,
    *,
    report_document: Document,
    prior_queries: list[dict[str, Any]] | None = None,
    conversation: list[dict[str, str]] | None = None,
    schema_context: str,
    on_event: EventFn | None = None,
    max_steps: int = 6,
    settings: Settings | None = None,
) -> QAResult:
    """Answer ``question`` about ``report_document`` with a materialized Document.

    ``conversation`` is a list of prior ``{"question", "answer"}`` turns for
    context. ``prior_queries`` is the report's query history (sql/columns only).
    """
    settings = settings or get_settings()
    client = get_openai()

    system = QA_SYSTEM.format(
        block_schema=BLOCK_SCHEMA_DOC,
        anti_fabrication=_ANTI_FABRICATION,
        schema_context=schema_context,
    )

    from datatalk.agent.blocks import document_to_text

    context_parts = [
        "=== REPORT ===",
        document_to_text(report_document),
        "=== END REPORT ===",
    ]
    if prior_queries:
        context_parts += [
            "\n=== QUERIES THAT PRODUCED THE REPORT ===",
            json.dumps(prior_queries, default=str),
            "=== END QUERIES ===",
        ]
    for turn in conversation or []:
        context_parts.append(f"\nPrevious Q: {turn.get('question', '')}")
        context_parts.append(f"Previous A: {turn.get('answer', '')}")
    context_parts.append(f"\nNew question: {question}")

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(context_parts)},
    ]

    loop = run_capture_loop(
        client,
        settings.openai_model,
        messages,
        max_steps=max_steps,
        on_event=on_event,
        settings=settings,
    )

    authoring = Document.from_dict(parse_json_object(loop.final_content))
    answer = materialize(authoring, loop.datasets)
    return QAResult(
        question=question,
        answer_document=answer,
        queries=loop.queries,
        steps=loop.steps,
    )
