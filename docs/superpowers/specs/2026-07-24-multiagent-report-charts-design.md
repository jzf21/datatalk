# Multiagent Report Generation with Charts, Tables, and Q&A

**Date:** 2026-07-24
**Status:** Approved — implementation in progress

## Problem

Report generation currently produces a single narrative Markdown string
([`agent/report.py`](../../../datatalk/agent/report.py)). The prompt actively
suppresses charts and tables ("not a bare table dump and not chart
specifications"), and there is no way to ask follow-up questions about the data
after a report is generated.

## Goals

1. Report generation produces **charts and tables backed by real query data**,
   alongside narrative.
2. After a report is generated, the user can **ask questions about the data** in
   a conversation.
3. The system is structured as a **multiagent pipeline**.

## Non-goals

- No change to the ClickHouse read-only execution guardrails
  ([`agent/executor.py`](../../../datatalk/agent/executor.py)) — reused as-is.
- No move off SQLite for app metadata. The `MemoryStore` boundary keeps a future
  Postgres/pgvector migration cheap, but that is out of scope here.
- No model fine-tuning; memory stays retrieval-augmented as today.

## Core idea: a dataset-referencing block document

The key decision that makes charts trustworthy: **the LLM never re-types
numbers.** The Analyst captures query result sets, each addressable by a
`dataset_id`. The Reporter and Q&A agent emit blocks that *reference* a dataset
plus column mappings; the backend **materializes** those references into concrete
values before anything is stored or sent to the UI.

A **Document** is an ordered list of typed blocks. New module
`datatalk/agent/blocks.py` owns the block dataclasses, `to_dict`/`from_dict`,
`materialize(doc, datasets)`, and `document_to_text(doc)`.

Block types:

- `heading` — `{level, text}`
- `paragraph` — `{text}` (inline markdown allowed)
- `table`
  - authoring form (from LLM): `{dataset_id, columns?}` (columns optional = all)
  - materialized form: `{columns, rows}`
- `chart`
  - authoring form: `{dataset_id, chart_type, title, x_col, series_cols}`
    where `chart_type ∈ {bar, line, area, pie}`
  - materialized form: `{chart_type, title, x: {label, values}, series: [{name, values}]}`

`materialize(doc, datasets)` resolves each authoring table/chart against the
captured dataset with that id. Missing dataset id or a column not present in the
dataset degrades gracefully to a `paragraph` note (e.g. "_chart unavailable: …_")
rather than raising — a malformed reference must never abort a whole report.

`document_to_text(doc)` flattens a materialized Document to plain text (headings,
paragraphs, tables as pipe rows, charts as a titled value summary) for the
Analyze agent and for memory embedding.

## Agents

### Generation pipeline

Orchestrated by `generate_report()` in `agent/report.py`, which keeps the
existing `on_event(kind, data)` progress mechanism and emits per-phase events.

1. **Planner** (`agent/planner.py`)
   - In: request + schema context + memory suggestions.
   - Out: a plan = ordered list of sections, each `{id, title, goal,
     data_questions: [...]}`. JSON output.
   - No DB access.

2. **Analyst** (`agent/analyst.py`)
   - In: plan + schema context.
   - Runs the existing agentic `run_sql` loop (reuses `executor.run_sql`) to
     gather data covering every section.
   - Out: captured datasets `{dataset_id: QueryResult}` + query history list.
   - The only agent that touches ClickHouse during generation. Each successful
     query becomes an addressable dataset (`dataset_id` assigned by the
     orchestrator, e.g. `q1`, `q2`, …).

3. **Reporter** (`agent/reporter.py`)
   - In: plan + a compact preview of each captured dataset (id, columns, a few
     sample rows, row count).
   - Out: an authoring Document referencing datasets by id.
   - No DB access, no number transcription. The orchestrator materializes the
     returned Document against the captured datasets.

### Q&A agent

`agent/qa.py`
- In: the report Document + prior query history + schema context +
  conversation history + the new question.
- Has its own `run_sql` loop (re-queries ClickHouse), capturing new datasets.
- Out: an authoring Document (may include its own tables/charts) + query
  history. The orchestrator materializes it.

All four agents carry the existing anti-fabrication rule ("every figure must
come from a query you actually ran") and the redaction/secret-handling rules
from the current `REPORT_SYSTEM` prompt. New prompts live in
`llm/prompts.py` (`PLANNER_SYSTEM`, `ANALYST_SYSTEM`, `REPORTER_SYSTEM`,
`QA_SYSTEM`).

## Storage (`memory/store.py`)

- `reports` table: add `document` (JSON, canonical) and `queries` (JSON history)
  columns. Keep `markdown`, now populated via `document_to_text()` so the
  Analyze agent and memory retrieval keep working unchanged. Migration is an
  idempotent add-column guarded by a `PRAGMA table_info` check.
- New `qa_turns` table: `id, report_id, question, answer_document (JSON),
  queries (JSON), created_at`. Methods `add_qa_turn(report_id, question,
  answer_document, queries)` and `list_qa_turns(report_id)`.
- `save_report(request, document, queries)` — signature change; derives
  `markdown` internally from `document_to_text(document)`. `SavedReport` gains
  `document` and `queries` fields.

## API (`web/app.py`)

- `POST /api/report` (streaming NDJSON) — new phase events: `plan` (the plan),
  existing `sql`/`result`/`status`/`error` during the Analyst loop, and a
  `report` event now carrying the **materialized Document** (not markdown).
  `saved` carries `report_id`.
- `POST /api/reports/{id}/ask` (streaming NDJSON) — same event shape as report;
  runs the Q&A agent, shows SQL as it runs, persists the turn, and the final
  `answer` event carries the materialized answer Document. 404 if the report
  does not exist.
- `GET /api/reports/{id}` — now returns `document`, `queries`, and prior
  `qa_turns` so a conversation can be reopened.
- `POST /api/analyze` — unchanged interface; reads the report's derived text
  (`markdown` column) as today.

## Frontend (`web/static/`)

- Vendor Chart.js (single UMD file) into `web/static/vendor/chart.min.js` —
  offline, no build step. Served as a static file.
- Replace the markdown-string report render with a **block renderer**: headings,
  paragraphs (via the existing inline-markdown helper), styled tables, and
  `<canvas>` charts drawn with Chart.js. The existing `md()` string renderer
  stays only for the Analyze tab output.
- Add a **Q&A chat panel** beneath the report: a question input + a turn list,
  each answer rendered with the same block renderer (so answers can include
  charts/tables), streaming SQL progress like generation.
- Loading a past report (from the existing report list) rehydrates its blocks +
  prior Q&A turns.

## Testing

- `blocks.py` — round-trip `to_dict`/`from_dict`; `materialize` including the
  missing-dataset and bad-column-mapping degradation paths; `document_to_text`.
- Planner / Analyst / Reporter / QA — unit tests with a stubbed LLM client
  following the existing [`tests/test_report_agent.py`](../../../tests/test_report_agent.py)
  pattern. Assert the wiring: planner → analyst → reporter yields a valid
  materialized Document; the Reporter never invents numbers (materialized values
  equal captured dataset values); the QA agent runs `run_sql` and returns a
  Document.
- `store.py` — migration adds columns on an old DB without data loss;
  `save_report`/`get_report` round-trip document + queries; `qa_turns` CRUD.
- `executor.py` guardrails unchanged (reused as-is).

## Module summary

New:
- `datatalk/agent/blocks.py` — Document/block model, materialize, document_to_text
- `datatalk/agent/planner.py` — Planner agent
- `datatalk/agent/analyst.py` — Analyst agent (agentic run_sql loop)
- `datatalk/agent/reporter.py` — Reporter agent
- `datatalk/agent/qa.py` — Q&A agent
- `datatalk/web/static/vendor/chart.min.js` — vendored chart library

Changed:
- `datatalk/agent/report.py` — becomes the generation orchestrator
- `datatalk/llm/prompts.py` — new per-agent prompts
- `datatalk/memory/store.py` — document/queries columns, qa_turns table
- `datatalk/web/app.py` — Document-carrying report event, `/ask` endpoint
- `datatalk/web/static/index.html` — block renderer + Q&A chat panel
