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
import datatalk.agent.sqlloop as sqlloop_mod
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
    assert 'SOURCE "billing" [clickhouse]' in text
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

    # Quoted: unquoted beside `database.table` lines the handle reads like one
    # more schema name, which is exactly the mistake the next test guards.
    assert 'SOURCE "events" [clickhouse]' in text
    assert 'SOURCE "billing" [postgres]' in text
    # Column names only -- types and samples cost too much across N warehouses.
    assert "web.pageviews (ts, url)" in text
    assert "ts String" not in text
    # Both dialect hints, since both engines are in play.
    assert "ClickHouse dialect" in text
    assert "PostgreSQL dialect" in text


def test_the_catalog_says_a_source_name_is_not_a_database():
    """The confusion this prevents: a source called `analytics` whose tables
    live in some other database, and a model writing `FROM analytics.events`."""
    events, _ = _two_sources()
    ctx = make_ctx(warehouses={"events": events})

    text = catalog.build_catalog(ctx).lower()
    assert "not a database" in text
    assert "never write it inside sql" in text


def test_the_catalog_names_the_scope_when_the_source_is_narrowed():
    """Otherwise the model keeps reaching for a database this source hides and
    reads the error as a transient failure worth retrying."""
    import dataclasses

    events, _ = _two_sources()
    ctx = make_ctx(warehouses={"events": events})
    scoped = dataclasses.replace(
        ctx.sources[0],
        spec=dataclasses.replace(
            ctx.sources[0].spec, introspect_databases=("web",)
        ),
    )
    ctx = ctx.with_sources((scoped,))

    text = catalog.build_catalog(ctx)
    assert "scopes this source to the database web" in text


def test_an_unscoped_source_gets_no_scope_line():
    """An org that has not narrowed anything must see the catalog it always saw."""
    events, _ = _two_sources()
    text = catalog.build_catalog(make_ctx(warehouses={"events": events}))
    assert "scopes this source" not in text


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


def test_read_context_bodies_are_capped_with_a_visible_marker():
    """A context file body must not be handed back in full -- and a clipped one
    must say so.

    Bodies are stored up to 16k chars and the loop resends its whole history
    every turn, so one uncapped call is paid for again on every remaining step.
    The cut used to be silent, which read as a complete file; now the model is
    told how much is missing. An explicit ``read_context`` gets the larger cap
    (the model asked for this file); ``_describe``'s unrequested attachments
    keep the tighter one.
    """
    from datatalk.context import ContextFile, ContextModel

    big = "x" * 16_000
    model = ContextModel(files=(
        ContextFile(id=1, path="ontology/orders.md", summary="orders", body_md=big),
    ))
    ctx = make_ctx(context_model=model)

    payload = json.loads(
        sqlloop_mod._read_context(ctx, "ontology/orders.md", lambda k, d: None)
    )
    assert payload["path"] == "ontology/orders.md"
    assert len(payload["detail"]) < len(big)
    assert f"truncated at {sqlloop_mod._READ_CONTEXT_BODY_CHARS} of 16000" in payload["detail"]


def test_short_context_bodies_are_not_marked():
    from datatalk.context import ContextFile, ContextModel

    model = ContextModel(files=(
        ContextFile(id=1, path="ontology/orders.md", summary="orders", body_md="short"),
    ))
    ctx = make_ctx(context_model=model)

    payload = json.loads(
        sqlloop_mod._read_context(ctx, "ontology/orders.md", lambda k, d: None)
    )
    assert payload["detail"] == "short"


def test_read_context_offers_real_paths_on_a_miss():
    """A mistyped path must self-correct rather than burn the remaining steps."""
    from datatalk.context import ContextFile, ContextModel

    model = ContextModel(files=(
        ContextFile(id=1, path="playbooks/churn.md", summary="churn", body_md="body"),
    ))
    ctx = make_ctx(context_model=model)

    payload = json.loads(
        sqlloop_mod._read_context(ctx, "ontology/nope.md", lambda k, d: None)
    )
    assert "playbooks/churn.md" in payload["error"]


def _context_model(*files):
    from datatalk.context import ContextModel

    return ContextModel(files=tuple(files))


def _context_file(id, path, body, covers=(), summary=""):
    from datatalk.context import ContextFile

    return ContextFile(
        id=id, path=path, summary=summary, body_md=body, covers=tuple(covers)
    )


def test_read_context_reads_several_files_in_one_call():
    """One batched call replaces N turns, and every turn resends the whole
    history — reading the ontology file and the playbook together is the
    single biggest retrieval saving the loop can make."""
    ctx = make_ctx(
        context_model=_context_model(
            _context_file(1, "ontology/orders.md", "ORDERS"),
            _context_file(2, "playbooks/churn.md", "CHURN"),
        )
    )

    payload = json.loads(
        sqlloop_mod._read_context_batch(
            ctx, ["ontology/orders.md", "playbooks/churn.md"], lambda k, d: None
        )
    )
    assert [f["path"] for f in payload["files"]] == [
        "ontology/orders.md",
        "playbooks/churn.md",
    ]
    assert [f["detail"] for f in payload["files"]] == ["ORDERS", "CHURN"]


def test_read_context_batch_reports_misses_per_file():
    """One typo must not void the files that did resolve."""
    ctx = make_ctx(
        context_model=_context_model(_context_file(1, "ontology/orders.md", "ORDERS"))
    )

    payload = json.loads(
        sqlloop_mod._read_context_batch(
            ctx, ["ontology/orders.md", "ontology/nope.md"], lambda k, d: None
        )
    )
    good, bad = payload["files"]
    assert good["detail"] == "ORDERS"
    assert "ontology/orders.md" in bad["error"]


def test_read_context_batch_caps_the_path_count():
    files = [_context_file(i, f"ontology/t{i}.md", f"B{i}") for i in range(6)]
    ctx = make_ctx(context_model=_context_model(*files))

    payload = json.loads(
        sqlloop_mod._read_context_batch(
            ctx, [f.path for f in files], lambda k, d: None
        )
    )
    bodies = [f for f in payload["files"] if "detail" in f]
    assert len(bodies) == sqlloop_mod._READ_CONTEXT_MAX_PATHS
    assert "Only the first" in payload["files"][-1]["error"]


def test_context_bodies_are_not_resent_within_a_run():
    """The loop resends its whole history every turn, so a body delivered once
    is still in front of the model. Describing two tables covered by the same
    ontology file must pay for that file once, not twice."""
    model = _context_model(
        _context_file(
            1,
            "ontology/orders.md",
            "ORDERBODY",
            covers=(("main", "db.orders"), ("main", "db.order_items")),
            summary="orders",
        )
    )
    wh = FakeWarehouse(tables=[fake_table("orders"), fake_table("order_items")])
    ctx = make_ctx(warehouses={"main": wh}, context_model=model)
    seen: dict[str, int] = {}

    first = json.loads(
        sqlloop_mod._describe(ctx, "main", "db.orders", lambda k, d: None, seen)
    )
    second = json.loads(
        sqlloop_mod._describe(ctx, "main", "db.order_items", lambda k, d: None, seen)
    )

    assert first["context_files"][0]["body_md"] == "ORDERBODY"
    assert "body_md" not in second["context_files"][0]
    assert "already provided" in second["context_files"][0]["note"]


def test_read_context_upgrades_a_clipped_describe_attachment():
    """An explicit read_context after a clipped describe attachment is a real
    upgrade (8k cap vs 4k) and must deliver the fuller body — but a second
    read of the same file collapses to a pointer."""
    body = "x" * 6000
    model = _context_model(
        _context_file(
            1, "ontology/orders.md", body, covers=(("main", "db.orders"),)
        )
    )
    wh = FakeWarehouse(tables=[fake_table("orders")])
    ctx = make_ctx(warehouses={"main": wh}, context_model=model)
    seen: dict[str, int] = {}

    described = json.loads(
        sqlloop_mod._describe(ctx, "main", "db.orders", lambda k, d: None, seen)
    )
    assert f"truncated at {sqlloop_mod._CONTEXT_BODY_CHARS}" in (
        described["context_files"][0]["body_md"]
    )

    read = json.loads(
        sqlloop_mod._read_context(ctx, "ontology/orders.md", lambda k, d: None, seen)
    )
    assert read["detail"] == body  # under the 8k read cap, so complete

    again = json.loads(
        sqlloop_mod._read_context(ctx, "ontology/orders.md", lambda k, d: None, seen)
    )
    assert "detail" not in again
    assert "already provided" in again["note"]


def test_the_loop_dispatches_a_batched_read_context_call():
    """The wire form: the model sends `paths` as a list and gets every body in
    one tool result."""
    from types import SimpleNamespace

    read_call = SimpleNamespace(
        id="c1",
        function=SimpleNamespace(
            name="read_context",
            arguments=json.dumps(
                {"paths": ["ontology/orders.md", "playbooks/churn.md"]}
            ),
        ),
    )
    ctx = make_ctx(
        openai=FakeOpenAI(
            [
                response(message(tool_calls=[read_call])),
                response(message(content="done")),
            ]
        ),
        context_model=_context_model(
            _context_file(1, "ontology/orders.md", "ORDERS"),
            _context_file(2, "playbooks/churn.md", "CHURN"),
        ),
    )
    seed = _seed()

    result = run_capture_loop(seed, ctx=ctx)

    tool_msgs = [m for m in seed if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    payload = json.loads(tool_msgs[0]["content"])
    assert [f["detail"] for f in payload["files"]] == ["ORDERS", "CHURN"]
    assert result.final_content == "done"


# --- batched turns run concurrently -------------------------------------------


class _BarrierWarehouse(FakeWarehouse):
    """Only releases a query when ``parties`` queries are in flight at once.

    Run serially, the first query times out at the barrier -- so a passing
    test proves concurrency by construction, not by timing.
    """

    def __init__(self, barrier, **kw):
        super().__init__(**kw)
        self._barrier = barrier

    def query(self, sql, *, timeout_s, max_rows):
        self._barrier.wait(timeout=5)
        return super().query(sql, timeout_s=timeout_s, max_rows=max_rows)


def test_a_batched_turn_runs_its_queries_concurrently():
    import threading

    barrier = threading.Barrier(3)
    wh = _BarrierWarehouse(barrier, columns=["n"], rows=[[1]])
    events: list[tuple[str, dict]] = []
    messages = _seed()
    ctx = make_ctx(
        openai=FakeOpenAI(
            [
                response(
                    message(
                        tool_calls=[
                            fn_call("c1", "SELECT 1"),
                            fn_call("c2", "SELECT 2"),
                            fn_call("c3", "SELECT 3"),
                        ]
                    )
                ),
                response(message(content="done")),
            ]
        ),
        warehouses={"main": wh},
    )

    result = run_capture_loop(
        messages, ctx=ctx, on_event=lambda k, d: events.append((k, d))
    )

    # Ids are assigned in tool_calls order regardless of completion order.
    assert list(result.datasets) == ["q1", "q2", "q3"]
    assert [q["dataset_id"] for q in result.queries] == ["q1", "q2", "q3"]
    assert [q["sql"] for q in result.queries] == [
        "SELECT 1\nLIMIT 1000",
        "SELECT 2\nLIMIT 1000",
        "SELECT 3\nLIMIT 1000",
    ]
    # Tool results go back to the model in the order the calls were made.
    tool_ids = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
    assert tool_ids == ["c1", "c2", "c3"]
    # sql/result events correlate through query_id.
    sql_ids = [d["query_id"] for k, d in events if k == "sql"]
    assert sql_ids == ["c1", "c2", "c3"]
    results = {d["query_id"]: d["dataset_id"] for k, d in events if k == "result"}
    assert results == {"c1": "q1", "c2": "q2", "c3": "q3"}


def test_a_failure_in_a_batched_turn_does_not_consume_a_dataset_id():
    """Numbering is what it always was: successes only, in call order."""
    events_wh, billing = _two_sources()
    billing.fail = WarehouseError("relation does not exist")
    messages = _seed()
    stream: list[tuple[str, dict]] = []
    ctx = make_ctx(
        openai=FakeOpenAI(
            [
                response(
                    message(
                        tool_calls=[
                            fn_call("c1", "SELECT 1", "billing"),
                            fn_call("c2", "SELECT 2", "events"),
                        ]
                    )
                ),
                response(message(content="done")),
            ]
        ),
        warehouses={"events": events_wh, "billing": billing},
    )

    result = run_capture_loop(
        messages, ctx=ctx, on_event=lambda k, d: stream.append((k, d))
    )

    assert result.dataset_sources == {"q1": "events"}
    errors = [d for k, d in stream if k == "error"]
    assert errors and errors[0]["query_id"] == "c1"
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert "relation does not exist" in json.loads(tool_msgs[0]["content"])["error"]


def test_an_exceeded_deadline_finalizes_with_what_was_captured():
    """The wall-clock budget bounds a slow warehouse without discarding data."""

    class SlowWarehouse(FakeWarehouse):
        def query(self, sql, *, timeout_s, max_rows):
            import time

            time.sleep(0.05)  # far past the 10ms budget below
            return super().query(sql, timeout_s=timeout_s, max_rows=max_rows)

    messages = _seed()
    ctx = make_ctx(
        openai=FakeOpenAI(
            [
                response(message(tool_calls=[fn_call("c1", "SELECT 1")])),
                # The deadline gate fires before turn 2; this is forced-finalize.
                response(message(content="ran out of time")),
            ]
        ),
        warehouses={"main": SlowWarehouse()},
    )

    result = run_capture_loop(messages, ctx=ctx, deadline_s=0.01)

    assert result.dataset_sources == {"q1": "main"}
    assert result.final_content == "ran out of time"
    assert result.steps == 1
    assert ctx.openai.chat.completions.calls == 2


# --- history compaction and the error budget ----------------------------------


def test_old_large_tool_results_are_compacted_exactly_once():
    """The loop resends history every turn; a big result older than two turns
    shrinks to a stub — once, so the provider's prefix cache stays warm."""
    import copy

    big = FakeWarehouse(columns=["n"], rows=[[i] for i in range(30)])
    small = FakeWarehouse(columns=["n"], rows=[[1]])
    fake = FakeOpenAI(
        [
            response(message(tool_calls=[fn_call("c1", "SELECT big", "big")])),
            response(message(tool_calls=[fn_call("c2", "SELECT 1", "small")])),
            response(message(tool_calls=[fn_call("c3", "SELECT 2", "small")])),
            response(message(tool_calls=[fn_call("c4", "SELECT 3", "small")])),
            response(message(content="done")),
        ]
    )
    snapshots = []
    real_create = fake.chat.completions.create

    def create(**kwargs):
        snapshots.append(copy.deepcopy(kwargs["messages"]))
        return real_create(**kwargs)

    fake.chat.completions.create = create
    ctx = make_ctx(openai=fake, warehouses={"big": big, "small": small})

    result = run_capture_loop(_seed(), ctx=ctx, max_steps=8)

    def q1_content(snapshot):
        return next(
            m["content"] for m in snapshot if m.get("role") == "tool"
        )

    # Turn 3 (age 2): still full fidelity. Turn 4 (age 3): compacted.
    assert json.loads(q1_content(snapshots[2]))["rows_shown"] == 30
    compacted = json.loads(q1_content(snapshots[3]))
    assert compacted["rows_shown"] == 5
    assert compacted["dataset_id"] == "q1"
    assert "compacted" in compacted["note"]
    # Rewritten exactly once: turn 5 sees the identical bytes.
    assert q1_content(snapshots[4]) == q1_content(snapshots[3])
    # The captured dataset itself keeps every row.
    assert len(result.datasets["q1"].rows) == 30


def test_all_error_turns_burn_the_error_budget_not_the_step_budget():
    """A model recovering from a dialect misunderstanding still gets its
    max_steps of productive turns."""
    events_wh, billing = _two_sources()
    billing.fail = WarehouseError("syntax error near LIMIT")
    ctx = make_ctx(
        openai=FakeOpenAI(
            [
                response(message(tool_calls=[fn_call("c1", "SELECT 1", "billing")])),
                response(message(tool_calls=[fn_call("c2", "SELECT 1", "events")])),
                response(message(content="done")),
            ]
        ),
        warehouses={"events": events_wh, "billing": billing},
    )

    # max_steps=1: without the refund, the failed first turn would exhaust the
    # budget and the successful retry would never run.
    result = run_capture_loop(_seed(), ctx=ctx, max_steps=1)

    assert result.dataset_sources == {"q1": "events"}
    assert result.final_content == "done"
    assert result.steps == 1


def test_the_error_budget_itself_is_bounded():
    """Past the budget, failed turns consume steps again and the loop ends."""
    events_wh, billing = _two_sources()
    billing.fail = WarehouseError("still broken")
    failing_turn = lambda cid: response(
        message(tool_calls=[fn_call(cid, "SELECT 1", "billing")])
    )
    ctx = make_ctx(
        openai=FakeOpenAI(
            [
                failing_turn("c1"),
                failing_turn("c2"),
                failing_turn("c3"),
                failing_turn("c4"),  # budget (3) exhausted: this one costs the step
                response(message(content="gave up")),  # forced finalize
            ]
        ),
        warehouses={"events": events_wh, "billing": billing},
    )

    result = run_capture_loop(_seed(), ctx=ctx, max_steps=1, error_budget=3)

    assert result.datasets == {}
    assert result.final_content == "gave up"
    assert ctx.openai.chat.completions.calls == 5


def test_nan_reaches_the_model_as_null_not_the_string_nan():
    """avg() over an empty group is an ordinary warehouse result; the model
    must see JSON null, not a "nan" string it would read as a value."""
    wh = FakeWarehouse(columns=["avg"], rows=[[float("nan")]])
    messages = _seed()
    ctx = make_ctx(
        openai=FakeOpenAI(
            [
                response(message(tool_calls=[fn_call("c1", "SELECT avg(x) FROM t")])),
                response(message(content="done")),
            ]
        ),
        warehouses={"main": wh},
    )

    run_capture_loop(messages, ctx=ctx)

    tool_msg = next(m for m in messages if m.get("role") == "tool")
    assert json.loads(tool_msg["content"])["rows"] == [[None]]
    assert "nan" not in tool_msg["content"].lower()
