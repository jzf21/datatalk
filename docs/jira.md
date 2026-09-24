# Jira as a data source

A workspace can add a **Jira Cloud** site as a data source. After that, reports, Q&A and dashboards can chart
throughput, cycle time, sprint carry-over, workload and time logged, and can put those numbers next to
warehouse data in the same report.

Jira is **synced, not queried live.** Everything downstream of the agent assumes a SQL source:
- captured datasets
- `materialize()`
- dashboard refresh (which re-executes `queries[].sql`)
- filter binding
- eval scoring

So a sync job copies Jira into Postgres, and from then on Jira is one more read-only SQL source. None of
that machinery knows Jira exists. The cost is freshness: numbers are as current as the last sync, and the
Settings page states that age wherever the source is listed.

```
Jira Cloud ──(REST, email + API token)──▶ datatalk-sync / "Sync now"
                                             │ writes, as the store's owner
                                             ▼
                          sync store (separate Postgres database)
                            jira_3fa2b1c4d5e6.issues, .status_changes, …
                                             ▲ reads, as dt_src_3fa2b1c4d5e6
                                             │ (read-only, this schema only)
                                  Analyst / QA / dashboard refresh
```

## Setup

1. **Create the sync store.** This is a separate database, never the app database. On the dev container:

   ```bash
   docker exec datatalk-postgres psql -U datatalk -c "CREATE DATABASE datatalk_sync OWNER datatalk"
   ```

   New volumes create it automatically (`scripts/init-test-db.sql`).

2. **Point the app at it** in `.env`:

   ```bash
   DATATALK_SYNC_DATABASE_URL=postgresql+psycopg://datatalk:datatalk@localhost:5433/datatalk_sync
   ```

   The role in this URL must own the database and hold `CREATEROLE`, because it creates one login role per
   Jira source.

3. **Migrate:** `datatalk-db upgrade`. Revision `0007` admits `type = 'jira'` and adds `source_sync_state`.

4. **Add the source** under **Settings → Data sources → Add source → Jira** with:
   - **Site**, for example `acme.atlassian.net`. A pasted URL is fine.
   - **Account email** and **API token.** Create the token at id.atlassian.com → Security → API tokens.
     Consider a dedicated service account with read access to the relevant projects.
   - **Scope (JQL)**, optional, for example `project in (ABC, DEF)`. Its own `ORDER BY` is ignored.

   Use **Test connection** to confirm the sign-in and see roughly how many issues the scope covers.
   Saving starts the first sync automatically.

5. **Keep it fresh** with cron. There is no in-process scheduler, because every uvicorn worker would run it:

   ```cron
   */15 * * * *  cd /srv/datatalk && .venv/bin/datatalk-sync          >> /var/log/datatalk-sync.log 2>&1
   0 3 * * *     cd /srv/datatalk && .venv/bin/datatalk-sync --full   >> /var/log/datatalk-sync.log 2>&1
   ```

   An advisory lock in the sync store serializes runs per source. A cron run that overlaps a **Sync now**
   click is skipped rather than doubled. `datatalk-sync` exits 1 if any source failed.

## What gets synced

| Table | One row per | Use it for |
|---|---|---|
| `issues` | issue (current state) | counts, WIP, backlog by status category / assignee / label / epic, story points |
| `status_changes` | status transition (from the changelog) | cycle time, lead time, throughput, time in status |
| `sprints`, `issue_sprints` | sprint; issue×sprint (current membership) | which sprints an issue is in now |
| `sprint_events` | issue entering or leaving a sprint | committed scope, scope added/removed, carry-over, burndown |
| `field_changes` | change to story points, assignee, priority or type | the estimate (or owner) an issue had at a past time |
| `boards` | Jira Software board | board filters; empty without Jira Software access |
| `worklogs` | worklog | time logged by person / project / week |
| `users`, `projects` | account; project | display names. **No email addresses are synced** |
| `_sync_meta` | — | `synced_at`: when this copy was refreshed; `time_zone`: the site's zone |

**History is replayed, not stored as snapshots.** Jira keeps only an issue's current sprint list and
estimate. The sync keeps the changelog items for them instead. An issue created straight into a sprint has
no changelog item for it, so the sync records it as `added` at the issue's creation time. A sprint's scope at
time T is then the issues whose latest `sprint_events` row at or before T is `added`. Their estimate then is
the `to_value` of the last `story_points` change at or before T, else the `from_value` of the first change
after T, else the current value.

**Boards** come from the Agile API (`/rest/agile/1.0/board`). It exists only with Jira Software and answers
only accounts with board access, so a 403/404 there is not a failed sync: `boards` stays empty,
`stats.boards_available` is false, and sprints keep the `boardId` from the sprint field.

Table and column comments explain what the data means. For example, `status_category` is always `To Do`,
`In Progress` or `Done`, and `story_points` is NULL when unestimated. The Postgres adapter reads those
comments into the catalog and `describe_source`, so the model sees them. The context model
(**Settings → Data context → Generate**) can document the rest.

Story points and sprints are custom fields. The sync finds them by meaning: a field named
"Story Points"/"Story point estimate", and the `gh-sprint` custom type. A site that uses neither just gets
NULLs.

## How a sync works

- **Incremental** (the default): it fetches issues whose `updated` value is at or after the cursor minus a
  2-minute overlap. JQL dates have minute precision and use the account's timezone, and upserts are
  idempotent, so re-reading costs little and skipping would be a silent gap. Each re-fetched issue replaces
  its own rows and its child rows, so a transition or worklog deleted in Jira disappears here too.
- **Full** (`--full`, or **Full resync**): it re-reads everything, deletes issues it did not see, and
  prunes projects, sprints and users that no issue references any more. This is the only way to notice an
  issue deleted in Jira. The first sync, a changed site, account or scope, and a table-shape upgrade
  (`SCHEMA_VERSION`) all force a full sync.
- Each page of 100 issues is written in its own transaction. The cursor only advances when a run completes,
  so after a crash the next run re-reads from the old cursor instead of skipping.
- A 429 from Jira is retried after its `Retry-After` delay, a bounded number of times.
- Every sync first re-asserts the source's schema, role and grants (idempotent). Drift and half-finished
  deletes are repaired by the next sync rather than by hand.

## Isolation and secrets

- **The agent never holds the Jira token.** A Jira row resolves to a `WarehouseSpec` that points at the
  sync store and uses that source's own login role. Only the sync job ever reads the site and token.
- **The server enforces tenant isolation.** Each role gets `CONNECT`, `USAGE` on its own schema, and
  `SELECT` on its tables. It is created `NOINHERIT`, with `default_transaction_read_only`, and the adapter
  opens every session `read_only` as well. Our code doesn't have to be right for this to hold. A model that
  types another org's schema name gets `permission denied`. The sync store also revokes the `public` schema
  and `TEMPORARY` from `PUBLIC`, so a source role has nowhere to write.
- **Only the host allowlist permits outbound requests.** The server calls whatever site an admin types, so
  the site must end in an allowed suffix. The default is `.atlassian.net`; set
  `DATATALK_JIRA_ALLOWED_HOST_SUFFIXES` to widen it. Ports, userinfo and IP literals are refused, and
  redirects are not followed.
- Schema and role names are derived from the connection id. A database `CHECK` also constrains their
  shape, because they end up as identifiers in DDL.

**If the sync store shares a server with the app database**, as it does in dev, Postgres grants `CONNECT`
to `PUBLIC` by default, so a source role can *connect* to the app database. It still can't *read*
anything there: the app tables grant `PUBLIC` nothing, and `tests/test_jira_sync.py` pins that. For a
stricter boundary, do one of these:

- Run the sync store on its own server.
- Run `REVOKE CONNECT ON DATABASE datatalk FROM PUBLIC` on the app database.
- Restrict `dt_src_*` roles to the sync database in `pg_hba.conf`.

## Operations

```bash
datatalk-sync                           # every Jira source, incrementally
datatalk-sync --org acme --source jira  # one source
datatalk-sync --full                    # everything, noticing deletions
datatalk-sync --gc --dry-run            # schemas no source owns any more
datatalk-sync --gc                      # ...dropped, with their roles
```

Deleting a source drops its schema and role on a best-effort basis. If the store is unreachable at that
moment the delete still succeeds, and `--gc` removes the leftovers later.

## Report templates

A Jira source unlocks five ready-made dashboards on **Dashboards → Or start from a Jira report**: Sprint
report, Velocity, Flow metrics, Backlog & delivery, and People & effort. They are hand-written SQL
(`datatalk/dashboards/templates/jira/`), not generated. Building one runs no LLM. It saves an ordinary
dashboard, and that dashboard refreshes through the usual path. The definitions they use are written at the
top of each module (for example, *committed* means in the sprint when it started, at its estimate then).

Their controls are Jira-native: project, board, **sprint** (active, last N, or specific sprints), issue type,
priority, assignee (including *Unassigned*), epic, label, component and fix version. Each widget is wired
to the controls its query can express. **Edit layout** can unwire a control from one widget, or pin it to
one value ("always Bugs"). The response's `unwired` map names widgets that ignore a control on purpose.

## Not in v1

Jira Data Center / Server (PAT auth), OAuth 3LO, webhooks for near-real-time updates, UI mapping for other
custom fields, and a Jira eval suite.
