"""The agent across several sources: routing, provenance, and recovery.

An org's context carries every source it has and the model names one per query.
The things that go wrong here are specific: a query reaching the wrong
warehouse, a captured dataset losing track of where its numbers came from, one
dead source taking down every report, and the model inventing a source name.
"""

from __future__ import annotations

import json

import pytest

import datatalk.agent.report as report_mod
from datatalk.agent.sqlloop import run_capture_loop
from datatalk.context import UnknownSourceError
from datatalk.warehouse import WarehouseError, catalog
from tests.conftest import (
    FakeOpenAI,
    FakeWarehouse,
    describe_call,
    fake_table,
    fn_call,
    make_ctx,
    message,
    response,
)


def _two_sources():
    events = FakeWarehouse(
        tables=[fake_table("pageviews", database="web", columns=("ts", "url"))],
        columns=["url", "hits"],
        rows=[["/home", 10]],
    )
    billing = FakeWarehouse(
        tables=[fake_table("invoices", database="public", columns=("id", "amount"))],
        columns=["amount"],
        rows=[[99]],
    )
    return events, billing


def _seed():
    return [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


# --- routing ------------------------------------------------------------------


def test_each_query_reaches_the_named_source():
    """The core of multi-source: the right statement hits the right warehouse."""
    events, billing = _two_sources()
    ctx = make_ctx(
        openai=FakeOpenAI(
            [
                response(message(tool_calls=[fn_call("c1", "SELECT 1", "events")])),
                response(message(tool_calls=[fn_call("c2", "SELECT 2", "billing")])),
                response(message(content="done")),
            ]
        ),
        warehouses={"events": events, "billing": billing},
    )

    result = run_capture_loop(_seed(), ctx=ctx)

    assert [q.strip() for q in events.queries] == ["SELECT 1\nLIMIT 1000"]
    assert [q.strip() for q in billing.queries] == ["SELECT 2\nLIMIT 1000"]
    assert result.final_content == "done"


def test_captured_datasets_record_their_source():
    """Provenance is what makes a stored report auditable after the fact."""
    events, billing = _two_sources()
    ctx = make_ctx(
        openai=FakeOpenAI(
            [
                response(
                    message(
                        tool_calls=[
                            fn_call("c1", "SELECT url, hits FROM web.pageviews", "events"),
                            fn_call("c2", "SELECT amount FROM public.invoices", "billing"),
                        ]
                    )
                ),
                response(message(content="done")),
            ]
        ),
        warehouses={"events": events, "billing": billing},
    )

    result = run_capture_loop(_seed(), ctx=ctx)

    assert result.dataset_sources == {"q1": "events", "q2": "billing"}
    assert [q["source"] for q in result.queries] == ["events", "billing"]
    # And the two datasets really are the two warehouses' rows, not one twice.
    assert result.datasets["q1"].rows == [["/home", 10]]
    assert result.datasets["q2"].rows == [[99]]


def test_the_default_source_is_used_when_none_is_named():
    events, billing = _two_sources()
    ctx = make_ctx(
        openai=FakeOpenAI(
            [
                response(message(tool_calls=[fn_call("c1", "SELECT 1", "")])),
                response(message(content="done")),
            ]
        ),
        warehouses={"events": events, "billing": billing},
    )

    result = run_capture_loop(_seed(), ctx=ctx)

    assert events.queries and not billing.queries
    assert result.dataset_sources == {"q1": "events"}


@pytest.mark.parametrize(
    "sql,allowed_on,rejected_on",
    [
        # LOCK is a Postgres hazard; on ClickHouse it is just an identifier.
        ("SELECT lock FROM t", "ch", "pg"),
        # SYSTEM is a ClickHouse hazard; on Postgres it is just an identifier.
        ("SELECT system FROM t", "pg", "ch"),
    ],
)
def test_the_dialect_of_the_named_source_is_applied(sql, allowed_on, rejected_on):
    """A statement is validated against the engine it will actually run on.

    Both directions matter: applying one engine's forbidden list to the other
    either lets a hazard through or rejects a perfectly ordinary column name.
    """
    from datatalk.warehouse.postgres import POSTGRES_DIALECT

    warehouses = {"pg": FakeWarehouse(dialect=POSTGRES_DIALECT), "ch": FakeWarehouse()}
    messages = _seed()
    ctx = make_ctx(
        openai=FakeOpenAI(
            [
                response(
                    message(
                        tool_calls=[
                            fn_call("c1", sql, rejected_on),
                            fn_call("c2", sql, allowed_on),
                        ]
                    )
                ),
                response(message(content="done")),
            ]
        ),
        warehouses=warehouses,
    )

    result = run_capture_loop(messages, ctx=ctx)

    assert not warehouses[rejected_on].queries
    assert warehouses[allowed_on].queries
    assert result.dataset_sources == {"q1": allowed_on}
    rejected = json.loads([m for m in messages if m.get("role") == "tool"][0]["content"])
    assert "Rejected" in rejected["error"]


# --- recovery -----------------------------------------------------------------


def test_an_invented_source_name_is_a_recoverable_tool_error():
    """The model must get told what exists, not blow up the whole report."""
    events, billing = _two_sources()
    messages = _seed()
    ctx = make_ctx(
        openai=FakeOpenAI(
            [
                response(message(tool_calls=[fn_call("c1", "SELECT 1", "warehouse_of_dreams")])),
                response(message(tool_calls=[fn_call("c2", "SELECT 1", "events")])),
                response(message(content="recovered")),
            ]
        ),
        warehouses={"events": events, "billing": billing},
    )

    result = run_capture_loop(messages, ctx=ctx)

    assert result.final_content == "recovered"
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    error = json.loads(tool_msgs[0]["content"])["error"]
    assert "warehouse_of_dreams" in error
    # Actionable: it names the sources that do exist.
    assert "events" in error and "billing" in error
    # And the retry against a real source succeeded.
    assert result.dataset_sources == {"q1": "events"}


def test_a_query_error_on_one_source_does_not_stop_the_loop():
    events, billing = _two_sources()
    billing.fail = WarehouseError("relation does not exist")
    messages = _seed()
    ctx = make_ctx(
        openai=FakeOpenAI(
            [
                response(message(tool_calls=[fn_call("c1", "SELECT 1", "billing")])),
                response(message(tool_calls=[fn_call("c2", "SELECT 1", "events")])),
                response(message(content="done")),
            ]
        ),
        warehouses={"events": events, "billing": billing},
    )

    result = run_capture_loop(messages, ctx=ctx)

    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert "relation does not exist" in json.loads(tool_msgs[0]["content"])["error"]
    assert result.dataset_sources == {"q1": "events"}


def test_one_dead_source_still_yields_a_usable_catalog():
    """With N sources, a single bad credential must not break every report."""
    events, billing = _two_sources()
    billing.fail = WarehouseError("password authentication failed")
    ctx = make_ctx(warehouses={"events": events, "billing": billing})

    text = catalog.build_catalog(ctx)

    assert "web.pageviews (ts, url)" in text
    assert "SOURCE billing [clickhouse]" in text
    assert "UNAVAILABLE: password authentication failed" in text


def test_a_connectionless_org_gets_a_catalog_that_says_so():
    ctx = make_ctx(warehouses={})
    assert "no data sources" in catalog.build_catalog(ctx)


# --- describe_source ----------------------------------------------------------


def test_describe_source_returns_detail_for_the_named_source():
    events, billing = _two_sources()
    messages = _seed()
    ctx = make_ctx(
        openai=FakeOpenAI(
            [
                response(message(tool_calls=[describe_call("c1", "web.pageviews", "events")])),
                response(message(content="done")),
            ]
        ),
        warehouses={"events": events, "billing": billing},
    )

    run_capture_loop(messages, ctx=ctx)

    detail = json.loads(
        [m for m in messages if m.get("role") == "tool"][0]["content"]
    )["detail"]
    assert "TABLE web.pageviews" in detail
    assert "ts String" in detail


def test_describe_source_on_an_unknown_source_is_a_tool_error():
    events, billing = _two_sources()
    messages = _seed()
    ctx = make_ctx(
        openai=FakeOpenAI(
            [
                response(message(tool_calls=[describe_call("c1", "t", "nope")])),
                response(message(content="done")),
            ]
        ),
        warehouses={"events": events, "billing": billing},
    )

    run_capture_loop(messages, ctx=ctx)

    payload = json.loads([m for m in messages if m.get("role") == "tool"][0]["content"])
    assert "nope" in payload["error"]


# --- the catalog the model actually sees --------------------------------------


def test_the_catalog_lists_every_source_with_its_engine():
    from datatalk.warehouse.postgres import POSTGRES_DIALECT

    events, billing = _two_sources()
    billing.dialect = POSTGRES_DIALECT
    ctx = make_ctx(warehouses={"events": events, "billing": billing})

    text = catalog.build_catalog(ctx)

    assert "SOURCE events [clickhouse]" in text
    assert "SOURCE billing [postgres]" in text
    # Column names only -- types and samples cost too much across N warehouses.
    assert "web.pageviews (ts, url)" in text
    assert "ts String" not in text
    # Both dialect hints, since both engines are in play.
    assert "ClickHouse dialect" in text
    assert "PostgreSQL dialect" in text


def test_the_catalog_states_the_no_cross_source_join_rule():
    """Without this the model writes a join and gets an unexplainable error."""
    events, billing = _two_sources()
    ctx = make_ctx(warehouses={"events": events, "billing": billing})

    text = catalog.build_catalog(ctx)
    assert "cannot join across" in text.lower()


def test_a_single_source_org_is_not_told_about_joining():
    events, _ = _two_sources()
    ctx = make_ctx(warehouses={"events": events})

    text = catalog.build_catalog(ctx)
    assert "cannot join across" not in text.lower()
    # But it is still told to name the source, because run_sql requires it.
    assert "name a source" in text.lower()


# --- the pipeline end to end ---------------------------------------------------


def test_a_report_composes_datasets_from_two_sources():
    """The end the user sees: one document, blocks fed by different warehouses."""
    events, billing = _two_sources()
    plan = json.dumps(
        {
            "sections": [
                {
                    "id": "both",
                    "title": "Both",
                    "goal": "Traffic and revenue",
                    "data_questions": ["How much of each?"],
                }
            ]
        }
    )
    reporter = json.dumps(
        {
            "blocks": [
                {"type": "table", "dataset_id": "q1", "columns": ["url", "hits"]},
                {"type": "table", "dataset_id": "q2", "columns": ["amount"]},
            ]
        }
    )
    ctx = make_ctx(
        openai=FakeOpenAI(
            [
                response(message(content=plan)),  # Planner
                response(
                    message(
                        tool_calls=[
                            fn_call("c1", "SELECT url, hits FROM web.pageviews", "events"),
                            fn_call("c2", "SELECT amount FROM public.invoices", "billing"),
                        ]
                    )
                ),
                response(message(content="Data gathering complete.")),
                response(message(content=reporter)),  # Reporter
            ]
        ),
        warehouses={"events": events, "billing": billing},
    )

    result = report_mod.generate_report("traffic and revenue", ctx=ctx)

    rows = [b.rows for b in result.document.blocks if hasattr(b, "rows")]
    assert rows == [[["/home", 10]], [[99]]]
    # Provenance survives into what gets persisted.
    assert {q["source"] for q in result.queries} == {"events", "billing"}


def test_the_reporter_is_told_which_source_each_dataset_came_from():
    """Otherwise it cannot label two similarly shaped tables from two warehouses."""
    from datatalk.agent.sqlloop import dataset_previews
    from datatalk.warehouse.base import QueryResult

    datasets = {"q1": QueryResult(["a"], [[1]], 1, False, "SELECT a")}
    preview = dataset_previews(datasets, sources={"q1": "billing"})
    assert '"source": "billing"' in preview


@pytest.mark.parametrize("name", ["events", "billing"])
def test_source_resolution_is_exact_not_prefix(name):
    """`bill` must not silently resolve to `billing`."""
    events, billing = _two_sources()
    ctx = make_ctx(warehouses={"events": events, "billing": billing})

    assert ctx.source(name).name == name
    with pytest.raises(UnknownSourceError):
        ctx.source(name[:3])
