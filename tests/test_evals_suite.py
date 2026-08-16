"""The fixture and the shipped suite, checked without a warehouse or a model.

Everything here guards a way the harness could go quietly wrong: a fixture that
drifts (so every reference answer changes), a case phrased against a moving
calendar (so the pass rate rots on its own), a context model whose `covers`
entries point at tables that do not exist (so the file never reaches the agent
that needed it).
"""

from __future__ import annotations

import json

import pytest

from datatalk.evals import dataset
from datatalk.evals.cases import (
    SuiteError,
    combine_steps,
    load_suite,
    parse_case,
)
from datatalk.evals.dataset import COLUMNS, SOURCE_TABLES
from datatalk.evals.targets import SOURCE_DESCRIPTIONS, load_context_model


# --- the fixture ---------------------------------------------------------------


def test_fixture_is_reproducible():
    """Two builds in one process must agree -- the LCG holds no global state."""
    assert dataset.fingerprint(dataset.build_fixture()) == dataset.fingerprint(
        dataset.build_fixture()
    )


def test_fixture_matches_the_pinned_fingerprint():
    """The suite's reference queries were written against exactly this data.

    If this fails, the accuracy numbers before and after the change are not
    comparable -- which is a thing to decide deliberately, not to discover.
    """
    dataset.build_verified_fixture()


def test_fixture_drift_is_a_loud_error(monkeypatch):
    monkeypatch.setattr(dataset, "FINGERPRINT", "0" * 64)
    with pytest.raises(dataset.FixtureDriftError) as exc:
        dataset.build_verified_fixture()
    assert "re-baseline" in str(exc.value)


def test_every_table_has_ddl_and_columns_that_agree():
    fx = dataset.build_fixture()
    for source, tables in SOURCE_TABLES.items():
        for name in tables:
            rows = getattr(fx, name)
            assert rows, f"{name} is empty"
            assert len(rows[0]) == len(COLUMNS[name]), (
                f"{name}: generated {len(rows[0])} values for "
                f"{len(COLUMNS[name])} columns"
            )
            ddl = (
                dataset.POSTGRES_DDL[name]
                if source == "sales" or name not in dataset.CLICKHOUSE_DDL
                else dataset.CLICKHOUSE_DDL[name]
            )
            for column in COLUMNS[name]:
                assert column in ddl, f"{name}.{column} is missing from its DDL"


def test_events_tables_have_a_clickhouse_form():
    """The events source can run on either engine; both need real DDL."""
    for name in SOURCE_TABLES["events"]:
        assert name in dataset.CLICKHOUSE_DDL
        assert name in dataset.POSTGRES_DDL


def test_the_fixture_spans_the_advertised_period():
    fx = dataset.build_fixture()
    order_dates = [row[2] for row in fx.orders]
    assert min(order_dates) >= dataset.PERIOD_START
    assert max(order_dates) <= dataset.PERIOD_END
    # Every month has orders, or a monthly-trend case has a hole in it.
    assert {d.month for d in order_dates} == set(dataset.ORDERS_PER_MONTH)


def test_refunds_only_attach_to_delivered_orders():
    """ontology/orders.md tells the agent this; the data has to actually obey."""
    fx = dataset.build_fixture()
    status_of = {row[0]: row[3] for row in fx.orders}
    assert all(status_of[r[1]] == "delivered" for r in fx.refunds)


def test_spend_channels_match_order_channels():
    """The cross-source cases join on these by name; a mismatch would make
    'cost per order by channel' unanswerable rather than hard."""
    fx = dataset.build_fixture()
    assert {row[1] for row in fx.marketing_spend} == set(dataset.CHANNELS)
    assert {row[4] for row in fx.orders} == set(dataset.CHANNELS)


# --- the shipped suite ---------------------------------------------------------


@pytest.fixture(scope="module")
def suite():
    return load_suite("retail")


def test_the_shipped_suite_loads(suite):
    assert len(suite) >= 15


def test_every_case_names_real_sources(suite):
    for case in suite:
        for source in case.expect.sources:
            assert source in SOURCE_TABLES, f"{case.id}: unknown source {source!r}"
        for step in case.expect.reference:
            assert step.source in SOURCE_TABLES, (
                f"{case.id}: reference step names unknown source {step.source!r}"
            )


def test_reference_steps_only_touch_their_own_source(suite):
    """A reference that reads a table from the other warehouse would be scoring
    the agent against something the product cannot do."""
    for case in suite:
        for step in case.expect.reference:
            others = {
                table
                for source, tables in SOURCE_TABLES.items()
                if source != step.source
                for table in tables
            }
            for table in others:
                assert table not in step.sql, (
                    f"{case.id}: a reference step against {step.source!r} "
                    f"mentions {table!r}, which lives in the other source"
                )


def test_multi_source_cases_combine_rather_than_join(suite):
    multi = [c for c in suite if c.expect.is_multi_source]
    assert multi, "the suite must exercise cross-source routing"
    for case in multi:
        assert case.expect.combine, f"{case.id}: multi-source case needs a combine"


def test_clickhouse_forms_exist_for_every_events_step(suite):
    """The events source may be moved onto ClickHouse; a case with no ClickHouse
    reference would fail at run time rather than at load time."""
    for case in suite:
        for step in case.expect.reference:
            if step.source == "events":
                assert step.sql_clickhouse, (
                    f"{case.id}: events step has no 'sql_clickhouse'"
                )


def test_no_case_uses_a_relative_time_window():
    with pytest.raises(SuiteError) as exc:
        parse_case(
            {
                "id": "bad",
                "question": "What was revenue last month?",
                "expect": {"reference": {"source": "sales", "sql": "SELECT 1"}},
            }
        )
    assert "relative time window" in str(exc.value)


def test_duplicate_case_ids_are_rejected(tmp_path):
    path = tmp_path / "dupes.json"
    case = {
        "id": "same",
        "question": "How many orders were placed in March 2025?",
        "expect": {"reference": {"source": "sales", "sql": "SELECT 1"}},
    }
    path.write_text(json.dumps({"name": "dupes", "cases": [case, dict(case)]}))
    with pytest.raises(SuiteError, match="duplicate case ids"):
        load_suite(path)


def test_a_multi_step_reference_without_a_combine_is_rejected():
    with pytest.raises(SuiteError, match="combine"):
        parse_case(
            {
                "id": "bad",
                "question": "What did each channel cost in March 2025?",
                "expect": {
                    "reference": [
                        {"source": "sales", "sql": "SELECT 1", "as": "a"},
                        {"source": "events", "sql": "SELECT 2", "as": "b"},
                    ]
                },
            }
        )


def test_filtering_by_tag_and_id(suite):
    tagged = suite.filtered(tags=["multi-source"])
    assert tagged and all("multi-source" in c.tags for c in tagged)
    one = suite.filtered(ids=[suite.cases[0].id])
    assert len(one) == 1


# --- combining reference steps -------------------------------------------------


def test_combine_relates_two_step_results():
    """The cross-source ground truth path: two relations, one SQLite join."""
    columns, rows = combine_steps(
        {
            "orders_by_channel": (["channel", "orders"], [["email", 4], ["social", 5]]),
            "spend_by_channel": (["channel", "spend"], [["email", 40.0], ["social", 100.0]]),
        },
        "SELECT o.channel, ROUND(s.spend / o.orders, 2) AS cost FROM "
        "orders_by_channel o JOIN spend_by_channel s ON s.channel = o.channel "
        "ORDER BY o.channel",
    )
    assert columns == ["channel", "cost"]
    assert rows == [["email", 10.0], ["social", 20.0]]


def test_combine_accepts_warehouse_types():
    """Decimals and dates come straight off a driver; SQLite binds neither."""
    from datetime import date
    from decimal import Decimal

    columns, rows = combine_steps(
        {"t": (["d", "amount"], [[date(2025, 3, 1), Decimal("10.50")]])},
        "SELECT d, amount * 2 AS doubled FROM t",
    )
    assert columns == ["d", "doubled"]
    assert rows == [["2025-03-01", 21.0]]


# --- the shipped context model -------------------------------------------------


def test_context_model_loads_with_bodies_and_summaries():
    model = load_context_model()
    assert not model.is_empty
    for f in model.files:
        assert f.summary, f"{f.path} has no summary; the tree is all the agent sees"
        assert f.body_md.strip(), f"{f.path} has no body"


def test_context_model_covers_only_real_tables():
    """A `covers` entry pointing at a table that does not exist means the file
    silently never attaches to a describe_source call."""
    for f in load_context_model().files:
        for source, table in f.covers:
            assert source in SOURCE_TABLES, f"{f.path}: unknown source {source!r}"
            assert table in SOURCE_TABLES[source], (
                f"{f.path}: {table!r} is not a table in {source!r}"
            )


def test_the_tree_never_carries_a_body():
    """The two-tier bargain: adding a file costs ~35 prompt tokens, not a page."""
    model = load_context_model()
    tree = model.render_tree()
    for f in model.files:
        assert f.path in tree
        body_line = next(
            (line for line in f.body_md.splitlines() if len(line.strip()) > 40), ""
        )
        assert body_line and body_line not in tree


def test_every_source_is_described():
    """The description is how the agent routes a question to a warehouse."""
    for source in SOURCE_TABLES:
        assert SOURCE_DESCRIPTIONS.get(source, "").strip()
