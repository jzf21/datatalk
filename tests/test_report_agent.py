"""Multi-agent pipeline wiring test with a fake OpenAI client (no live DB/API).

Asserts the wiring Planner -> Analyst -> Reporter yields a valid materialized
Document, and that the Reporter never invents numbers (materialized values equal
the captured dataset values).
"""

import json

import datatalk.agent.report as report_mod
import datatalk.agent.sqlloop as sqlloop_mod
from datatalk.agent.blocks import Chart, Table
from datatalk.agent.executor import QueryResult
from tests.conftest import FakeOpenAI, make_ctx
from tests.conftest import fn_call as _fn_call
from tests.conftest import message as _message
from tests.conftest import response as _response


def test_pipeline_yields_materialized_document(monkeypatch):
    plan_json = json.dumps(
        {
            "sections": [
                {
                    "id": "trends",
                    "title": "Trends",
                    "goal": "Show monthly issue counts",
                    "data_questions": ["How many issues per month?"],
                }
            ]
        }
    )
    reporter_json = json.dumps(
        {
            "blocks": [
                {"type": "heading", "level": 1, "text": "Trends"},
                {"type": "table", "dataset_id": "q1", "columns": ["month", "issues"]},
                {
                    "type": "chart",
                    "chart_type": "bar",
                    "title": "Issues by month",
                    "dataset_id": "q1",
                    "x_col": "month",
                    "series_cols": ["issues"],
                },
            ]
        }
    )
    scripted = [
        _response(_message(content=plan_json)),  # Planner
        _response(_message(tool_calls=[_fn_call("c1", "SELECT month, count() FROM jira.issues GROUP BY month")])),  # Analyst step 1
        _response(_message(content="Data gathering complete.")),  # Analyst step 2
        _response(_message(content=reporter_json)),  # Reporter
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    monkeypatch.setattr(
        report_mod, "build_catalog", lambda ctx, **kw: "SOURCE main [clickhouse]\n  jira.issues (month, issues)"
    )

    captured_rows = [["2026-01", 10], ["2026-02", 20]]

    def fake_run_sql(sql, *, ctx=None, source=None):
        return QueryResult(
            columns=["month", "issues"],
            rows=[list(r) for r in captured_rows],
            row_count=2,
            truncated=False,
            sql=sql,
        )

    monkeypatch.setattr(sqlloop_mod, "run_sql", fake_run_sql)

    events = []
    result = report_mod.generate_report(
        "Monthly issue trends",
        ctx=ctx,
        on_event=lambda kind, data: events.append((kind, data)),
    )

    blocks = result.document.blocks
    table = next(b for b in blocks if isinstance(b, Table))
    chart = next(b for b in blocks if isinstance(b, Chart))

    # Reporter never invents numbers: materialized values == captured dataset.
    assert table.rows == captured_rows
    assert chart.series == [{"name": "issues", "values": [10, 20]}]
    assert chart.x == {"label": "month", "values": ["2026-01", "2026-02"]}

    # One dataset captured, and the plan/report events were emitted.
    assert len(result.queries) == 1
    assert result.queries[0]["dataset_id"] == "q1"
    kinds = [k for k, _ in events]
    assert "plan" in kinds and "sql" in kinds and "report" in kinds


def test_bad_dataset_reference_degrades_not_raises(monkeypatch):
    plan_json = json.dumps({"sections": [{"id": "s", "title": "S", "goal": "", "data_questions": []}]})
    # Reporter references a dataset id that was never captured.
    reporter_json = json.dumps(
        {"blocks": [{"type": "table", "dataset_id": "q9", "columns": ["x"]}]}
    )
    scripted = [
        _response(_message(content=plan_json)),
        _response(_message(content="Data gathering complete.")),  # Analyst runs no queries
        _response(_message(content=reporter_json)),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    monkeypatch.setattr(report_mod, "build_catalog", lambda ctx, **kw: "SOURCE main [clickhouse]")

    result = report_mod.generate_report("anything", ctx=ctx)
    # Degraded to a paragraph note rather than raising.
    from datatalk.agent.blocks import Paragraph

    assert isinstance(result.document.blocks[0], Paragraph)
    assert "unavailable" in result.document.blocks[0].text


def test_the_report_path_still_uses_the_report_prompts(monkeypatch):
    """The dashboard's prompt overrides must not have leaked into reports.

    ``plan_report`` and ``gather_data`` grew a ``system_prompt`` parameter for
    the dashboard. Its default is the report prompt, and this is the assertion
    that keeps it that way.
    """
    plan_json = json.dumps(
        {"sections": [{"id": "s", "title": "S", "goal": "g", "data_questions": ["q?"]}]}
    )
    scripted = [
        _response(_message(content=plan_json)),
        _response(_message(content="Data gathering complete.")),
        _response(_message(content=json.dumps({"blocks": [{"type": "paragraph", "text": "hi"}]}))),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    monkeypatch.setattr(report_mod, "build_catalog", lambda ctx, **kw: "SOURCE main [clickhouse]")

    report_mod.generate_report("anything", ctx=ctx)
    kwargs = ctx.openai.chat.completions.kwargs

    planner_system = kwargs[0]["messages"][0]["content"]
    assert "reporting pipeline" in planner_system
    assert "DASHBOARD" not in planner_system

    analyst_system = kwargs[1]["messages"][0]["content"]
    assert "reporting pipeline" in analyst_system
    assert "DASHBOARD" not in analyst_system


def test_the_reporter_sees_user_guidance(monkeypatch):
    """Learned guidance shapes wording and units, which the Reporter decides."""
    plan_json = json.dumps(
        {"sections": [{"id": "s", "title": "S", "goal": "g", "data_questions": ["q?"]}]}
    )
    scripted = [
        _response(_message(content=plan_json)),
        _response(_message(content="Data gathering complete.")),
        _response(_message(content=json.dumps({"blocks": [{"type": "paragraph", "text": "hi"}]}))),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    monkeypatch.setattr(report_mod, "build_catalog", lambda ctx, **kw: "SOURCE main [clickhouse]")

    report_mod.generate_report(
        "anything", ctx=ctx, memory_suggestions=["Report revenue in millions"])

    reporter_system = ctx.openai.chat.completions.kwargs[-1]["messages"][0]["content"]
    assert "Report revenue in millions" in reporter_system


def test_a_deferred_memory_fetch_reaches_the_agents_and_emits_the_event(monkeypatch):
    """memory_suggestions_fn overlaps build_catalog; its result must still land
    in every agent prompt and surface as a `memory` stream event."""
    plan_json = json.dumps(
        {"sections": [{"id": "s", "title": "S", "goal": "g", "data_questions": ["q?"]}]}
    )
    scripted = [
        _response(_message(content=plan_json)),
        _response(_message(content="Data gathering complete.")),
        _response(_message(content=json.dumps({"blocks": [{"type": "paragraph", "text": "hi"}]}))),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    monkeypatch.setattr(report_mod, "build_catalog", lambda ctx, **kw: "SOURCE main [clickhouse]")
    events = []

    report_mod.generate_report(
        "anything",
        ctx=ctx,
        memory_suggestions_fn=lambda: ["Report revenue in millions"],
        on_event=lambda k, d: events.append((k, d)),
    )

    memory_events = [d for k, d in events if k == "memory"]
    assert memory_events == [
        {"count": 1, "suggestions": ["Report revenue in millions"]}
    ]
    reporter_system = ctx.openai.chat.completions.kwargs[-1]["messages"][0]["content"]
    assert "Report revenue in millions" in reporter_system


def test_a_crashing_memory_fetch_degrades_to_no_suggestions(monkeypatch):
    """Memory is best-effort: a dead embeddings endpoint must not kill the run."""
    plan_json = json.dumps(
        {"sections": [{"id": "s", "title": "S", "goal": "g", "data_questions": ["q?"]}]}
    )
    scripted = [
        _response(_message(content=plan_json)),
        _response(_message(content="Data gathering complete.")),
        _response(_message(content=json.dumps({"blocks": [{"type": "paragraph", "text": "hi"}]}))),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    monkeypatch.setattr(report_mod, "build_catalog", lambda ctx, **kw: "SOURCE main [clickhouse]")
    events = []

    def boom():
        raise RuntimeError("embeddings down")

    result = report_mod.generate_report(
        "anything",
        ctx=ctx,
        memory_suggestions_fn=boom,
        on_event=lambda k, d: events.append((k, d)),
    )

    assert result.document.blocks
    assert not [k for k, _ in events if k == "memory"]
