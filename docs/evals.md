# Evaluating the agents

Every other test in this repo drives the agents with a scripted OpenAI client.
That proves the plumbing and proves nothing about the only question a user of
this product has: **how often does it get the right number?**

`datatalk-eval` answers that. It seeds a deterministic synthetic business into
two real warehouses, asks the production agents twenty questions with known
answers, and scores what comes back by execution match.

```bash
docker compose up -d                 # Postgres on :5433
pip install -e ".[dev]"

datatalk-eval seed                   # load the fixture (~19k rows, a few seconds)
datatalk-eval validate               # execute every reference query — no model calls
datatalk-eval run --out evals/latest.json
```

`validate` before `run`, always. `run` spends real tokens against whatever
endpoint your `.env` names, and a reference query that errors is a broken case
you want to find for free.

---

## What is measured

| Metric | Question it answers |
|---|---|
| **Execution accuracy** | Did a dataset the agent captured contain the right answer? |
| **Source routing** | Did it query *exactly* the sources the question needs? |
| **Answer fidelity** | Among runs that fetched the right rows, did the prose carry the right numbers? |
| **Consistency** | With `--repeat`, did the same case pass every time? |
| Steps · queries · failed queries · tokens · p50/p95 latency | What it cost to get there. |

They are reported side by side and never averaged into one score. Execution
accuracy alone would reward an agent that captures forty datasets hoping one
lands; next to the query count, it cannot. Answer fidelity alone would look like
honesty when it is really an agent narrating the wrong rows faithfully — so it
is scored only over the cases that already passed execution match.

### The headline experiment

By default `run` executes the suite **twice**: once with the workspace context
model loaded and once without, on identical data, prompts, model and
temperature. The only variable is the curated documentation.

That A/B is the point. DataTalk's thesis is that an agent's missing ingredient
is *meaning*, not schema — that it fails on "revenue" because nobody told it
cancelled orders do not count, not because it cannot write a `JOIN`. Six of the
twenty cases are tagged `definition` and turn on exactly such a rule. The
difference between the two arms is the closest thing to a measurement of whether
the thesis holds.

Run one arm with `--arm with-context` or `--arm no-context`.

---

## How scoring works

Scored by **execution match**, never by comparing SQL text. A question has many
correct queries and one correct answer; a string comparison would mark a better
query wrong for being different, and a familiar-looking query right for
returning nonsense. This is the standard from Spider and BIRD.

**Forgiven** — column names and aliases, column order, extra columns, row order
(unless the case says the ranking is the answer), and every representational
difference between the engines: `Decimal` vs. the string ClickHouse returns for
one, a `DATE` arriving as a midnight `datetime`, thousands separators.

**Not forgiven** — a wrong value outside the case's tolerance, a different row
count, or a missing column. Eighteen rows where six were asked for is a
different answer, not a generous one.

**Every captured dataset is a candidate.** The loop captures exploratory queries
too, and finding the answer on the third try is still finding it.

### Ground truth cannot drift

A case stores a **reference query**, not a reference answer:

```json
{
  "id": "revenue-by-category-q1",
  "question": "What was total revenue by product category in Q1 2025?",
  "tags": ["aggregation", "join", "time-window"],
  "expect": {
    "sources": ["sales"],
    "reference": {"source": "sales", "sql": "SELECT p.category, ROUND(SUM(...), 2) ..."}
  }
}
```

The reference runs at eval time against the same seeded data, through the same
read-only executor the agent uses. Pinning literal expected values would create
a second copy of the fixture to keep in step with the first, and getting that
wrong is invisible — the stale numbers still look like numbers.

A **cross-source** case declares one reference step per source plus a `combine`
query, which runs over the step results in an in-memory SQLite database:

```json
"reference": [
  {"source": "sales",  "as": "orders_by_channel", "sql": "SELECT channel, COUNT(DISTINCT order_id) AS orders ..."},
  {"source": "events", "as": "spend_by_channel",  "sql": "SELECT channel, SUM(amount) AS spend ..."}
],
"combine": "SELECT o.channel, ROUND(s.spend / o.orders, 2) FROM orders_by_channel o JOIN spend_by_channel s ON ..."
```

That is deliberately the same shape as what the product does — query each
warehouse, relate the results outside the engine. Ground truth for a
cross-source question is computed the way the product computes it, never by a
join the product could not write.

---

## The fixture

A synthetic direct-to-consumer electronics business, split across two sources
because a single-source suite cannot score routing:

| Source | Tables |
|---|---|
| `sales` | `customers` · `products` · `orders` · `order_items` · `refunds` |
| `events` | `page_views` · `marketing_spend` |

Keeping `marketing_spend` away from `orders` is what makes "cost per order by
channel" a genuine two-warehouse question rather than a join.

**It is deterministic and it stays that way.** The generator uses a Park-Miller
LCG the repo owns rather than `random`, whose derived helpers carry no
cross-version reproducibility promise. A SHA-256 of every generated cell is
pinned in `datatalk/evals/dataset.py`, and `seed` verifies it before writing a
row. Change a weight and you get a hard error naming the old and new
fingerprints — because every reference answer in the suite is a function of
those exact bytes, and accuracy numbers from either side of such a change are
not comparable.

**Every window is an absolute date.** The data covers 2025-01-01 to 2025-06-30
and the loader *rejects* a question containing "last month", "this quarter",
"YTD" and friends. A relative case answers differently every quarter, and
nothing about the resulting decline would point back at the suite.

Expectations avoid month and date *labels* as matched columns: `2025-03`,
`2025-03-01` and `March` are all correct answers to the same question, and a
scorer insisting on one would be measuring presentation. Temporal cases project
the measure only and enforce chronology through ordering instead.

---

## Running it

```bash
# The full default run: 20 cases x 2 arms = 40 agent runs
datatalk-eval run --out evals/latest.json

# While iterating on one case
datatalk-eval run --case net-revenue-may --arm with-context

# Just the cases that turn on a business definition
datatalk-eval run --tag definition

# The end-to-end pipeline instead of the Analyst alone
datatalk-eval run --task report

# Stability, not accuracy: does the same case pass three times running?
datatalk-eval run --repeat 3 --arm with-context

# As a merge gate
datatalk-eval run --arm with-context --min-accuracy 0.75
```

`--out` writes the JSON record (every per-case field, including the failure
reason) and a Markdown summary beside it.

### `--task analyst` vs `--task report`

`analyst` (the default) hands the question to the real Analyst as a one-section
plan and skips the Planner. It isolates SQL generation, so a drop points at the
Analyst or the catalog rather than at three agents at once.

`report` runs the whole Planner → Analyst → Reporter pipeline. `ReportResult`
carries the materialized document and the query *log* rather than the rows, so
the harness recovers the datasets by re-executing the recorded SQL — exactly
what `dashboards/refresh.py` does to refresh a live dashboard. Same statement,
deterministic fixture, same rows.

### Cross-engine mode

```bash
docker compose --profile evals up -d
datatalk-eval seed --events-engine clickhouse
datatalk-eval run --events-engine clickhouse
```

This moves `events` onto ClickHouse, so a single cross-source question requires
the agent to write two different dialects and send each to the source that
speaks it. Routing accuracy only becomes a strong claim in this mode.

---

## What CI runs, and what it does not

`pytest` covers the **scorer and the wiring** — `tests/test_evals_scoring.py`,
`test_evals_suite.py`, `test_evals_runner.py` — against the same fakes as every
other agent test. No warehouse, no model, no spend. Those tests exist because
the scorer is the piece that can be wrong *silently*: a matcher that is a little
too generous just reports a higher number, and nothing in the output says so.

The suite itself is a CLI and never a pytest module. A test suite that silently
spends money, or fails because someone's API key is missing, is one people learn
to skip. Wire `datatalk-eval run --min-accuracy` into a scheduled or
manually-dispatched job instead, where the cost is deliberate.

---

## Adding a case

1. Write the question with an **absolute** date window.
2. Write the reference query by hand and check it against the fixture. Keep the
   result small — under ~100 rows, so the executor's row cap cannot truncate a
   candidate and turn a correct answer into a failure.
3. Tag it. `definition` is the one that matters most: it marks a case whose
   correct answer exists only in the context model, and those cases are what the
   A/B is really measuring.
4. Give it a `sql_clickhouse` form if it reads the `events` source.
5. `datatalk-eval validate` — it re-executes every reference and flags any that
   error, return nothing, return too much, or disagree with the case's declared
   `match` mode.

A case is a claim about what the product should get right. Prefer questions a
competent analyst would answer the same way every time; ambiguity in the
question shows up as noise in the number, and noise is what stops a suite
detecting the regression it exists to catch.
