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
