# DataTalk Dashboards — Design Spec

**Date:** 2026-07-24
**Status:** Approved for planning

## Summary

Add **dashboards** to DataTalk: a new grid-layout artifact — KPI stat-tiles and
charts arranged in a responsive grid — distinct from the existing narrative
reports. The agent generates a dashboard, the user can ask the agent to
**analyze** it (grounded insights from the real captured data), and every
generated dashboard is **auto-saved** and reopenable.

Dashboards reuse the existing multi-agent pipeline and the core trust rule —
**the LLM never types numbers.** Only the authoring/output shape and rendering
change; `materialize()` still fills concrete values from captured datasets.

## Goals

- Generate visually structured dashboards (KPI tiles + charts + tables in a grid).
- Analyze a dashboard's real underlying data and surface findings (trends,
  outliers, risks).
- Auto-save dashboards; list and reopen them with their data intact.

## Non-goals (YAGNI)

- No drag/resize or manual layout editing.
- No dashboard Q&A chat (reports already have that).
- No follow-up SQL during analysis — analysis reads the numbers already on the
  dashboard.
- No raw-dataset persistence — the materialized dashboard already holds the real
  values the analysis needs.

## Approach

Reuse **Planner → Analyst** unchanged (they already gather datasets `q1, q2, …`).
Add a **Dashboard authoring agent** that emits a *grid* document instead of a
narrative one, plus two new block types. This keeps the anti-fabrication core
(`materialize()`) and avoids duplicating Planner/Analyst.

Rejected alternatives: a fully separate dashboard pipeline (duplicates
Planner/Analyst); a freeform x/y/w/h grid (heavier to author, render, validate —
the responsive row grid is enough).

## Components

### 1. Block model — `agent/blocks.py`

Two new dataclasses, each following the existing **authoring → materialized**
pattern (drop-`None` serialization, graceful degradation to a paragraph note on
bad references, never raises).

**`Stat`** (KPI tile):
- Authoring form: `dataset_id`, `value_col`, `label`, optional `row_index`
  (default: last row — aggregates are typically single-row), optional
  `delta_col` (a prior-period value column present in the same dataset), optional
  `unit` (e.g. `"%"`, `"$"`).
- Materialized form: `value`, `delta`, `delta_pct` (computed from `value` and the
  `delta_col` value), plus `label`/`unit` carried through.
- The number is **pulled from the dataset, never typed.** Unknown
  dataset/column/row → paragraph note.

**`Row`** (container):
- `children`: a list of child blocks; each child carries a `width` (1–12 on a
  12-col grid). Named `children` (not `columns`) to avoid colliding with the
  existing `columns` field on `Table`/`Chart`, which means column *names*. `Row`
  itself is not a data block.
- `materialize()` recurses into a `Row`'s children. A child that fails
  materialization degrades to a note in place; the row still renders.

`Heading`/`Paragraph`/`Table`/`Chart` are unchanged and may appear inside a `Row`
or at top level. `_BLOCK_CLASSES`, `block_from_dict`, `materialize`, and
`document_to_text` all learn about `Stat` and `Row` (so flattening for analysis
and memory keeps working — a `Row` flattens by flattening its children; a `Stat`
flattens to `"label: value (Δ delta)"`).

`width` handling: values are clamped to 1–12; missing width defaults to an even
split across the row's children.

### 2. Generation — `agent/dashboard.py` (new)

`generate_dashboard(request, *, memory_suggestions, on_event, max_steps,
settings)` mirrors `generate_report()`:

1. Load schema, build memory block.
2. **Planner** (`plan_report`) — reused unchanged; emits `plan`.
3. **Analyst** (`gather_data`) — reused unchanged; emits `sql`/`result`,
   captures datasets.
4. **Dashboard authoring agent** (new, in this module or `agent/dashboard.py`):
   given the plan + `dataset_previews(datasets)`, emits an authoring grid
   Document. Uses a new `DASHBOARD_SYSTEM` prompt and an extended block-schema
   doc describing `stat` and `row`.
5. `materialize()` the authoring Document against the datasets; emit
   `dashboard` with the materialized doc.

Returns a `DashboardResult` (mirrors `ReportResult`: `request`, `document`,
`queries`, `steps`).

The authoring agent is instructed to lay out: a **row of KPI stat-tiles first**,
then **rows of charts**, then **supporting tables** — and to reference only
dataset ids/columns that exist.

### 3. Analysis — `agent/analyze.py`

New `analyze_dashboard(document, *, focus, memory_suggestions, settings)`:
- Flattens the **materialized** dashboard (`document_to_text`, which now includes
  stats/rows) — this already contains the real numbers on every tile/chart/table.
- Feeds it to an insights agent (new `DASHBOARD_ANALYZE_SYSTEM` prompt) that
  returns Markdown findings: notable trends, outliers, correlations, risks, and
  suggested follow-ups. Grounded in the shown data; runs no SQL.

The existing `analyze_report` is untouched.

### 4. Persistence — `memory/store.py`

New `dashboards` table, created with `CREATE TABLE IF NOT EXISTS` in
`_init_schema` (same idempotent style as `reports`):

```
dashboards(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request TEXT NOT NULL,
  title TEXT NOT NULL,
  document TEXT NOT NULL,   -- JSON of materialized Document
  queries TEXT NOT NULL,    -- JSON
  analysis TEXT,            -- Markdown, nullable until analyzed
  created_at TEXT NOT NULL
)
```

New `SavedDashboard` dataclass + methods: `save_dashboard(request, document,
queries, title=None)` (title derived from the request when omitted),
`get_dashboard(id)`, `list_dashboards(limit=50)`, and
`set_dashboard_analysis(id, analysis)` (UPDATE). JSON serialization uses
`json.dumps(..., default=str)` per the ClickHouse-datetimes convention.

### 5. Web + UI — `web/app.py`, `web/static/index.html`

**Endpoints:**
- `POST /api/dashboard` — streaming NDJSON, mirrors `/api/report`. Emits
  `status`/`plan`/`sql`/`result`/`dashboard`, then auto-saves and emits `saved`
  (`{dashboard_id, queries, steps}`).
- `POST /api/dashboards/{id}/analyze` — runs `analyze_dashboard` on the saved
  doc, persists via `set_dashboard_analysis`, returns `{analysis}`.
- `GET /api/dashboards` — list (`id`, `request`/`title`, `created_at`).
- `GET /api/dashboards/{id}` — full record (document, queries, analysis).

**Frontend (new "Dashboard" tab, additive — Report/Analyze/Memory untouched):**
- Request textarea + "Generate dashboard" + a "— open a saved dashboard —"
  picker.
- **Grid renderer:** CSS grid, 12 columns, responsive (collapses to fewer
  columns on narrow widths). Renders `row` blocks as grid rows and their children
  at the given `width`.
- **Stat-tile component:** large value, label, unit, and a colored delta
  (up/down) when present. Reuses existing Chart.js/table renderers inside cells.
- **"Analyze" button** on a rendered dashboard → calls the analyze endpoint and
  renders the Markdown insights below (reusing the existing `md()` renderer); the
  saved analysis reloads when a dashboard is reopened.

### 6. Prompts — `llm/prompts.py`

- Extend the block-schema doc (or add a `DASHBOARD_BLOCK_SCHEMA_DOC`) to describe
  `stat` and `row` block shapes and the "KPIs first, then charts, then tables"
  layout guidance. Keep the NEVER-type-numbers rules.
- `DASHBOARD_SYSTEM` — the dashboard authoring agent's system prompt (carries
  `_ANTI_FABRICATION`).
- `DASHBOARD_ANALYZE_SYSTEM` — the grounded-insights analysis prompt.

## Data flow

```
request
  → Planner (sections)
  → Analyst (datasets q1..qn)         [only DB access]
  → Dashboard authoring agent         → authoring grid Document (refs only)
  → materialize(doc, datasets)        → concrete Stat/Row/Chart/Table values
  → auto-save (dashboards table)      → dashboard_id
  → [user clicks Analyze]
  → analyze_dashboard(materialized doc) → Markdown findings → persisted
```

## Error handling

- Bad dataset/column/row references in any block (`Stat`/`Row` child/`Table`/
  `Chart`) degrade to a paragraph note — never raise. Matches existing behavior.
- Malformed authoring JSON → `Document.from_dict` tolerates and drops unknown
  blocks (existing behavior).
- Analysis on a dashboard with no numbers still returns (agent notes the lack of
  data); empty datasets don't crash rendering.
- Endpoints follow existing patterns: 400 on empty request, 404 on missing
  dashboard, streaming errors surfaced as an `error` NDJSON event.

## Testing

Follow the scripted-fake-OpenAI pattern in `tests/test_report_agent.py` (no live
DB/API):

1. **Blocks:** `materialize()` fills a `Stat` (value + delta_pct) and a `Row`'s
   children from datasets; a bad `value_col`/`dataset_id` degrades to a note; a
   `Row` with one bad child still renders the good children. `document_to_text`
   flattens stats/rows.
2. **Generation:** dashboard authoring agent (fake client returning grid JSON)
   yields a valid authoring Document; `generate_dashboard` materializes it and
   emits the expected events.
3. **Analysis:** `analyze_dashboard` runs off a materialized dashboard and
   returns the fake client's Markdown.
4. **Store:** `save_dashboard` → `get_dashboard` round-trips document + queries;
   `set_dashboard_analysis` persists and reloads; `list_dashboards` orders newest
   first; `CREATE TABLE IF NOT EXISTS` is idempotent across two `MemoryStore`s.

## Open decisions (resolved)

- **Stat delta:** the authoring agent chooses a `delta_col` from a dataset when a
  prior-period column exists; we do not compute comparisons ourselves. Deltas
  only appear when the Analyst captured a comparison column. (Confirmed.)
- **Additive UI:** the Dashboard tab is added; existing tabs are untouched.
  (Confirmed.)
