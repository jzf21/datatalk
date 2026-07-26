# DataTalk

Schema-agnostic LLM report generation and analysis over ClickHouse and PostgreSQL.

You ask, in natural language, for a report. DataTalk introspects your databases at runtime (so
nothing about your schema is hardcoded), lets an OpenAI model explore the data by writing
read-only SQL, and returns a full narrative report. It can also analyze reports and gets better
over time from your suggestions.

DataTalk is **multi-tenant**: users sign in, belong to one or more workspaces (orgs). No
workspace ever reads another's data.

It is also **multi-source**: a workspace connects any number of named data sources, each a
ClickHouse or PostgreSQL warehouse. The agent sees a catalog of all of them and chooses which
to query — so a single report can pull traffic from a ClickHouse events warehouse and revenue
from a Postgres billing database, and relate the two. (One SQL statement still runs against one
source; DataTalk composes the results rather than joining across warehouses.)

## Status

Built incrementally by milestone:

- [x] **M1 — Connection checkout**: connect to ClickHouse, discover schema, verify OpenAI key.
- [x] **M2 — Safe read-only SQL execution**: tokenizer-based guardrails (30 tests).
- [x] **M3 — Text-to-SQL report agent**: agentic `run_sql` tool loop → narrative report.
- [x] **M4 — Report analysis**: analyze own + external reports.
- [x] **M5 — Few-shot memory**: "training" from user suggestions (embeddings).
- [x] **M6 — Web UI**: FastAPI + streaming NDJSON, Next.js frontend.
- [x] **M7 — Multi-tenancy**: Postgres, auth, orgs, per-org encrypted connections.
- [x] **M8 — Multi-source**: pluggable warehouse adapters (ClickHouse + PostgreSQL), several
      sources per workspace, agent-chosen per query.

## Setup

DataTalk needs Postgres for its own state (users, orgs, reports, memory), plus at least one
data source — ClickHouse or PostgreSQL — holding the data you ask questions about.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
# Edit .env: DATABASE_URL, DATATALK_SECRET_KEY, and your LLM API key.

docker compose up -d       # Postgres on :5433
datatalk-db upgrade        # apply migrations -- the app refuses to start without this
```

Generate the encryption key (it protects stored data source passwords; losing it
means re-entering every workspace's credentials):

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The `CLICKHOUSE_*` values in `.env` are only a default for `datatalk-check` and
`--bootstrap-warehouse`. Real workspaces store their own sources, entered in the UI
(Settings → Data sources) and encrypted at rest — a workspace with none configured gets
a 409 rather than falling back to the environment's warehouse.

### LLM provider

Works with OpenAI or any OpenAI-compatible endpoint. For **Nebius Token Factory**:

```
OPENAI_BASE_URL=https://api.tokenfactory.nebius.com/v1/
OPENAI_API_KEY=<your nebius key>
OPENAI_MODEL=openai/gpt-oss-120b            # must support tool/function calling
OPENAI_EMBED_MODEL=Qwen/Qwen3-Embedding-8B
```

> **Security — the LLM writes SQL:**
> - The executor enforces read-only at the application layer (SELECT/WITH/SHOW/DESCRIBE only),
>   with the forbidden-keyword list and quoting rules of the source's own engine.
> - **Postgres sources open every session read-only**, so the server refuses writes even if a
>   statement slips past the guardrails.
> - ClickHouse has no equivalent, so point each ClickHouse source at a **read-only** account.
> - Scope each source with its database/schema allowlist and exclude patterns to keep prompts
>   small and focused.

## Connection checkout

```bash
datatalk-check
```

Verifies the **environment's** ClickHouse connectivity, prints the discovered schema with
sample rows, and makes one tiny OpenAI call to confirm your key and model. A workspace's own
sources are tested from the UI instead (Settings → Data sources), which is also where you add
Postgres ones.

## Run it

Two processes, two origins:

```bash
uvicorn datatalk.web.app:app --reload      # API on :8000
cd frontend && npm install && npm run dev  # UI on :3000
```

Open **http://localhost:3000** and sign up — the first account creates a workspace.

> Use `localhost`, not `127.0.0.1`. The session cookie is `SameSite=Lax`, so
> `localhost:3000 → 127.0.0.1:8000` counts as cross-site, the cookie is withheld,
> and every request 401s with nothing in either log. Keep the hostnames matching,
> and keep the UI's origin in `DATATALK_CORS_ORIGINS`.

Four sections: **Reports** (ask in natural language; watch the agent run SQL live),
**Dashboards**, **Analyze** (a report DataTalk generated, or one you paste in), and
**Memory** (suggestions like "for SLA use jira.sla_events", injected into future
reports — the "training" loop). Workspace switching and data sources live in the
account menu and Settings.

Give each source a short **name** (the agent types it: `run_sql(source: "billing", …)`) and a
one-line **description** of what it holds. The description is the main thing steering the agent
to the right source for a question, so it is worth writing well.

## Migrating a pre-multi-tenant database

Older single-tenant installs kept everything in `datatalk.sqlite3`. Import it into a
workspace:

```bash
datatalk-import-sqlite --org-name "Acme" --owner-email you@example.com \
  --owner-password '<pw>' --bootstrap-warehouse --dry-run
```

`--dry-run` does the whole import, reports the counts, then rolls back. Drop it to
commit. Re-runs are no-ops, so it is safe to repeat. `--owner-password` is only needed
when the account does not exist yet; `--bootstrap-warehouse` seeds the workspace's first
data source from the current environment's `CLICKHOUSE_*` values.

## Tests

```bash
pip install -e ".[dev]"
DATATALK_TEST_DATABASE_URL=postgresql+psycopg://datatalk:datatalk@localhost:5433/datatalk_test \
  pytest -q
```

`docker compose up -d` creates the `datatalk_test` database alongside the dev one
(see `scripts/init-test-db.sql`), so there is nothing else to set up.

The DB tests **skip silently** without `DATATALK_TEST_DATABASE_URL`, and refuse to run if
it equals `DATABASE_URL` — the suite truncates tables. Frontend tests: `cd frontend && npm test`.
