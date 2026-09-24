"""The scorer is the part of the eval harness that can be wrong silently.

A seeding bug fails loudly; a case with broken SQL fails loudly. A matcher that
is a little too generous just reports a higher number, and nothing about the
output says so. These tests pin the exact shape of that generosity: what it
forgives (column names, column order, extra columns, row order, engine
representation) and what it must never forgive (wrong values, wrong row counts,
missing columns).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from datatalk.evals.scoring import (
    Relation,
    answer_contains,
    execution_match,
    match_relation,
    match_scalar,
    normalize_value,
    routing_score,
)


def rel(columns, rows) -> Relation:
    return Relation(columns=list(columns), rows=[list(r) for r in rows])


EXPECTED = rel(
    ["category", "revenue"],
    [["Audio", Decimal("1200.50")], ["Cameras", Decimal("900.00")]],
)


# --- what the matcher forgives -------------------------------------------------


def test_column_names_are_ignored():
    """The agent picks its own aliases; 'revenue' and 'total_rev' are one answer."""
    candidate = rel(["cat", "total_rev"], [["Audio", 1200.50], ["Cameras", 900.0]])
    assert match_relation(EXPECTED, candidate).matched


def test_column_order_is_ignored():
    candidate = rel(["rev", "cat"], [[1200.50, "Audio"], [900.0, "Cameras"]])
    assert match_relation(EXPECTED, candidate).matched


def test_extra_columns_are_forgiven():
    """Also returning the order count still answers the question asked."""
    candidate = rel(
        ["cat", "orders", "rev"],
        [["Audio", 12, 1200.50], ["Cameras", 7, 900.0]],
    )
    assert match_relation(EXPECTED, candidate).matched


def test_row_order_is_ignored_by_default():
    candidate = rel(["cat", "rev"], [["Cameras", 900.0], ["Audio", 1200.50]])
    assert match_relation(EXPECTED, candidate).matched


def test_row_order_is_enforced_when_the_case_says_so():
    """A 'top 5, highest first' question is partly *about* the order."""
    candidate = rel(["cat", "rev"], [["Cameras", 900.0], ["Audio", 1200.50]])
    assert not match_relation(EXPECTED, candidate, order_sensitive=True).matched


def test_values_within_tolerance_match():
    candidate = rel(["cat", "rev"], [["Audio", 1200.505], ["Cameras", 899.995]])
    assert match_relation(EXPECTED, candidate, tolerance=0.01).matched


def test_string_case_is_ignored():
    candidate = rel(["cat", "rev"], [["audio", 1200.50], ["CAMERAS", 900.0]])
    assert match_relation(EXPECTED, candidate).matched


# --- what it must never forgive ------------------------------------------------


def test_wrong_value_fails():
    candidate = rel(["cat", "rev"], [["Audio", 1200.50], ["Cameras", 901.0]])
    assert not match_relation(EXPECTED, candidate).matched


def test_extra_rows_fail():
    """Revenue by category-and-month is a different answer to revenue by category."""
    candidate = rel(
        ["cat", "rev"],
        [["Audio", 1200.50], ["Cameras", 900.0], ["Audio", 300.0]],
    )
    outcome = match_relation(EXPECTED, candidate)
    assert not outcome.matched
    assert "rows" in outcome.reason


def test_missing_column_fails():
    candidate = rel(["rev"], [[1200.50], [900.0]])
    outcome = match_relation(EXPECTED, candidate)
    assert not outcome.matched
    assert "columns" in outcome.reason


def test_right_values_in_the_wrong_pairing_fail():
    """Both columns hold the right multisets, but the rows pair them wrongly.

    The case the per-column pre-filter cannot catch on its own, and the reason
    the matcher verifies the whole row set after choosing an assignment.
    """
    candidate = rel(["cat", "rev"], [["Audio", 900.0], ["Cameras", 1200.50]])
    outcome = match_relation(EXPECTED, candidate)
    assert not outcome.matched


def test_a_duplicated_column_cannot_satisfy_two_expectations():
    """Assignment is injective: one captured column answers for one reference
    column, never two."""
    expected = rel(["a", "b"], [[1, 1], [2, 2]])
    candidate = rel(["only"], [[1], [2]])
    assert not match_relation(expected, candidate).matched


# --- engine representation -----------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (Decimal("12.50"), ("n", 12.5)),
        (12.5, ("n", 12.5)),
        ("12.50", ("n", 12.5)),  # ClickHouse hands Decimals back as strings
        ("1,200.50", ("n", 1200.5)),
        (True, ("n", 1.0)),
        (date(2025, 3, 1), ("s", "2025-03-01")),
        (datetime(2025, 3, 1, 0, 0), ("s", "2025-03-01")),  # a DATE via a driver
        (datetime(2025, 3, 1, 9, 30), ("s", "2025-03-01 09:30:00")),
        ("  Audio ", ("s", "audio")),
        (None, None),
    ],
)
def test_normalization_absorbs_representation_only(raw, expected):
    assert normalize_value(raw) == expected


def test_nan_does_not_silently_match_itself_as_a_number():
    """NaN compares unequal to everything, so it gets its own class rather than
    poisoning a numeric comparison."""
    assert normalize_value(float("nan")) == ("s", "nan")


def test_none_only_matches_none():
    expected = rel(["a"], [[None]])
    assert match_relation(expected, rel(["a"], [[None]])).matched
    assert not match_relation(expected, rel(["a"], [[0]])).matched


# --- scalar mode ---------------------------------------------------------------


def test_scalar_finds_the_value_anywhere_in_a_single_row():
    expected = rel(["orders"], [[412]])
    assert match_scalar(expected, rel(["n", "lo", "hi"], [[412, 1, 9]])).matched


def test_scalar_rejects_a_multi_row_candidate():
    expected = rel(["orders"], [[412]])
    assert not match_scalar(expected, rel(["n"], [[412], [7]])).matched


# --- picking among captured datasets -------------------------------------------


def test_execution_match_scans_every_captured_dataset():
    """Exploring first and finding the answer on q3 is still finding it."""
    candidates = {
        "q1": rel(["n"], [[1]]),
        "q2": rel(["x", "y"], [["Audio", 1.0]]),
        "q3": rel(["cat", "rev"], [["Audio", 1200.50], ["Cameras", 900.0]]),
    }
    outcome = execution_match(EXPECTED, candidates)
    assert outcome.matched
    assert outcome.dataset_id == "q3"


def test_execution_match_reports_why_when_nothing_matched():
    outcome = execution_match(EXPECTED, {"q1": rel(["n"], [[1]])})
    assert not outcome.matched
    assert "q1" in outcome.reason


def test_no_datasets_is_a_failure_with_a_reason():
    outcome = execution_match(EXPECTED, {})
    assert not outcome.matched
    assert "no datasets" in outcome.reason


# --- routing -------------------------------------------------------------------


def test_routing_requires_exactly_the_expected_sources():
    assert routing_score(["sales", "sales"], ["sales"])
    assert routing_score(["sales", "events"], ["events", "sales"])
    # Reaching a source that cannot contribute is a routing failure, not a
    # harmless extra: it is latency and tokens spent on data that is irrelevant.
    assert not routing_score(["sales", "events"], ["sales"])
    assert not routing_score(["sales"], ["sales", "events"])


# --- answer fidelity -----------------------------------------------------------


def test_answer_fidelity_matches_numbers_numerically():
    text = "Revenue reached $1,200.50 in Audio and 900 in Cameras."
    assert answer_contains(text, ["1200.5", "900"]).passed


def test_answer_fidelity_tolerates_rounded_prose():
    """'$1.31M' is not a wrong report of 1,310,402.11."""
    assert answer_contains("Revenue was about 1310400.", ["1310402.11"]).passed


def test_answer_fidelity_reports_what_is_missing():
    check = answer_contains("Revenue reached 1200.50.", ["1200.5", "900"])
    assert not check.passed
    assert check.missing == ["900"]


def test_answer_fidelity_falls_back_to_substring_for_non_numbers():
    assert answer_contains("The top category was Audio.", ["audio"]).passed
    assert not answer_contains("The top category was Audio.", ["cameras"]).passed
