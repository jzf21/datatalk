"""Scoring: did the agent actually get the right answer?

Scored by **execution match**, never by comparing SQL text. A question has many
correct queries and one correct answer, so a string comparison would mark a
better query wrong for being different -- and would mark a query right for
looking familiar while returning nonsense. This is the same choice Spider and
BIRD make, for the same reason.

Three properties the matcher deliberately has:

**Column names are ignored.** The agent picks its own aliases; ``revenue``,
``total_revenue`` and ``sum`` are the same answer. Matching is by *values*: the
scorer searches for an injective assignment of reference columns onto captured
columns under which the row sets agree. That search is why column-name drift
costs nothing.

**Extra columns are forgiven, extra rows are not.** An agent that also returns
the order count alongside revenue answered the question; an agent that returned
eighteen rows where six were asked for answered a different one. Row *order* is
forgiven unless the case says the ranking is the point.

**Every dataset the agent captured is a candidate.** The loop captures
exploratory queries too, and finding the answer on the third attempt is still
finding it. What that would wrongly reward is a shotgun agent that captures a
hundred datasets hoping one lands -- which is why :mod:`datatalk.evals.runner`
reports queries-per-case alongside accuracy rather than accuracy alone.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

# A normalized cell: ``None``, ``("n", float)``, or ``("s", str)``. Booleans
# normalize to numbers so a driver returning 1/True for the same column across
# two engines does not read as two different answers.
Cell = Any


@dataclass(frozen=True)
class Relation:
    """A result set, from either the agent or the reference query."""

    columns: list[str]
    rows: list[list[Any]]

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.rows), len(self.columns)

    def column(self, i: int) -> list[Any]:
        return [row[i] if i < len(row) else None for row in self.rows]


@dataclass
class MatchOutcome:
    matched: bool
    dataset_id: str = ""
    reason: str = ""
    # Column assignment that made it match: reference index -> captured index.
    assignment: tuple[int, ...] = ()

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.matched


# --- normalization ------------------------------------------------------------

_NUMERIC_TEXT = re.compile(r"^[+-]?[\d,]*\.?\d+(?:[eE][+-]?\d+)?$")


def normalize_value(value: Any) -> Cell:
    """Reduce a cell to a comparable form, absorbing engine differences only.

    The differences absorbed here are all *representational*: ClickHouse hands
    back a Decimal as a string where Postgres hands back a Decimal; a DATE
    column arrives as a ``date`` from one driver and a midnight ``datetime``
    from the other. None of those are disagreements about the answer, and
    treating them as such would score the engine rather than the agent.

    What is NOT absorbed: rounding beyond the case's tolerance, and any
    difference in row count. Those are answers.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return ("n", float(value))
    if isinstance(value, (int, float, Decimal)):
        f = float(value)
        # NaN would compare unequal to itself and quietly fail every match that
        # touched it; an infinity has no tolerance band. Both are answers a
        # reference query never produces, so they are their own class.
        if math.isnan(f):
            return ("s", "nan")
        if math.isinf(f):
            return ("s", "inf" if f > 0 else "-inf")
        return ("n", f)
    if isinstance(value, datetime):
        # Midnight collapses to a plain date: a DATE column read back through a
        # driver that has no date type is the same day, not a different one.
        if (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0):
            return ("s", value.date().isoformat())
        return ("s", value.replace(tzinfo=None).isoformat(sep=" "))
    if isinstance(value, date):
        return ("s", value.isoformat())

    text = str(value).strip()
    if not text:
        return ("s", "")
    if _NUMERIC_TEXT.match(text):
        try:
            return ("n", float(Decimal(text.replace(",", ""))))
        except (InvalidOperation, ValueError):
            pass
    return ("s", text.casefold())


def _cells_equal(a: Cell, b: Cell, tolerance: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    kind_a, val_a = a
    kind_b, val_b = b
    if kind_a != kind_b:
        return False
    if kind_a == "n":
        return abs(val_a - val_b) <= tolerance
    return val_a == val_b


def _sort_key(row: Sequence[Cell]) -> tuple:
    """A total order over normalized rows, so two multisets can be compared by
    sorting rather than by an O(n^2) pairing."""
    key: list[tuple[int, float, str]] = []
    for cell in row:
        if cell is None:
            key.append((0, 0.0, ""))
        elif cell[0] == "n":
            key.append((1, cell[1], ""))
        else:
            key.append((2, 0.0, cell[1]))
    return tuple(key)


def _normalize_relation(rel: Relation) -> list[list[Cell]]:
    return [[normalize_value(v) for v in row] for row in rel.rows]


# --- matching -----------------------------------------------------------------


def _columns_compatible(
    expected_col: list[Cell], candidate_col: list[Cell], tolerance: float
) -> bool:
    """Necessary condition for an assignment: the two columns hold the same
    multiset of values. Cheap, and it prunes the search hard."""
    if len(expected_col) != len(candidate_col):
        return False
    a = sorted(expected_col, key=lambda c: _sort_key([c]))
    b = sorted(candidate_col, key=lambda c: _sort_key([c]))
    return all(_cells_equal(x, y, tolerance) for x, y in zip(a, b))


def _rows_agree(
    expected: list[list[Cell]],
    candidate: list[list[Cell]],
    assignment: Sequence[int],
    tolerance: float,
    order_sensitive: bool,
) -> bool:
    projected = [[row[i] for i in assignment] for row in candidate]
    if order_sensitive:
        pairs = zip(expected, projected)
    else:
        pairs = zip(
            sorted(expected, key=_sort_key), sorted(projected, key=_sort_key)
        )
    return all(
        all(_cells_equal(x, y, tolerance) for x, y in zip(erow, crow))
        for erow, crow in pairs
    )


def _search_assignment(
    expected: list[list[Cell]],
    candidate: list[list[Cell]],
    compatible: list[list[int]],
    tolerance: float,
    order_sensitive: bool,
) -> tuple[int, ...] | None:
    """Backtracking search for an injective column assignment.

    Bounded in practice by the compatibility pre-filter: reference relations
    here have at most a handful of columns, and two columns are only
    interchangeable when they hold literally the same values -- in which case
    either choice is equally correct.
    """
    n = len(compatible)
    used: set[int] = set()
    assignment: list[int] = []

    # Most-constrained column first: an assignment that cannot work fails on the
    # first choice rather than after exploring every permutation behind it.
    order = sorted(range(n), key=lambda i: len(compatible[i]))
    slots: list[int] = [-1] * n

    def backtrack(depth: int) -> tuple[int, ...] | None:
        if depth == n:
            candidate_assignment = tuple(slots)
            if _rows_agree(
                expected, candidate, candidate_assignment, tolerance, order_sensitive
            ):
                return candidate_assignment
            return None
        col = order[depth]
        for c in compatible[col]:
            if c in used:
                continue
            used.add(c)
            slots[col] = c
            found = backtrack(depth + 1)
            if found is not None:
                return found
            used.discard(c)
            slots[col] = -1
        return None

    del assignment
    return backtrack(0)


def match_relation(
    expected: Relation,
    candidate: Relation,
    *,
    tolerance: float = 0.01,
    order_sensitive: bool = False,
) -> MatchOutcome:
    """Is ``expected`` embeddable in ``candidate``?"""
    exp_rows = _normalize_relation(expected)
    cand_rows = _normalize_relation(candidate)

    if len(exp_rows) != len(cand_rows):
        return MatchOutcome(
            False,
            reason=f"{len(cand_rows)} rows, reference has {len(exp_rows)}",
        )
    if len(expected.columns) > len(candidate.columns):
        return MatchOutcome(
            False,
            reason=(
                f"{len(candidate.columns)} columns, reference needs "
                f"{len(expected.columns)}"
            ),
        )
    if not exp_rows:
        # Both empty. An empty reference is a weak case, but "no rows" is a real
        # answer and matching it is not a bug.
        return MatchOutcome(True, reason="both empty")

    exp_cols = [[row[i] for row in exp_rows] for i in range(len(expected.columns))]
    cand_cols = [
        [row[i] if i < len(row) else None for row in cand_rows]
        for i in range(len(candidate.columns))
    ]

    compatible: list[list[int]] = []
    for i, ecol in enumerate(exp_cols):
        options = [
            j for j, ccol in enumerate(cand_cols) if _columns_compatible(ecol, ccol, tolerance)
        ]
        if not options:
            return MatchOutcome(
                False,
                reason=(
                    f"no captured column holds the values of reference column "
                    f"{expected.columns[i]!r}"
                ),
            )
        compatible.append(options)

    found = _search_assignment(
        exp_rows, cand_rows, compatible, tolerance, order_sensitive
    )
    if found is None:
        return MatchOutcome(
            False,
            reason=(
                "columns match individually but no assignment reproduces the "
                "rows" + (" in order" if order_sensitive else "")
            ),
        )
    return MatchOutcome(True, assignment=found)


def match_scalar(
    expected: Relation, candidate: Relation, *, tolerance: float = 0.01
) -> MatchOutcome:
    """Single-value mode: one row, and the value is in it somewhere.

    Forgiving about the shape of the row because the shape is not the answer --
    ``SELECT count(*)`` and ``SELECT count(*), min(d), max(d)`` both answer "how
    many"; requiring the narrower one would score formatting.
    """
    if not expected.rows or not expected.rows[0]:
        return MatchOutcome(False, reason="reference produced no value")
    wanted = normalize_value(expected.rows[0][0])
    if len(candidate.rows) != 1:
        return MatchOutcome(
            False, reason=f"{len(candidate.rows)} rows, expected a single value"
        )
    for value in candidate.rows[0]:
        if _cells_equal(normalize_value(value), wanted, tolerance):
            return MatchOutcome(True)
    return MatchOutcome(False, reason="the value is not in the captured row")


def execution_match(
    expected: Relation,
    candidates: dict[str, Relation],
    *,
    match: str = "rows",
    tolerance: float = 0.01,
    order_sensitive: bool = False,
) -> MatchOutcome:
    """Does any captured dataset contain the reference answer?

    Candidates are tried in capture order, so a hit names the earliest dataset
    that answers the question -- which is the one a reader of the trace would
    point at.
    """
    if not candidates:
        return MatchOutcome(False, reason="the agent captured no datasets")

    reasons: list[str] = []
    for dataset_id, candidate in candidates.items():
        if match == "scalar":
            outcome = match_scalar(expected, candidate, tolerance=tolerance)
        else:
            outcome = match_relation(
                expected,
                candidate,
                tolerance=tolerance,
                order_sensitive=order_sensitive,
            )
        if outcome.matched:
            outcome.dataset_id = dataset_id
            return outcome
        reasons.append(f"{dataset_id}: {outcome.reason}")

    return MatchOutcome(False, reason="; ".join(reasons[:4]))


# --- answer fidelity ----------------------------------------------------------

_NUMBER_IN_TEXT = re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?")


@dataclass
class AnswerCheck:
    passed: bool
    missing: list[str] = field(default_factory=list)


def answer_contains(text: str, wanted: Iterable[str], *, tolerance: float = 0.01) -> AnswerCheck:
    """Did the written answer carry the values through?

    Scored separately from execution match on purpose. An agent that fetches the
    right rows and then narrates a number it made up has failed in a way that a
    combined metric would hide -- and that specific failure is what
    ``materialize()`` exists to make impossible, so it is worth measuring rather
    than assuming.

    Numbers are compared numerically, so thousands separators, currency symbols
    and trailing zeroes do not count against an answer.
    """
    haystack = text.casefold()
    numbers = [
        float(m.group(0).replace(",", "")) for m in _NUMBER_IN_TEXT.finditer(text)
    ]
    missing: list[str] = []
    for token in wanted:
        stripped = token.strip()
        try:
            target = float(stripped.replace(",", "").lstrip("$€£"))
        except ValueError:
            if stripped.casefold() not in haystack:
                missing.append(token)
            continue
        # A relative band as well as the absolute one: an answer that reports
        # revenue as "$1.31M" is not wrong, and an absolute cent tolerance would
        # call it wrong.
        band = max(tolerance, abs(target) * 0.005)
        if not any(abs(n - target) <= band for n in numbers):
            missing.append(token)
    return AnswerCheck(passed=not missing, missing=missing)


# --- routing ------------------------------------------------------------------


def routing_score(queried: Sequence[str], expected: Sequence[str]) -> bool:
    """Did the agent reach exactly the sources the question needs?

    Equality, not containment. Querying a source the question does not need is a
    real failure in a multi-tenant, multi-warehouse product: it is latency and
    tokens spent on data that cannot contribute, and at scale it is the
    difference between a router and a scanner.
    """
    if not expected:
        return True
    return set(q for q in queried if q) == set(expected)
