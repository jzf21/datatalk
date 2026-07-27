"""Dashboard pipeline test with a scripted fake OpenAI client (no live DB/API)."""

import json

import datatalk.agent.dashboard as dashboard_mod
import datatalk.agent.sqlloop as sqlloop_mod
from datatalk.agent.blocks import Row, Stat, count_data_blocks
from datatalk.agent.executor import QueryResult
from tests.conftest import FakeOpenAI, make_ctx, make_settings
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
        _insight(),                                                               # Insight pass
        _response(_message(content=dash_json)),                                   # Dashboard author
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    monkeypatch.setattr(dashboard_mod, "build_catalog", lambda ctx, **kw: "SOURCE main [clickhouse]\n  db.t (a, b)")

    def fake_run_sql(sql, *, ctx=None, source=None):
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


_PLAN_JSON = json.dumps(
    {"sections": [{"id": "s", "title": "S", "goal": "", "data_questions": ["q?"]}]}
)


def _stub_catalog(monkeypatch, text="SOURCE main [clickhouse]\n  db.t (a, b)"):
    monkeypatch.setattr(dashboard_mod, "build_catalog", lambda ctx, **kw: text)


def _stub_one_dataset(monkeypatch, columns=("metric", "current"), rows=(["rev", 120],)):
    def fake_run_sql(sql, *, ctx=None, source=None):
        return QueryResult(columns=list(columns), rows=[list(r) for r in rows],
                           row_count=len(rows), truncated=False, sql=sql)

    monkeypatch.setattr(sqlloop_mod, "run_sql", fake_run_sql)


def _gathering(sql="SELECT metric, current FROM t"):
    """The two Analyst turns: one query, then stop."""
    return [
        _response(_message(tool_calls=[_fn_call("c1", sql)])),
        _response(_message(content="Data gathering complete.")),
    ]


def _insight(insights=None, **extra):
    """The insight pass's scripted reply. Empty findings by default."""
    payload = {"insights": insights or [], **extra}
    return _response(_message(content=json.dumps(payload)))


def test_no_datasets_skips_the_author_call(monkeypatch):
    """With nothing captured there is nothing to author, so we must not ask.

    The author's only possible reply is invented dataset ids, which all degrade
    to notes -- a page of them reads as a broken dashboard rather than as the
    empty result it actually is.
    """
    scripted = [
        _response(_message(content=_PLAN_JSON)),
        _response(_message(content="Data gathering complete.")),  # no queries
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    _stub_catalog(monkeypatch)

    events = []
    result = dashboard_mod.generate_dashboard(
        "anything", ctx=ctx, on_event=lambda k, d: events.append((k, d)))

    # Planner + one Analyst turn, and then we stopped: neither the insight pass
    # nor the author was called -- both would only be shown "(no datasets)".
    assert ctx.openai.chat.completions.calls == 2
    assert any(k == "error" for k, _ in events)
    text = " ".join(getattr(b, "text", "") for b in result.document.blocks)
    assert "No data was captured" in text
    assert result.queries == []


def test_a_bad_reference_triggers_one_repair(monkeypatch):
    """A hallucinated column is shown to the author and fixed, not shipped."""
    bad = json.dumps({"blocks": [{"type": "stat", "dataset_id": "q1",
                                  "value_col": "nope", "label": "Revenue"}]})
    good = json.dumps({"blocks": [{"type": "stat", "dataset_id": "q1",
                                   "value_col": "current", "label": "Revenue"}]})
    scripted = [
        _response(_message(content=_PLAN_JSON)),
        *_gathering(),
        _insight(),
        _response(_message(content=bad)),
        _response(_message(content=good)),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    _stub_catalog(monkeypatch)
    _stub_one_dataset(monkeypatch)

    result = dashboard_mod.generate_dashboard("revenue", ctx=ctx)

    stat = result.document.blocks[0]
    assert isinstance(stat, Stat) and stat.value == 120

    # The repair turn must name the bad column and list the real ones, or the
    # model is just being asked to guess again.
    repair = ctx.openai.chat.completions.kwargs[-1]["messages"][-1]["content"]
    assert "nope" in repair
    assert "current" in repair and "q1" in repair


def test_an_unparsable_reply_is_retried_then_explained(monkeypatch):
    """Two failed attempts yield an explained empty dashboard, never a blank one."""
    scripted = [
        _response(_message(content=_PLAN_JSON)),
        *_gathering(),
        _insight(),
        _response(_message(content="Here is your dashboard!"), finish_reason="length"),
        _response(_message(content="still not json")),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    _stub_catalog(monkeypatch)
    _stub_one_dataset(monkeypatch)

    events = []
    result = dashboard_mod.generate_dashboard(
        "revenue", ctx=ctx, on_event=lambda k, d: events.append((k, d)))

    assert any(k == "error" for k, _ in events)
    text = " ".join(getattr(b, "text", "") for b in result.document.blocks)
    assert "did not produce any usable blocks" in text
    assert "cut off" in text  # finish_reason="length" was surfaced, not swallowed
    # A dashboard event still fires: the run finished, it is simply empty.
    assert any(k == "dashboard" for k, _ in events)


def test_a_dashboard_with_data_is_not_retried(monkeypatch):
    """The repair turn must not fire on a clean document."""
    good = json.dumps({"blocks": [{"type": "stat", "dataset_id": "q1",
                                   "value_col": "current", "label": "Revenue"}]})
    scripted = [
        _response(_message(content=_PLAN_JSON)),
        *_gathering(),
        _insight(),
        _response(_message(content=good)),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    _stub_catalog(monkeypatch)
    _stub_one_dataset(monkeypatch)

    dashboard_mod.generate_dashboard("revenue", ctx=ctx)
    assert ctx.openai.chat.completions.calls == 5  # planner, 2 analyst, insight, author


def test_the_dashboard_agents_get_dashboard_prompts(monkeypatch):
    """Planner and Analyst must be told this is a dashboard, not a report.

    Nothing downstream can recover a KPI tile from a dataset shaped for prose,
    so the shape requirement has to arrive before the queries are written.
    """
    scripted = [
        _response(_message(content=_PLAN_JSON)),
        *_gathering(),
        _insight(),
        _response(_message(content=json.dumps({"blocks": [
            {"type": "stat", "dataset_id": "q1", "value_col": "current", "label": "R"}]}))),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    _stub_catalog(monkeypatch)
    _stub_one_dataset(monkeypatch)

    dashboard_mod.generate_dashboard("revenue", ctx=ctx)
    kwargs = ctx.openai.chat.completions.kwargs

    planner_system = kwargs[0]["messages"][0]["content"]
    assert "planning a DASHBOARD" in planner_system
    assert "EXACTLY ONE ROW" in planner_system

    analyst_system = kwargs[1]["messages"][0]["content"]
    assert "for a DASHBOARD" in analyst_system
    assert "reads ONE ROW" in analyst_system


def test_an_empty_plan_falls_back_to_the_request(monkeypatch):
    """A planner that returns nothing must not starve the Analyst."""
    scripted = [
        _response(_message(content=json.dumps({"sections": []}))),
        *_gathering(),
        _insight(),
        _response(_message(content=json.dumps({"blocks": [
            {"type": "stat", "dataset_id": "q1", "value_col": "current", "label": "R"}]}))),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    _stub_catalog(monkeypatch)
    _stub_one_dataset(monkeypatch)

    result = dashboard_mod.generate_dashboard("weekly revenue", ctx=ctx)
    analyst_user = ctx.openai.chat.completions.kwargs[1]["messages"][1]["content"]
    assert "weekly revenue" in analyst_user
    assert isinstance(result.document.blocks[0], Stat)


def test_the_author_sees_user_guidance(monkeypatch):
    """Learned guidance shapes tile labels, so it must reach the author."""
    scripted = [
        _response(_message(content=_PLAN_JSON)),
        *_gathering(),
        _insight(),
        _response(_message(content=json.dumps({"blocks": [
            {"type": "stat", "dataset_id": "q1", "value_col": "current", "label": "R"}]}))),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    _stub_catalog(monkeypatch)
    _stub_one_dataset(monkeypatch)

    dashboard_mod.generate_dashboard(
        "revenue", ctx=ctx, memory_suggestions=["Always call it MRR"])

    author_system = ctx.openai.chat.completions.kwargs[-1]["messages"][0]["content"]
    assert "Always call it MRR" in author_system


def test_the_author_uses_the_author_model(monkeypatch):
    """The author may run on a stronger model; the query loop must not."""
    scripted = [
        _response(_message(content=_PLAN_JSON)),
        *_gathering(),
        _insight(),
        _response(_message(content=json.dumps({"blocks": [
            {"type": "stat", "dataset_id": "q1", "value_col": "current", "label": "R"}]}))),
    ]
    ctx = make_ctx(
        openai=FakeOpenAI(scripted),
        settings=make_settings(OPENAI_AUTHOR_MODEL="stronger-model"),
    )
    _stub_catalog(monkeypatch)
    _stub_one_dataset(monkeypatch)

    dashboard_mod.generate_dashboard("revenue", ctx=ctx)
    models = ctx.openai.chat.completions.models_used
    assert models[:3] == ["test-model"] * 3  # planner + analyst stay cheap
    # Insight + author are the same quality trade, so both use the author model.
    assert models[3:] == ["stronger-model"] * 2


def test_an_empty_dashboard_still_carries_its_queries(monkeypatch):
    """Provenance survives an empty result.

    When a dashboard comes back with nothing on it, the queries that actually
    ran are the whole diagnosis -- dropping them leaves the user with an error
    and no way to see what was asked of the warehouse.
    """
    scripted = [
        _response(_message(content=_PLAN_JSON)),
        *_gathering(),
        _insight(),
        _response(_message(content="not json")),
        _response(_message(content="still not json")),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    _stub_catalog(monkeypatch)
    _stub_one_dataset(monkeypatch)

    result = dashboard_mod.generate_dashboard("revenue", ctx=ctx)

    assert count_data_blocks(result.document) == 0
    assert [q["dataset_id"] for q in result.queries] == ["q1"]
    assert result.steps > 0


def test_the_retry_prompts_contain_no_unrendered_braces(monkeypatch):
    """The empty-reply prompt is sent verbatim, so its braces must be literal.

    ``DASHBOARD_EMPTY_TEMPLATE`` never goes through ``.format()``. Escaped
    braces would reach the model as `{{"blocks": ...}}` -- showing it malformed
    JSON in the very message asking for valid JSON.
    """
    scripted = [
        _response(_message(content=_PLAN_JSON)),
        *_gathering(),
        _insight(),
        _response(_message(content="not json")),
        _response(_message(content=json.dumps({"blocks": [
            {"type": "stat", "dataset_id": "q1", "value_col": "current", "label": "R"}]}))),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    _stub_catalog(monkeypatch)
    _stub_one_dataset(monkeypatch)

    dashboard_mod.generate_dashboard("revenue", ctx=ctx)

    followup = ctx.openai.chat.completions.kwargs[-1]["messages"][-1]["content"]
    assert '{"blocks": [...]}' in followup
    assert "{{" not in followup and "}}" not in followup


# --- the insight pass ---------------------------------------------------------


def test_insight_findings_and_analyst_notes_reach_the_author(monkeypatch):
    """The insight pass exists to steer the author, so its output must arrive."""
    scripted = [
        _response(_message(content=_PLAN_JSON)),
        _response(_message(tool_calls=[_fn_call("c1", "SELECT metric, current FROM t")])),
        _response(_message(content="q1 serves the KPI. q2 was recon; ignore it.")),
        _insight(
            insights=[{"dataset_id": "q1", "kind": "trend",
                       "finding": "Revenue fell for three straight months",
                       "importance": 3, "presentation_hint": "lead with the line chart"}],
            lead=["q1"], drop=["q2"],
        ),
        _response(_message(content=json.dumps({"blocks": [
            {"type": "stat", "dataset_id": "q1", "value_col": "current", "label": "R"}]}))),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    _stub_catalog(monkeypatch)
    _stub_one_dataset(monkeypatch)

    events = []
    dashboard_mod.generate_dashboard(
        "revenue", ctx=ctx, on_event=lambda k, d: events.append((k, d)))

    author_user = ctx.openai.chat.completions.kwargs[-1]["messages"][1]["content"]
    assert "Revenue fell for three straight months" in author_user
    assert "Lead with: q1" in author_user
    assert "Do not show: q2" in author_user
    assert "q2 was recon; ignore it." in author_user  # the Analyst's manifest
    assert any(k == "insights" for k, _ in events)


def test_the_insight_pass_sees_more_rows_than_the_author(monkeypatch):
    """Insight reviews the full data; the author still gets cheap previews."""
    rows = [[f"cat{i}", i] for i in range(60)]
    scripted = [
        _response(_message(content=_PLAN_JSON)),
        *_gathering(),
        _insight(),
        _response(_message(content=json.dumps({"blocks": [
            {"type": "stat", "dataset_id": "q1", "value_col": "current", "label": "R"}]}))),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    _stub_catalog(monkeypatch)
    _stub_one_dataset(monkeypatch, rows=rows)

    dashboard_mod.generate_dashboard("revenue", ctx=ctx)

    insight_user = ctx.openai.chat.completions.kwargs[3]["messages"][1]["content"]
    author_user = ctx.openai.chat.completions.kwargs[4]["messages"][1]["content"]
    assert "cat42" in insight_user      # far past the preview cap
    assert "cat42" not in author_user   # previews stay at 5 rows
    assert "cat4" in author_user


def test_an_unparsable_insight_reply_degrades_to_prose(monkeypatch):
    """A prose insight beats none, and the dashboard must still materialize."""
    scripted = [
        _response(_message(content=_PLAN_JSON)),
        *_gathering(),
        _response(_message(content="The trend is clearly downward.")),  # not JSON
        _response(_message(content=json.dumps({"blocks": [
            {"type": "stat", "dataset_id": "q1", "value_col": "current", "label": "R"}]}))),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    _stub_catalog(monkeypatch)
    _stub_one_dataset(monkeypatch)

    result = dashboard_mod.generate_dashboard("revenue", ctx=ctx)

    assert count_data_blocks(result.document) == 1
    author_user = ctx.openai.chat.completions.kwargs[-1]["messages"][1]["content"]
    assert "The trend is clearly downward." in author_user


def test_a_crashing_insight_pass_does_not_kill_the_dashboard(monkeypatch):
    """Insight is best-effort: an exception there must cost only the insights."""
    good = json.dumps({"blocks": [
        {"type": "stat", "dataset_id": "q1", "value_col": "current", "label": "R"}]})
    scripted = [
        _response(_message(content=_PLAN_JSON)),
        *_gathering(),
        RuntimeError("insight endpoint down"),
        _response(_message(content=good)),
    ]

    class ExplodingCompletions:
        def __init__(self, scripted):
            self._scripted = list(scripted)
            self.calls = 0
            self.kwargs = []

        def create(self, **kwargs):
            self.kwargs.append(kwargs)
            item = self._scripted[self.calls]
            self.calls += 1
            if isinstance(item, Exception):
                raise item
            return item

    fake = FakeOpenAI()
    fake.chat.completions = ExplodingCompletions(scripted)
    ctx = make_ctx(openai=fake)
    _stub_catalog(monkeypatch)
    _stub_one_dataset(monkeypatch)

    events = []
    result = dashboard_mod.generate_dashboard(
        "revenue", ctx=ctx, on_event=lambda k, d: events.append((k, d)))

    assert count_data_blocks(result.document) == 1
    assert any("Insight pass failed" in d.get("message", "")
               for k, d in events if k == "error")


def test_author_max_tokens_is_passed_when_configured(monkeypatch):
    scripted = [
        _response(_message(content=_PLAN_JSON)),
        *_gathering(),
        _insight(),
        _response(_message(content=json.dumps({"blocks": [
            {"type": "stat", "dataset_id": "q1", "value_col": "current", "label": "R"}]}))),
    ]
    ctx = make_ctx(
        openai=FakeOpenAI(scripted),
        settings=make_settings(OPENAI_AUTHOR_MAX_TOKENS=5000),
    )
    _stub_catalog(monkeypatch)
    _stub_one_dataset(monkeypatch)

    dashboard_mod.generate_dashboard("revenue", ctx=ctx)
    kwargs = ctx.openai.chat.completions.kwargs
    assert kwargs[3]["max_tokens"] == 5000  # insight
    assert kwargs[4]["max_tokens"] == 5000  # author
    assert all("max_tokens" not in k for k in kwargs[:3])  # loop calls untouched


def test_author_max_tokens_is_omitted_by_default(monkeypatch):
    scripted = [
        _response(_message(content=_PLAN_JSON)),
        *_gathering(),
        _insight(),
        _response(_message(content=json.dumps({"blocks": [
            {"type": "stat", "dataset_id": "q1", "value_col": "current", "label": "R"}]}))),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    _stub_catalog(monkeypatch)
    _stub_one_dataset(monkeypatch)

    dashboard_mod.generate_dashboard("revenue", ctx=ctx)
    assert all("max_tokens" not in k for k in ctx.openai.chat.completions.kwargs)


def test_the_insight_findings_land_on_the_result_for_persistence(monkeypatch):
    """The raw findings shape the grid; losing them at save time loses the
    dashboard's own explanation of itself."""
    findings = [{"dataset_id": "q1", "kind": "trend",
                 "finding": "Revenue fell for three straight months",
                 "importance": 3}]
    scripted = [
        _response(_message(content=_PLAN_JSON)),
        _response(_message(tool_calls=[_fn_call("c1", "SELECT metric, current FROM t")])),
        _response(_message(content="done")),
        _insight(insights=findings, lead=["q1"], gaps=["No cost data"]),
        _response(_message(content=json.dumps({"blocks": [
            {"type": "stat", "dataset_id": "q1", "value_col": "current", "label": "R"}]}))),
    ]
    ctx = make_ctx(openai=FakeOpenAI(scripted))
    _stub_catalog(monkeypatch)
    _stub_one_dataset(monkeypatch)

    result = dashboard_mod.generate_dashboard("revenue", ctx=ctx)

    assert result.insights["insights"] == findings
    assert result.insights["lead"] == ["q1"]
    assert result.insights["gaps"] == ["No cost data"]
