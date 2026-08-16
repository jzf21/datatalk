"""The runner's wiring, driven by the same fakes every other agent test uses.

No live model and no warehouse. What is checked here is that the harness scores
a *real agent run* correctly end to end: that the reference query and the
agent's query go to the right sources, that a match is a pass and a mismatch is
a failure with a reason, that routing and token accounting are recorded, and
that one exploding case does not void the arm around it.

The accuracy number itself is not testable here -- it is a property of a model
against a warehouse, which is the whole reason the suite is a CLI.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from datatalk.context import TenantContext
from datatalk.evals.cases import parse_case
from datatalk.evals.cases import Suite
from datatalk.evals.runner import (
    CountingOpenAI,
    compute_expected,
    run_arm,
)
from datatalk.warehouse.base import QueryResult
from datatalk.warehouse.clickhouse import CLICKHOUSE_DIALECT
from datatalk.warehouse.postgres import POSTGRES_DIALECT

from tests.conftest import FakeOpenAI, fake_table, fn_call, message, response


class ScriptedWarehouse:
    """A warehouse whose answer depends on the statement, so a reference query
    and an agent query can disagree -- which is the only interesting case."""

    def __init__(self, script, *, dialect=None, tables=()):
        # script: list of (substring, columns, rows)
        self.dialect = dialect or POSTGRES_DIALECT
        self.spec = None
        self._script = list(script)
        self._tables = list(tables)
        self.queries: list[str] = []

    def ping(self):
        return {"version": "test", "database": "test"}

    def query(self, sql, *, timeout_s, max_rows, parameters=None):
        self.queries.append(sql)
        for needle, columns, rows in self._script:
            if needle in sql:
                return QueryResult(
                    columns=list(columns),
                    rows=[list(r) for r in rows],
                    row_count=len(rows),
                    truncated=False,
                    sql=sql,
                )
        raise RuntimeError(f"unscripted statement: {sql}")

    def introspect(self, *, with_samples=True):
        return list(self._tables)

    def close(self):
        pass


REFERENCE_ROWS = [["Audio", 1200.5], ["Cameras", 900.0]]


def make_case(case_id="revenue-by-category", sources=("sales",), **expect):
    payload = {
        "id": case_id,
        "question": "What was revenue by category in Q1 2025?",
        "tags": ["aggregation"],
        "expect": {
            "sources": list(sources),
            "reference": {
                "source": "sales",
                "sql": "SELECT reference_marker, 1",
            },
            **expect,
        },
    }
    return parse_case(payload)


def make_ctx(*, agent_rows=REFERENCE_ROWS, scripted=(), events_rows=None):
    sales = ScriptedWarehouse(
        [
            ("reference_marker", ["category", "revenue"], REFERENCE_ROWS),
            ("agent_marker", ["cat", "rev"], agent_rows),
        ],
        tables=[fake_table("orders", database="public")],
    )
    events = ScriptedWarehouse(
        [("events_marker", ["channel", "spend"], events_rows or [["email", 40.0]])],
        dialect=CLICKHOUSE_DIALECT,
        tables=[fake_table("page_views", database="analytics")],
    )
    ctx = TenantContext.for_test(
        openai=FakeOpenAI(scripted),
        warehouses={"sales": sales, "events": events},
    )
    return ctx, sales, events


def analyst_script(sql="SELECT agent_marker, 1", source="sales", notes="Revenue was 1200.50 and 900."):
    """One tool-calling turn, then a final message -- the shape of a real run."""
    return [
        response(message(tool_calls=[fn_call("c1", sql, source=source)])),
        response(message(content=notes)),
    ]


def run_one(case, ctx, **kw):
    suite = Suite(name="test", cases=[case])
    expected = {case.id: compute_expected(case, ctx)}
    arm = run_arm(
        suite,
        ctx,
        name="test-arm",
        with_context_model=False,
        concurrency=1,  # the scripted client has one cursor; do not race it
        expected_by_case=expected,
        **kw,
    )
    return arm


# --- the happy path -----------------------------------------------------------


def test_a_matching_run_passes_and_names_the_dataset():
    case = make_case()
    ctx, sales, _ = make_ctx(scripted=analyst_script())
    arm = run_one(case, ctx)

    (result,) = arm.results
    assert result.passed
    assert result.dataset_id == "q1"
    assert result.query_count == 1
    assert arm.execution_accuracy == 1.0
    # The reference and the agent both reached the sales warehouse.
    assert any("reference_marker" in q for q in sales.queries)
    assert any("agent_marker" in q for q in sales.queries)


def test_a_wrong_answer_fails_with_a_reason():
    case = make_case()
    ctx, _, _ = make_ctx(
        agent_rows=[["Audio", 1200.5], ["Cameras", 901.0]],
        scripted=analyst_script(),
    )
    arm = run_one(case, ctx)

    (result,) = arm.results
    assert not result.passed
    assert result.reason
    assert arm.execution_accuracy == 0.0


def test_answer_fidelity_is_scored_from_the_written_text():
    """Right rows, wrong prose: a failure the execution match cannot see."""
    from datatalk.evals.runner import _EventCollector, score_case
    from datatalk.evals.runner import AgentRun

    case = make_case()
    ctx, _, _ = make_ctx()
    expected = compute_expected(case, ctx)
    run = AgentRun(
        datasets={"q1": expected},
        sources=("sales",),
        text="Revenue was strong across the board.",
        steps=2,
        query_count=1,
    )
    result = score_case(case, expected, run, _EventCollector(), check_answer=True)

    assert result.passed  # the data was right
    assert result.answer_ok is False  # the numbers never made it into the text
    assert set(result.answer_missing) == {"1200.5", "900"}


def test_answer_fidelity_is_not_scored_for_the_analyst_task():
    """The Analyst is told to stop with 'Data gathering complete'. Scoring it on
    whether the prose repeats the numbers would penalise it for obeying the
    anti-fabrication rule the whole product is built on."""
    case = make_case()
    ctx, _, _ = make_ctx(scripted=analyst_script(notes="Data gathering complete."))
    arm = run_one(case, ctx)

    (result,) = arm.results
    assert result.passed
    assert result.answer_ok is None
    assert arm.answer_fidelity is None  # not 0.0 -- it did not apply
    assert arm.answer_scored == 0


# --- routing ------------------------------------------------------------------


def test_querying_an_extra_source_is_a_routing_failure():
    case = make_case(sources=("sales",))
    ctx, _, _ = make_ctx(
        scripted=[
            response(
                message(
                    tool_calls=[
                        fn_call("c1", "SELECT agent_marker, 1", source="sales"),
                        fn_call("c2", "SELECT events_marker, 1", source="events"),
                    ]
                )
            ),
            response(message(content="Revenue was 1200.50 and 900.")),
        ]
    )
    arm = run_one(case, ctx)

    (result,) = arm.results
    assert result.passed  # it did find the answer...
    assert not result.routed_ok  # ...while also reading a source it did not need
    assert result.sources_queried == ("events", "sales")
    assert arm.routing_accuracy == 0.0


# --- failure handling ---------------------------------------------------------


def test_a_failing_query_is_counted_not_fatal():
    case = make_case()
    ctx, _, _ = make_ctx(
        scripted=[
            response(message(tool_calls=[fn_call("c1", "SELECT nonsense", source="sales")])),
            response(message(tool_calls=[fn_call("c2", "SELECT agent_marker, 1", source="sales")])),
            response(message(content="Revenue was 1200.50 and 900.")),
        ]
    )
    arm = run_one(case, ctx)

    (result,) = arm.results
    assert result.passed
    assert result.failed_queries == 1
    assert result.rejected_queries == 0


def test_an_attempted_write_is_recorded_as_a_rejected_query():
    """The guardrails refusing a statement is a finding, not a scoring problem."""
    case = make_case()
    ctx, _, _ = make_ctx(
        scripted=[
            response(
                message(tool_calls=[fn_call("c1", "DROP TABLE orders", source="sales")])
            ),
            response(message(tool_calls=[fn_call("c2", "SELECT agent_marker, 1", source="sales")])),
            response(message(content="Revenue was 1200.50 and 900.")),
        ]
    )
    arm = run_one(case, ctx)

    (result,) = arm.results
    assert result.rejected_queries == 1
    assert arm.rejected_queries == 1


def test_an_exploding_case_is_recorded_rather_than_voiding_the_arm():
    class Boom(FakeOpenAI):
        def __init__(self):
            super().__init__([])
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kw: (_ for _ in ()).throw(RuntimeError("upstream 500"))
                )
            )

    case = make_case()
    ctx, _, _ = make_ctx(scripted=analyst_script())
    ctx = ctx.__class__(**{**vars(ctx), "openai_override": Boom()})
    arm = run_one(case, ctx)

    (result,) = arm.results
    assert not result.passed
    assert "upstream 500" in result.error
    assert arm.error_rate == 1.0


# --- accounting ---------------------------------------------------------------


def test_counting_openai_tallies_usage_without_the_agent_knowing():
    inner = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kw: SimpleNamespace(
                    usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30)
                )
            )
        ),
        embeddings=None,
    )
    counter = CountingOpenAI(inner)
    counter.chat.completions.create(model="m", messages=[])
    counter.chat.completions.create(model="m", messages=[])
    assert counter.calls == 2
    assert counter.prompt_tokens == 240
    assert counter.completion_tokens == 60
    assert counter.total_tokens == 300


def test_usage_absent_from_a_response_does_not_break_the_run():
    """Not every OpenAI-compatible endpoint returns usage; a missing tally must
    not be an exception in the middle of a paid run."""
    counter = CountingOpenAI(
        SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kw: SimpleNamespace())
            ),
            embeddings=None,
        )
    )
    counter.chat.completions.create(model="m", messages=[])
    assert counter.calls == 1
    assert counter.total_tokens == 0


# --- expectations -------------------------------------------------------------


def test_compute_expected_runs_the_reference_through_the_guardrails():
    """The reference is held to the same read-only rules as the agent."""
    from datatalk.agent.executor import UnsafeSQLError

    case = parse_case(
        {
            "id": "unsafe",
            "question": "How many orders were placed in March 2025?",
            "expect": {"reference": {"source": "sales", "sql": "DELETE FROM orders"}},
        }
    )
    ctx, _, _ = make_ctx()
    with pytest.raises(UnsafeSQLError):
        compute_expected(case, ctx)


def test_compute_expected_combines_multi_source_steps():
    case = parse_case(
        {
            "id": "cross",
            "question": "What did an order cost per channel in Q1 2025?",
            "expect": {
                "sources": ["sales", "events"],
                "reference": [
                    {"source": "sales", "as": "o", "sql": "SELECT reference_marker, 1"},
                    {
                        "source": "events",
                        "as": "s",
                        "sql": "SELECT events_marker, 1",
                        # The fake events warehouse speaks ClickHouse, so the
                        # step needs its ClickHouse form -- exactly what the
                        # suite test asserts for the real cases.
                        "sql_clickhouse": "SELECT events_marker, 1",
                    },
                ],
                "combine": "SELECT category, spend FROM o JOIN s ON s.channel = 'email' ORDER BY category",
            },
        }
    )
    ctx, _, _ = make_ctx()
    expected = compute_expected(case, ctx)
    assert expected.columns == ["category", "spend"]
    assert expected.rows == [["Audio", 40.0], ["Cameras", 40.0]]


def test_expectations_are_computed_once_and_shared_across_arms():
    """Two arms must be scored against the same ground truth; recomputing it per
    arm would let a warehouse hiccup look like the effect being measured."""
    case = make_case()
    ctx, sales, _ = make_ctx(scripted=analyst_script())
    run_one(case, ctx)
    assert sum("reference_marker" in q for q in sales.queries) == 1
