# DataTalk — guidance for Claude Code

Schema-agnostic LLM report generation and Q&A over a workspace's data,
**multi-tenant** and **multi-source**: users belong to orgs, and each org brings
any number of named data sources, each a ClickHouse or Postgres warehouse. The
agent sees them all and picks per query. Python 3.11+, FastAPI backend on
Postgres, Next.js frontend in `frontend/`.

## Commands
- Postgres (Docker, port 5433): `docker compose up -d`
- Migrate (required — the app refuses to start on a stale schema): `datatalk-db upgrade`
- Run the API: `uvicorn datatalk.web.app:app --reload`
- Run the UI: `cd frontend && npm run dev` — then open **http://localhost:3000**
- Tests: `DATATALK_TEST_DATABASE_URL=... python -m pytest -q`
  (**every DB test silently skips without that variable**, and it must not equal
  `DATABASE_URL`). Frontend: `cd frontend && npm test`.
- Check the *deployment's* env warehouse (not any org's): `datatalk-check`
- Generate the context model: **Settings → Data context → Generate** in the UI
  (needs `OPENAI_DOCS_MODEL`, or it falls back to `OPENAI_MODEL`).
- Import a legacy single-tenant DB: `datatalk-import-sqlite --help` (has `--dry-run`)
- Config comes from a local `.env` (see `.env.example`).

> Use `localhost`, never `127.0.0.1`, for the API. The session cookie is
> `SameSite=Lax`; `localhost:3000 → 127.0.0.1:8000` is cross-*site*, so the
> cookie is dropped and every request 401s with no visible cause.

## Architecture
Report generation is a **multi-agent pipeline** orchestrated by
`agent/report.py`: Planner → Analyst → Reporter.

- `agent/blocks.py` — the `Document` model (heading/paragraph/table/chart
  blocks). The core trust rule: **the LLM never types numbers.** Agents emit
  *authoring* blocks that reference a captured dataset by id + columns;
  `materialize()` fills in real values. Bad references degrade to a paragraph
  note, never raise.
- `agent/sqlloop.py` — shared agentic loop over two tools: `run_sql(source, sql)`
  and `describe_source(source, table)`. Each successful query is captured as an
  addressable dataset (`q1`, `q2`, …) **tagged with its source**. Used by the
  Analyst and QA.
- `agent/planner.py` / `analyst.py` / `reporter.py` / `qa.py` — the agents.
  Only the Analyst (and QA) touch a warehouse.
- `agent/executor.py` — **read-only SQL guardrails** (SELECT/WITH/SHOW/DESCRIBE
  only), per-`Dialect`: shared leaders plus each engine's own unsafe verbs and
  quoting. Do not weaken.
- `warehouse/` — **the only package that names a SQL engine.** `base.py` holds
  the `Warehouse` protocol, `WarehouseSpec`, and `Dialect`; `clickhouse.py` and
  `postgres.py` are the adapters; `registry.py` maps `type` → adapter. A third
  engine is a new file here plus a `DATA_SOURCES` entry in the frontend.
- **The context model** — curated markdown describing what the data *means*,
  which is the thing agents lack far more often than they lack schema.
  `memory/datacontext.py` owns storage (a per-org tree of `.md` files: an
  `ontology/` keyed by *business entity*, `playbooks/` by analysis pattern, and
  `overview.md`) plus the process cache; `agent/datacontext.py` is the
  generation agent, which runs on a **stronger model** (`OPENAI_DOCS_MODEL`).
  Injected in two tiers, mirroring `build_catalog`/`describe_table`: the *tree*
  (path + one-line summary) goes into every catalog-bearing prompt, and bodies
  are fetched on demand via the `read_context` tool. **Never put a body in the
  tree** — that recreates the context pressure the feature exists to relieve.
- `warehouse/catalog.py` — schema discovery across *all* of an org's sources,
  introspected concurrently and cached per `(org_id, source fingerprint)`.
  `build_catalog()` renders column names only (it goes in every prompt);
  `describe_table()` fetches types and samples on demand. A source that fails
  renders `UNAVAILABLE` rather than breaking the report.
- `context.py` — `TenantContext`, the **only** handle to a warehouse or OpenAI
  client. Carries `sources: tuple[SourceRef, ...]`; `ctx.warehouse(name)`
  resolves one, `None` meaning the default. Frozen and session-free, so it is
  safe to hand to a worker thread. There are no zero-argument client singletons:
  forgetting to thread a context is a `TypeError`, not a silent cross-tenant read.
- `clients.py` — client registries keyed by connection *fingerprint* (which
  includes `type`), so changing credentials invalidates the client for free and
  two engines on one host:port cannot collide.
- `db/models.py` — Postgres schema. `OrgWarehouseConnection` is one data source;
  its `name` is the handle the LLM types and its `description` is how the model
  routes a question to the right source. Every content table carries `org_id`;
  `qa_turns` has a composite FK on `(report_id, org_id)` making a cross-org Q&A
  turn structurally impossible.
- `memory/store.py` — **Postgres via SQLAlchemy**, org-scoped: suggestions
  (retrieval-augmented memory), reports, qa_turns, dashboards. Every read goes
  through `_scoped()`; the caller owns the transaction (the store never commits).
- `migrations/` — Alembic. Schema changes are revisions, not runtime DDL.
- `auth/` — `passwords` (argon2), `sessions` (server-side, opaque cookie; not
  JWT, so revocation is immediate), `orgs` (membership + `build_tenant_context`).
- `security/crypto.py` — Fernet with key rotation. Source passwords are
  encrypted by the `EncryptedStr` column type, so no code path writes plaintext.
- `web/app.py` — streaming NDJSON endpoints. `web/deps.py` — request-scoped
  guards. `web/routes_auth.py`, `web/routes_orgs.py`.
- `llm/prompts.py` — all system prompts.
- `frontend/` — Next.js App Router + shadcn/ui. Two fetch sites only
  (`lib/api/client.ts`, `lib/api/stream.ts`); both send `credentials: "include"`.

## Conventions
- Every agent carries the anti-fabrication rule (`_ANTI_FABRICATION`).
- Agents return structured data; parse LLM JSON with `blocks.parse_json_object`.
- Any value serialized for the wire/DB uses `json.dumps(..., default=str)`
  (ClickHouse rows can hold datetimes/Decimals).
- Add tests alongside changes; agent tests use a scripted fake OpenAI client
  (no live DB/API) — see `tests/test_report_agent.py`.

### Multi-tenancy rules — the ones worth not rediscovering
- **Auth is structural.** Every app endpoint hangs off the `api` router in
  `web/app.py`, which carries `Depends(get_current_user)` and
  `Depends(csrf_guard)`. A new route there cannot forget them, and
  `tests/test_auth_web.py` walks `/openapi.json` to prove it.
- **Never fall back to the env warehouse.** `CLICKHOUSE_*` in `.env` is the
  *deployment's* warehouse, not any org's. An org with no stored source has an
  empty `ctx.sources`, so `has_connection` is false and there is literally
  nothing for `ctx.warehouse()` to resolve — it raises `NoConnectionError` →
  409 `no_connection`. The fallback is absent structurally, not by convention.
  Endpoints that reach a warehouse depend on `require_connection`; the
  Postgres-only readers deliberately do not, so a new org can still load its
  empty library and reach settings.
- **Cross-org reads return 404, not 403** — never confirm another org's
  existence. Store mutations return `False` rather than raising, and the
  endpoint turns that into a 404.
- Errors use stable machine codes in `detail` (`not_authenticated`, `no_org`,
  `forbidden`, `no_connection`) — see the docstring at the top of `web/deps.py`.
  The frontend maps them to wording in one place (`lib/api/client.ts`).
- Secrets never leave the server: `to_public_dict()` returns `has_password: bool`,
  and neither `__repr__` nor `WarehouseSpec.__repr__` renders the password.

### Multi-source rules
- **A statement targets one source.** There is no cross-source SQL and no join
  engine; the Analyst queries each warehouse separately and the Reporter relates
  the resulting datasets across blocks. The catalog states this rule explicitly,
  because a model that does not hear it writes the join.
- **Reports are not pinned to a source.** Provenance lives per captured query in
  `reports.queries[].source`, and Q&A re-reads the whole catalog, so a follow-up
  can reach any source the org has.
- **Prompts are engine-neutral.** No prompt in `llm/prompts.py` names a dialect;
  the per-engine hint comes from `Dialect.prompt_hint` via the catalog, built
  from the sources the org actually has.
- **Postgres sources are read-only at the server.** Every session is opened
  `read_only`, so a hole in the SQL tokenizer still cannot write. ClickHouse has
  no equivalent — point `CLICKHOUSE_USER` at a read-only user.
- One default source per org, enforced by a partial unique index. Promote by
  demoting the others and flushing first, or the index fires mid-transaction
  (`_set_default` in `web/routes_orgs.py`). Deleting the default promotes
  another, so an unqualified query always resolves.

### Context-model rules
- **The tree is always on; bodies never are.** `ContextModel.render_tree()`
  emits paths and summaries only. An org with no context model gets a
  byte-identical `build_catalog()` to before the feature existed.
- **It is loaded on the request thread, read on the worker thread.**
  `read_context` runs inside `run_capture_loop`, which has no session, so the
  whole model is materialized as a frozen `ContextModel` in
  `build_tenant_context` and carried on `TenantContext` — the same reason
  `sources` is. Never reach for a `Session` from `agent/` or `warehouse/`.
- **`generated_body_md` is a merge base, not a backup.** `body_md` differing
  from it means a human owns the file. A `revise` run *shows the model* the
  human's body and advances both together, so edit preservation is prompt
  input, not a post-hoc merge. A file whose `origin` is not `agent` is never
  written by the generator at all.
- **The docs model and its client travel together.** `run_capture_loop` takes
  `model=` and `openai=` as a pair: with `OPENAI_DOCS_BASE_URL` pointing at a
  second provider, overriding the model name alone sends it to the wrong
  endpoint.
- **Paths are validated in the database** (`ck_dcfile_path`), not just in
  Pydantic — they become filenames under the planned git sync, so a traversal
  must be unrepresentable rather than merely rejected.
- The context model is written by an LLM and lands in every prompt: keep its own
  fence, keep `SQL RULES` and `_ANTI_FABRICATION` rendered *after* it, and keep
  the length caps. It cannot cause a write, but it can steer the Analyst.
