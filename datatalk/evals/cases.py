"""Golden cases: a question, and how to compute the right answer from the data.

The central decision here is that a case stores a **reference query**, never a
reference answer. Pinning literal expected values would make the suite a second
copy of the fixture that has to be maintained in step with the first, and the
failure mode of getting that wrong is invisible: the numbers still look like
numbers. Executing the reference against the same seeded warehouse at eval time
means the expectation cannot drift from the data by construction.

A case that spans both sources declares one reference step per source plus a
``combine`` query, which runs over the step results in an in-memory SQLite
database (stdlib -- no dependency). That is deliberately the same shape as what
the product does: query each warehouse separately, relate the results outside
the engine. Ground truth for a cross-source question is computed the way the
product computes it, not by a join the product could never write.

Cases are JSON, not YAML: this repo has no YAML dependency and one eval suite is
a poor reason to add a parser to the install of every user.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

SUITES_DIR = Path(__file__).parent / "suites"

# Phrases whose meaning depends on when the suite is run. A case containing one
# has a correct answer today and a wrong one next quarter, and nothing about the
# resulting accuracy drop would point back here -- so they are rejected at load
# time rather than reviewed by hand.
_RELATIVE_TIME = re.compile(
    r"\b(last|next|this|past|previous|current|recent|so far)\s+"
    r"(week|month|quarter|year|30\s*days|90\s*days)\b"
    r"|\b(today|yesterday|tomorrow|ytd|year to date|month to date|mtd)\b",
    re.IGNORECASE,
)


class SuiteError(ValueError):
    """A malformed suite file. Raised at load time, never mid-run."""


@dataclass(frozen=True)
class ReferenceStep:
    """One reference query against one source.

    ``as_`` names the SQLite table the rows land in when a ``combine`` query
    relates several steps; it is unused for a single-step case.

    ``sql_clickhouse`` is the same query in the other dialect, for a step whose
    source can be either engine. The reference must be *hand-written per
    engine* for the same reason ``warehouse/`` keeps one adapter per engine and
    no translation layer: a query rewritten mechanically is a query nobody
    checked, and here it would be defining the right answer.
    """

    source: str
    sql: str
    as_: str = "result"
    sql_clickhouse: str = ""

    def sql_for(self, engine: str) -> str:
        if engine == "clickhouse":
            if not self.sql_clickhouse:
                raise SuiteError(
                    f"Reference step against source {self.source!r} has no "
                    "ClickHouse form, but that source is running on ClickHouse. "
                    "Add 'sql_clickhouse' to the step."
                )
            return self.sql_clickhouse
        return self.sql


@dataclass(frozen=True)
class Expectation:
    """How to compute the right answer, and what counts as getting it."""

    reference: tuple[ReferenceStep, ...]
    combine: str = ""
    # "rows"   -- the reference relation must be embeddable in some captured
    #             dataset (extra columns and row order are forgiven).
    # "scalar" -- the reference is one value; a captured dataset must be a
    #             single row containing it.
    match: str = "rows"
    tolerance: float = 0.01
    order_sensitive: bool = False
    # Sources the agent is expected to query. Routing is scored against this.
    sources: tuple[str, ...] = ()
    # Values that must survive into the written answer. Optional, and scored
    # separately from execution match: an agent can fetch the right rows and
    # still narrate the wrong number, and conflating the two would hide it.
    answer_contains: tuple[str, ...] = ()

    @property
    def is_multi_source(self) -> bool:
        return len({s.source for s in self.reference}) > 1


@dataclass(frozen=True)
class Case:
    id: str
    question: str
    expect: Expectation
    tags: tuple[str, ...] = ()
    notes: str = ""


@dataclass
class Suite:
    name: str
    cases: list[Case] = field(default_factory=list)

    def __iter__(self):
        return iter(self.cases)

    def __len__(self) -> int:
        return len(self.cases)

    def filtered(self, ids: Sequence[str] = (), tags: Sequence[str] = ()) -> "Suite":
        cases = self.cases
        if ids:
            wanted = set(ids)
            cases = [c for c in cases if c.id in wanted]
        if tags:
            wanted = set(tags)
            cases = [c for c in cases if wanted & set(c.tags)]
        return Suite(name=self.name, cases=cases)


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise SuiteError(message)


def parse_case(raw: dict[str, Any], *, where: str = "") -> Case:
    prefix = f"{where}: " if where else ""
    case_id = str(raw.get("id") or "").strip()
    _require(bool(case_id), f"{prefix}case is missing an id")
    question = str(raw.get("question") or "").strip()
    _require(bool(question), f"{prefix}{case_id}: case is missing a question")
    # Computed before the message, not inside it: `_require`'s argument is
    # evaluated eagerly, so a `.group()` on the non-match would raise on every
    # well-formed case.
    relative = _RELATIVE_TIME.search(question)
    _require(
        relative is None,
        f"{prefix}{case_id}: the question uses a relative time window "
        f"({relative.group(0)!r} in {question!r}). Anchor it to an absolute "
        "date -- a relative case silently changes answer with the calendar."
        if relative
        else "",
    )

    raw_expect = raw.get("expect")
    _require(isinstance(raw_expect, dict), f"{prefix}{case_id}: missing 'expect'")

    raw_ref = raw_expect.get("reference")
    if isinstance(raw_ref, dict):
        raw_ref = [raw_ref]
    _require(
        isinstance(raw_ref, list) and bool(raw_ref),
        f"{prefix}{case_id}: 'expect.reference' must be a step or a list of steps",
    )
    steps = []
    for i, step in enumerate(raw_ref):
        _require(
            isinstance(step, dict) and step.get("source") and step.get("sql"),
            f"{prefix}{case_id}: reference step {i} needs 'source' and 'sql'",
        )
        steps.append(
            ReferenceStep(
                source=str(step["source"]),
                sql=str(step["sql"]).strip(),
                as_=str(step.get("as") or f"step{i + 1}"),
                sql_clickhouse=str(step.get("sql_clickhouse") or "").strip(),
            )
        )

    combine = str(raw_expect.get("combine") or "").strip()
    _require(
        len(steps) == 1 or bool(combine),
        f"{prefix}{case_id}: a multi-step reference needs a 'combine' query to "
        "say how the steps relate",
    )
    if len(steps) > 1:
        names = [s.as_ for s in steps]
        _require(
            len(set(names)) == len(names),
            f"{prefix}{case_id}: reference steps must have distinct 'as' names",
        )

    match = str(raw_expect.get("match") or "rows")
    _require(
        match in ("rows", "scalar"),
        f"{prefix}{case_id}: unknown match mode {match!r} (rows | scalar)",
    )

    sources = tuple(raw_expect.get("sources") or sorted({s.source for s in steps}))
    answer_contains = tuple(str(v) for v in raw_expect.get("answer_contains") or ())

    return Case(
        id=case_id,
        question=question,
        tags=tuple(raw.get("tags") or ()),
        notes=str(raw.get("notes") or ""),
        expect=Expectation(
            reference=tuple(steps),
            combine=combine,
            match=match,
            tolerance=float(raw_expect.get("tolerance", 0.01)),
            order_sensitive=bool(raw_expect.get("order_sensitive", False)),
            sources=sources,
            answer_contains=answer_contains,
        ),
    )


def load_suite(path: str | Path) -> Suite:
    """Load and fully validate a suite file.

    Validation is total and up front: a suite with one bad case fails to load
    rather than failing on case 14 of 20, an hour and several dollars in.
    """
    p = Path(path)
    if not p.exists() and not p.suffix:
        p = SUITES_DIR / f"{p.name}.json"
    if not p.exists():
        raise SuiteError(f"No suite at {path!r}. Available: {', '.join(list_suites())}")

    try:
        raw = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise SuiteError(f"{p.name} is not valid JSON: {exc}") from exc

    cases_raw = raw.get("cases") if isinstance(raw, dict) else raw
    _require(isinstance(cases_raw, list), f"{p.name}: expected a list of cases")

    cases = [parse_case(c, where=p.name) for c in cases_raw]
    ids = [c.id for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    _require(not dupes, f"{p.name}: duplicate case ids {sorted(dupes)}")

    name = raw.get("name") if isinstance(raw, dict) else p.stem
    return Suite(name=str(name or p.stem), cases=cases)


def list_suites() -> list[str]:
    return sorted(p.stem for p in SUITES_DIR.glob("*.json"))


# --- computing the expected relation -----------------------------------------


def combine_steps(
    step_rows: dict[str, tuple[list[str], list[list[Any]]]], combine_sql: str
) -> tuple[list[str], list[list[Any]]]:
    """Run ``combine_sql`` over step results in an in-memory SQLite database.

    SQLite is the arbiter here purely because it is in the stdlib and neither of
    the engines under test is; using one of them would make ground truth depend
    on the same system being measured.
    """
    conn = sqlite3.connect(":memory:")
    try:
        for name, (columns, rows) in step_rows.items():
            quoted = ", ".join(f'"{c}"' for c in columns)
            conn.execute(f'CREATE TABLE "{name}" ({quoted})')
            placeholders = ", ".join("?" for _ in columns)
            conn.executemany(
                f'INSERT INTO "{name}" VALUES ({placeholders})',
                [[_sqlite_safe(v) for v in row] for row in rows],
            )
        cur = conn.execute(combine_sql)
        columns = [d[0] for d in cur.description or ()]
        return columns, [list(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _sqlite_safe(value: Any) -> Any:
    """Coerce a warehouse value into something SQLite will bind.

    Decimals become floats: the combine step exists to relate small aggregates,
    and the scorer compares numerically within a tolerance anyway, so exactness
    past that tolerance buys nothing and a refused bind costs the whole case.
    """
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float, str, bytes)) or value is None:
        return value
    return str(value)
