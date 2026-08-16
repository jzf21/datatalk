<!--
BASELINE. Committed as the number to diff against; regenerate with

    datatalk-eval run --repeat 3 --out evals/baseline.json

Two things that will move this number, so that a future diff is not mistaken
for a regression:

1. `CASE ... END` is currently REJECTED on Postgres sources. The Postgres
   dialect forbids `END` as transaction control (it is a synonym for COMMIT),
   which also catches the terminator of every CASE expression; ClickHouse
   accepts the same statement. Several attempts below lose a turn to it
   (`mom-growth-h1`, `mobile-share-q2`). Fixing that guardrail should RAISE
   accuracy, particularly on `ratio` and `window-function`.
2. The reference queries encode this workspace's own definitions, so the
   no-context arm fails `definition` cases by construction. See the caveat in
   docs/evals.md -- the gap measures what written-down definitions are worth,
   not SQL ability in the abstract.
-->

# DataTalk eval — `retail` / `analyst`

- Model: `openai/gpt-oss-120b`
- Started: 2026-08-16T08:37:04+00:00  ·  Wall clock: 95s
- Fixture: `9373ad249491…`

## Results

| Metric | with-context | no-context |
|---|---|---|
| **Execution accuracy** | 86.7% | 33.3% |
| &nbsp;&nbsp;passed / attempted | 52 / 60 | 20 / 60 |
| Source routing | 100.0% | 100.0% |
| Answer fidelity | n/a | n/a |
| Consistency | 95.0% | 95.0% |
| Run errors | 0.0% | 0.0% |
| Rejected (unsafe) SQL | 3 | 4 |
| Mean steps | 3.7 | 2.7 |
| Mean queries | 1.2 | 1.2 |
| Mean failed queries | 0.1 | 0.1 |
| Mean tokens / case | 10,912 | 4,930 |
| Latency p50 / p95 | 3s / 7s | 2s / 6s |

**Context model: 33.3% → 86.7%** (+53.3 points) on identical data, prompts and model.

## By question type

| Tag | with-context | no-context |
|---|---|---|
| `aggregation` | 36/36 | 14/36 |
| `definition` | 15/15 | 2/15 |
| `filter` | 12/12 | 6/12 |
| `join` | 18/18 | 3/18 |
| `multi-source` | 3/9 | 0/9 |
| `ranking` | 6/9 | 0/9 |
| `ratio` | 13/18 | 6/18 |
| `routing` | 12/18 | 12/18 |
| `time-window` | 13/15 | 3/15 |
| `window-function` | 1/3 | 0/3 |

## Failures

**with-context** — 8 failing attempt(s):

- `cost-per-order-by-channel-h1` — q1: no captured column holds the values of reference column 'cost_per_order'; q2: no captured column holds the values of reference column 'cost_per_order'
- `most-viewed-product-h1` — q1: no captured column holds the values of reference column 'name'; q2: no captured column holds the values of reference column 'views'
- `cost-per-order-by-channel-h1` — q1: no captured column holds the values of reference column 'cost_per_order'; q2: no captured column holds the values of reference column 'cost_per_order'
- `most-viewed-product-h1` — q1: no captured column holds the values of reference column 'name'; q2: no captured column holds the values of reference column 'views'
- `mom-growth-h1` — q1: no captured column holds the values of reference column 'growth_pct'; q2: 1 rows, reference has 5; q3: no captured column holds the values of reference column 'growth_pct'
- `cost-per-order-by-channel-h1` — q1: no captured column holds the values of reference column 'cost_per_order'; q2: no captured column holds the values of reference column 'cost_per_order'
- `most-viewed-product-h1` — q1: no captured column holds the values of reference column 'name'; q2: no captured column holds the values of reference column 'views'
- `mom-growth-h1` — q1: no captured column holds the values of reference column 'growth_pct'

**no-context** — 40 failing attempt(s):

- `revenue-by-category-q1` — q1: no captured column holds the values of reference column 'revenue'
- `order-count-march` — q1: the value is not in the captured row
- `top-5-products-revenue-h1` — q1: no captured column holds the values of reference column 'revenue'
- `aov-by-segment-q2` — q1: no captured column holds the values of reference column 'aov'
- `net-revenue-may` — q1: the value is not in the captured row
- `monthly-revenue-h1` — q1: no captured column holds the values of reference column 'revenue'
- `revenue-by-country-q1` — q1: no captured column holds the values of reference column 'revenue'
- `enterprise-top-category-h1` — q1: no captured column holds the values of reference column 'revenue'
- `total-discount-h1` — q1: the value is not in the captured row
- `cost-per-order-by-channel-h1` — q1: no captured column holds the values of reference column 'cost_per_order'; q2: no captured column holds the values of reference column 'cost_per_order'
- `roas-paid-search-q2` — q1: the value is not in the captured row; q2: the value is not in the captured row
- `most-viewed-product-h1` — q1: no captured column holds the values of reference column 'name'; q2: no captured column holds the values of reference column 'views'
- `mom-growth-h1` — q1: no captured column holds the values of reference column 'growth_pct'
- `revenue-by-category-q1` — q1: no captured column holds the values of reference column 'revenue'
- `order-count-march` — q1: the value is not in the captured row
- `top-5-products-revenue-h1` — q1: no captured column holds the values of reference column 'revenue'
- `aov-by-segment-q2` — q1: no captured column holds the values of reference column 'aov'
- `net-revenue-may` — q1: the value is not in the captured row; q2: the value is not in the captured row
- `monthly-revenue-h1` — q1: no captured column holds the values of reference column 'revenue'
- `revenue-by-country-q1` — q1: no captured column holds the values of reference column 'revenue'
- …and 20 more (see the JSON).

## Statements the guardrails refused

Not a scoring problem — the executor is doing its job. Worth reading anyway: each one is a statement the agent believed was a reasonable way to answer the question.

- `cancelled-share-h1` [with-context] — Rejected SQL: Forbidden keyword(s) present: END.
- `mobile-share-q2` [with-context] — Rejected SQL: Forbidden keyword(s) present: END.
- `mom-growth-h1` [with-context] — Rejected SQL: Forbidden keyword(s) present: END.
- `cancelled-share-h1` [no-context] — Rejected SQL: Forbidden keyword(s) present: END.
- `mom-growth-h1` [no-context] — Rejected SQL: Forbidden keyword(s) present: END.
- `cancelled-share-h1` [no-context] — Rejected SQL: Forbidden keyword(s) present: END.
- `cancelled-share-h1` [no-context] — Rejected SQL: Forbidden keyword(s) present: END.

## How to read this

- **Execution accuracy** — a dataset the agent captured contains the reference answer. Column names and ordering are forgiven; row counts and values are not.
- **Source routing** — the agent queried exactly the sources the question needs. Querying a source that cannot contribute is a failure, not a harmless extra.
- **Answer fidelity** — among the runs that fetched the right rows, the prose carried the right numbers. `n/a` for the `analyst` task, which is told to stop without restating anything.
- **Mean queries** — read alongside accuracy. An agent that captures everything will eventually capture the answer.