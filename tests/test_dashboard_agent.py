"""Dashboard pipeline test with a scripted fake OpenAI client (no live DB/API)."""

import json

import datatalk.agent.dashboard as dashboard_mod
import datatalk.agent.sqlloop as sqlloop_mod
from datatalk.agent.blocks import Row, Stat
from datatalk.agent.executor import QueryResult
from tests.conftest import FakeOpenAI, make_ctx
from tests.conftest import fn_call as _fn_call
from tests.conftest import message as _message
from tests.conftest import response as _response



def test_generate_dashboard_materializes_grid(monkeypatch):
    plan_json = json.dumps(
        {"sections": [{"id": "kpis", "title": "KPIs", "goal": "headline metrics",
                       "data_questions": ["totals?"]}]}
    )
    dash_json = json.dumps(
        {"blocks": [
            {"type": "row", "children": [
                {"type": "stat", "dataset_id": "q1", "value_col": "current",
                 "label": "Revenue", "delta_col": "prior", "unit": "$", "width": 6},
            ]},
            {"type": "chart", "chart_type": "bar", "title": "By metric",
             "dataset_id": "q1", "x_col": "metric", "series_cols": ["current"]},
        ]}
    )
    scripted = [
        _response(_message(content=plan_json)),                                   # Planner
        _response(_message(tool_calls=[_fn_call("c1", "SELECT metric, current, prior FROM t")])),  # Analyst
        _response(_message(content="Data gathering complete.")),                  # Analyst stop
        _response(_message(content=dash_json)),                                   # Dashboard author
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    monkeypatch.setattr(dashboard_mod, "get_schema_context", lambda ctx, **kw: "TABLE t")

    def fake_run_sql(sql, *, ctx=None):
        return QueryResult(columns=["metric", "current", "prior"],
                           rows=[["revenue", 120, 100]], row_count=1,
                           truncated=False, sql=sql)

    monkeypatch.setattr(sqlloop_mod, "run_sql", fake_run_sql)

    events = []
    result = dashboard_mod.generate_dashboard(
        "revenue dashboard", ctx=ctx, on_event=lambda k, d: events.append((k, d)))

    row = next(b for b in result.document.blocks if isinstance(b, Row))
    stat = row.children[0]
    assert isinstance(stat, Stat)
    assert stat.value == 120 and stat.delta == 20.0 and stat.delta_pct == 20.0
    assert stat.width == 6
    assert len(result.queries) == 1
    kinds = [k for k, _ in events]
    assert "plan" in kinds and "sql" in kinds and "dashboard" in kinds


def test_generate_dashboard_bad_reference_degrades(monkeypatch):
    from datatalk.agent.blocks import Paragraph
    plan_json = json.dumps({"sections": [{"id": "s", "title": "S", "goal": "", "data_questions": []}]})
    dash_json = json.dumps({"blocks": [{"type": "stat", "dataset_id": "q9",
                                        "value_col": "x", "label": "Nope"}]})
    scripted = [
        _response(_message(content=plan_json)),
        _response(_message(content="Data gathering complete.")),  # no queries
        _response(_message(content=dash_json)),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    monkeypatch.setattr(dashboard_mod, "get_schema_context", lambda ctx, **kw: "schema")

    result = dashboard_mod.generate_dashboard("anything", ctx=ctx)
    assert isinstance(result.document.blocks[0], Paragraph)
    assert "unavailable" in result.document.blocks[0].text
