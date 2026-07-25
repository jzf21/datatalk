# DataTalk — guidance for Claude Code

Schema-agnostic LLM report generation and Q&A over ClickHouse data. Python 3.11+,
FastAPI backend, single-file vanilla-JS frontend.

## Commands
- Tests: `python -m pytest -q`
- Run the app: `uvicorn datatalk.web.app:app --reload`
- Check connections (run first): `datatalk-check`
- Config comes from a local `.env` (see `.env.example`).

## Architecture
Report generation is a **multi-agent pipeline** orchestrated by
`agent/report.py`: Planner → Analyst → Reporter.

- `agent/blocks.py` — the `Document` model (heading/paragraph/table/chart
  blocks). The core trust rule: **the LLM never types numbers.** Agents emit
  *authoring* blocks that reference a captured dataset by id + columns;
  `materialize()` fills in real values. Bad references degrade to a paragraph
  note, never raise.
- `agent/sqlloop.py` — shared agentic `run_sql` loop; each successful query is
  captured as an addressable dataset (`q1`, `q2`, …). Used by the Analyst and QA.
- `agent/planner.py` / `analyst.py` / `reporter.py` / `qa.py` — the agents.
  Only the Analyst (and QA) touch ClickHouse.
- `agent/executor.py` — **read-only SQL guardrails** (SELECT/WITH/SHOW/DESCRIBE
  only). Reused as-is; do not weaken.
- `memory/store.py` — SQLite: suggestions (retrieval-augmented memory), reports,
  qa_turns. Schema migrations are idempotent `PRAGMA`-guarded add-columns.
- `web/app.py` — streaming NDJSON endpoints; `web/static/index.html` — block
  renderer + Q&A chat, Chart.js vendored under `static/vendor/`.
- `llm/prompts.py` — all system prompts.

## Conventions
- Every agent carries the anti-fabrication rule (`_ANTI_FABRICATION`).
- Agents return structured data; parse LLM JSON with `blocks.parse_json_object`.
- Any value serialized for the wire/DB uses `json.dumps(..., default=str)`
  (ClickHouse rows can hold datetimes/Decimals).
- Add tests alongside changes; agent tests use a scripted fake OpenAI client
  (no live DB/API) — see `tests/test_report_agent.py`.
