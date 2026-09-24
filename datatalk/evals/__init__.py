"""Offline evaluation of DataTalk's agents against a seeded, known warehouse.

The problem this package exists to solve: every other test in this repo drives
the agents with a **scripted** OpenAI client, which proves the plumbing and
proves nothing about whether a real model writes correct SQL. That question --
"how often does the agent actually get the right number?" -- has no answer
anywhere else in the codebase, and it is the only question a user of this
product cares about.

The shape, borrowed from Spider/BIRD:

1. Seed a **deterministic synthetic business** into real warehouses
   (:mod:`datatalk.evals.dataset`, :mod:`datatalk.evals.seed`). Two sources,
   because multi-source routing is a claim this project makes and a
   single-source suite cannot measure it.
2. Each golden case carries a hand-written **reference query**, not a
   hand-pinned answer. The reference is executed at eval time against the same
   seeded data, so the expectation cannot drift from the fixture
   (:mod:`datatalk.evals.cases`).
3. Score by **execution match** -- does any dataset the agent captured contain
   the reference relation? -- never by comparing SQL text. There are many
   correct queries for one question and exactly one correct answer
   (:mod:`datatalk.evals.scoring`).
4. Drive the **production agents through their production prompts**
   (:mod:`datatalk.evals.runner`). A harness that seeds its own system prompt
   measures a prompt nobody ships.

The headline experiment is the A/B in :mod:`datatalk.evals.runner`: the same
suite run with and without the workspace context model, which is the feature
this product is built around and the one thing an accuracy number can actually
prove.

Nothing here is imported by the application. The suite costs real tokens, so it
is a CLI (``datatalk-eval``), never part of ``pytest`` -- what pytest covers is
the scorer and the runner wiring, against the usual fakes.
"""

from datatalk.evals.cases import Case, Expectation, ReferenceStep, load_suite
from datatalk.evals.scoring import (
    MatchOutcome,
    Relation,
    execution_match,
    normalize_value,
)

__all__ = [
    "Case",
    "Expectation",
    "ReferenceStep",
    "load_suite",
    "MatchOutcome",
    "Relation",
    "execution_match",
    "normalize_value",
]
