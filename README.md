# DataTalk

Schema-agnostic LLM report generation and analysis over Jira/accounts data stored in ClickHouse.

You ask, in natural language, for a report. DataTalk introspects your ClickHouse database at
runtime (so nothing about your schema is hardcoded), lets an OpenAI model explore the data by
writing read-only SQL, and returns a full narrative report. It can also analyze reports and gets
better over time from your suggestions.

## Status

Built incrementally by milestone:

- [x] **M1 — Connection checkout**: connect to ClickHouse, discover schema, verify OpenAI key.
- [x] **M2 — Safe read-only SQL execution**: tokenizer-based guardrails (30 tests).
- [x] **M3 — Text-to-SQL report agent**: agentic `run_sql` tool loop → narrative report.
- [x] **M4 — Report analysis**: analyze own + external reports.
- [x] **M5 — Few-shot memory**: "training" from user suggestions (SQLite + embeddings).
- [x] **M6 — Web UI**: FastAPI + streaming single-page frontend.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
# edit .env with your ClickHouse connection + LLM API key
```

### LLM provider

Works with OpenAI or any OpenAI-compatible endpoint. For **Nebius Token Factory**:

```
OPENAI_BASE_URL=https://api.tokenfactory.nebius.com/v1/
OPENAI_API_KEY=<your nebius key>
OPENAI_MODEL=openai/gpt-oss-120b            # must support tool/function calling
OPENAI_EMBED_MODEL=Qwen/Qwen3-Embedding-8B
```

> **Security — the LLM writes SQL:**
> - Point `CLICKHOUSE_USER` at a **read-only** ClickHouse user as defense-in-depth.
> - The executor enforces read-only at the application layer (SELECT/WITH/SHOW/DESCRIBE only).
> - Scope the schema with `INTROSPECT_DATABASES` / `INTROSPECT_EXCLUDE_TABLE_PATTERNS` to keep
>   prompts small and focused.

## Connection checkout (run this first)

```bash
python -m datatalk.scripts.check_connection
# or
datatalk-check
```

This verifies ClickHouse connectivity, prints the discovered schema with sample rows, and makes
one tiny OpenAI call to confirm your key and model.

## Run the web app

```bash
uvicorn datatalk.web.app:app --reload
# open http://127.0.0.1:8000
```

Three tabs:
- **Report** — ask in natural language; watch the agent run SQL live, then read the report.
- **Analyze** — analyze a report DataTalk generated, or paste an external one.
- **Memory** — add suggestions ("for SLA use jira.sla_events"); relevant ones are injected into
  future reports. This is the "training" loop.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```
