"""Dashboard web endpoints (TestClient, monkeypatched agents, real Postgres).

The endpoints are authenticated now, so these go through a signed-up client
rather than monkeypatching a store factory.
"""

import json

import pytest

import datatalk.web.app as web
from datatalk.agent.blocks import Document, Row, Stat, materialize
from datatalk.agent.dashboard import DashboardResult
from datatalk.agent.executor import QueryResult


def _doc():
    ds = QueryResult(columns=["metric", "current"], rows=[["rev", 120]],
                     row_count=1, truncated=False, sql="x")
    return materialize(
        Document(blocks=[Row(children=[Stat(dataset_id="q1", value_col="current", label="Revenue")])]),
        {"q1": ds},
    )


@pytest.fixture
def client(auth_client):
    """Authenticated client; the dashboard endpoints all require a session."""
    return auth_client


def test_dashboard_generate_saves_and_lists(client, monkeypatch):
    doc = _doc()

    def fake_generate(request, **kwargs):
        on_event = kwargs.get("on_event")
        if on_event:
            on_event("dashboard", {"document": doc.to_dict()})
        return DashboardResult(request=request, document=doc,
                               queries=[{"dataset_id": "q1"}], steps=1)

    monkeypatch.setattr(web, "generate_dashboard", fake_generate)

    with client.stream("POST", "/api/dashboard", json={"request": "rev dash"}) as resp:
        assert resp.status_code == 200
        events = [json.loads(ln) for ln in resp.iter_lines() if ln]
    kinds = [e["kind"] for e in events]
    assert "dashboard" in kinds and "saved" in kinds and "done" in kinds
    saved = next(e for e in events if e["kind"] == "saved")["data"]
    dash_id = saved["dashboard_id"]

    listed = client.get("/api/dashboards").json()["dashboards"]
    assert listed and listed[0]["id"] == dash_id

    full = client.get(f"/api/dashboards/{dash_id}").json()
    assert full["document"] == doc.to_dict()
    assert full["analysis"] is None


def test_dashboard_generate_empty_request_400(client, monkeypatch):
    assert client.post("/api/dashboard", json={"request": "  "}).status_code == 400


def test_dashboard_analyze_persists(client, monkeypatch):
    doc = _doc()
    monkeypatch.setattr(
        web,
        "generate_dashboard",
        lambda request, **kw: DashboardResult(
            request=request, document=doc, queries=[], steps=1
        ),
    )
    with client.stream("POST", "/api/dashboard", json={"request": "d"}) as resp:
        saved_id = next(
            json.loads(ln)["data"]["dashboard_id"]
            for ln in resp.iter_lines()
            if ln and json.loads(ln)["kind"] == "saved"
        )

    monkeypatch.setattr(web, "analyze_dashboard", lambda document, **kw: "## Up")
    r = client.post(f"/api/dashboards/{saved_id}/analyze", json={"focus": None})
    assert r.status_code == 200 and r.json()["analysis"] == "## Up"
    # persisted
    assert client.get(f"/api/dashboards/{saved_id}").json()["analysis"] == "## Up"


def test_dashboard_analyze_missing_404(client, monkeypatch):
    monkeypatch.setattr(web, "analyze_dashboard", lambda document, **kw: "x")
    assert client.post("/api/dashboards/9999/analyze", json={}).status_code == 404


def test_dashboard_insights_survive_to_the_detail_endpoint(client, monkeypatch):
    doc = _doc()
    insights = {"insights": [{"dataset_id": "q1", "finding": "up", "importance": 2}],
                "lead": ["q1"]}
    monkeypatch.setattr(
        web,
        "generate_dashboard",
        lambda request, **kw: DashboardResult(
            request=request, document=doc, queries=[], steps=1, insights=insights
        ),
    )
    with client.stream("POST", "/api/dashboard", json={"request": "d"}) as resp:
        saved_id = next(
            json.loads(ln)["data"]["dashboard_id"]
            for ln in resp.iter_lines()
            if ln and json.loads(ln)["kind"] == "saved"
        )

    assert client.get(f"/api/dashboards/{saved_id}").json()["insights"] == insights


# --- refresh -----------------------------------------------------------------


def _authoring():
    from datatalk.agent.blocks import Row, Stat

    return Document(blocks=[
        Row(children=[Stat(dataset_id="q1", value_col="current", label="Revenue")])
    ])


def _generate(monkeypatch, *, authoring=None, queries=None):
    """Save one dashboard through the real endpoint and return its id."""
    authoring = authoring if authoring is not None else _authoring()
    ds = QueryResult(columns=["metric", "current"], rows=[["rev", 120]],
                     row_count=1, truncated=False, sql="x")
    doc = materialize(authoring, {"q1": ds})
    if queries is None:
        queries = [{"dataset_id": "q1", "source": "default", "sql": "SELECT 1"}]
    monkeypatch.setattr(
        web,
        "generate_dashboard",
        lambda request, **kw: DashboardResult(
            request=request, document=doc, queries=queries, steps=1,
            authoring_document=authoring,
        ),
    )
    return doc


def _save(client, monkeypatch, **kw):
    _generate(monkeypatch, **kw)
    with client.stream("POST", "/api/dashboard", json={"request": "d"}) as resp:
        return next(
            json.loads(ln)["data"]["dashboard_id"]
            for ln in resp.iter_lines()
            if ln and json.loads(ln)["kind"] == "saved"
        )


def test_a_saved_dashboard_reports_itself_refreshable(client, monkeypatch):
    dash_id = _save(client, monkeypatch)
    detail = client.get(f"/api/dashboards/{dash_id}").json()
    assert detail["refreshable"] is True
    assert detail["filters"] == {}


def test_refresh_returns_current_numbers_not_the_snapshot(client, monkeypatch, ctx_warehouse):
    """The bug: the stored document says 120, the warehouse now says 999."""
    dash_id = _save(client, monkeypatch)
    assert client.get(f"/api/dashboards/{dash_id}").json()[
        "document"]["blocks"][0]["children"][0]["value"] == 120

    ctx_warehouse.set_rows(["metric", "current"], [["rev", 999]])
    r = client.post(f"/api/dashboards/{dash_id}/refresh", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["document"]["blocks"][0]["children"][0]["value"] == 999
    assert body["partial"] is False
    assert body["exact"] is True
    assert body["refreshed_at"]


def test_a_refresh_does_not_overwrite_the_saved_snapshot(client, monkeypatch, ctx_warehouse):
    """Refresh is a read. `analysis` is prose about the stored numbers, so
    silently replacing them would invalidate it without touching it."""
    dash_id = _save(client, monkeypatch)
    ctx_warehouse.set_rows(["metric", "current"], [["rev", 999]])
    client.post(f"/api/dashboards/{dash_id}/refresh", json={})

    still = client.get(f"/api/dashboards/{dash_id}").json()
    assert still["document"]["blocks"][0]["children"][0]["value"] == 120


def test_refresh_of_another_orgs_dashboard_404s_without_querying(
    client, db, monkeypatch, ctx_warehouse
):
    from tests.conftest import give_connection, signup

    dash_id = _save(client, monkeypatch)

    # Same TestClient, a second signed-up org: signing up replaces the session
    # cookie, so subsequent requests are org B asking for org A's dashboard id.
    other_org = signup(client, org_name="Other Org")
    give_connection(db, other_org)
    ctx_warehouse.queries.clear()

    r = client.post(f"/api/dashboards/{dash_id}/refresh", json={})
    assert r.status_code == 404
    assert r.json()["detail"] == "dashboard_not_found"
    # The half that matters: it 404s *before* reaching a warehouse, so a
    # cross-org id cannot be used to make another org's SQL run.
    assert ctx_warehouse.queries == []


def test_refresh_of_a_missing_dashboard_404s(client):
    r = client.post("/api/dashboards/999999/refresh", json={})
    assert r.status_code == 404 and r.json()["detail"] == "dashboard_not_found"


def test_a_nan_cell_does_not_break_the_refresh_body(client, monkeypatch, ctx_warehouse):
    """A refreshed document is never persisted, so it passes through neither the
    JSONB serializer nor ndjson() -- jsonsafe has to be applied at this boundary
    or a bare `NaN` token makes the browser reject the whole response."""
    dash_id = _save(client, monkeypatch)
    ctx_warehouse.set_rows(["metric", "current"], [["rev", float("nan")]])

    r = client.post(f"/api/dashboards/{dash_id}/refresh", json={})
    assert r.status_code == 200
    assert "NaN" not in r.text
    assert r.json()["document"]["blocks"][0]["children"][0].get("value") is None


def test_a_dead_source_degrades_the_block_and_reports_it(client, monkeypatch, ctx_warehouse):
    from datatalk.warehouse.base import WarehouseError

    dash_id = _save(client, monkeypatch)
    ctx_warehouse.fail = WarehouseError("connection refused")

    r = client.post(f"/api/dashboards/{dash_id}/refresh", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["partial"] is True
    assert body["datasets"][0]["ok"] is False
    assert body["datasets"][0]["reason"] == "query_failed"
    # The dashboard still renders: the block degraded to a note, nothing 500ed.
    assert body["document"]["blocks"][0]["children"][0]["type"] == "paragraph"


def test_a_second_concurrent_refresh_is_rejected(client, monkeypatch, ctx_warehouse):
    from datatalk.dashboards import refresh as refresh_svc

    dash_id = _save(client, monkeypatch)
    assert refresh_svc.try_acquire(client.org_id, dash_id) is True
    try:
        r = client.post(f"/api/dashboards/{dash_id}/refresh", json={})
        assert r.status_code == 409
        assert r.json()["detail"] == "refresh_in_progress"
    finally:
        refresh_svc.release(client.org_id, dash_id)


# --- filters -----------------------------------------------------------------


_TEMPLATE = (
    "SELECT metric, current FROM revenue\n"
    "WHERE ({{dt.p_range_all}} OR (ts >= {{dt.p_range_from}} "
    "AND ts < {{dt.p_range_to}}))\n"
    "LIMIT 1000"
)


def _script_rewrite(monkeypatch, sql=_TEMPLATE):
    """Stand in for the templatize LLM call, leaving verification real."""
    from datatalk.dashboards import configure as configure_svc
    from datatalk.agent.templatize import TemplateProposal

    monkeypatch.setattr(
        configure_svc,
        "propose_template",
        lambda query, defs, **kw: TemplateProposal(
            sql=sql,
            params=[
                {"name": "p_range_all", "type": "bool"},
                {"name": "p_range_from", "type": "date"},
                {"name": "p_range_to", "type": "date"},
            ],
            filters=["range"],
        ),
    )


def test_filters_are_configured_verified_and_then_applied(
    client, monkeypatch, ctx_warehouse
):
    dash_id = _save(client, monkeypatch)
    _script_rewrite(monkeypatch)

    r = client.put(
        f"/api/dashboards/{dash_id}/filters",
        json={"filters": [{"id": "range", "kind": "date_range", "label": "Period"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["wired"] == ["q1"]

    # The definition survives to the detail endpoint...
    detail = client.get(f"/api/dashboards/{dash_id}").json()
    assert [d["id"] for d in detail["filters"]["filters"]] == ["range"]

    # ...and a filtered refresh binds the value rather than inlining it.
    ctx_warehouse.queries.clear()
    ctx_warehouse.parameter_sets.clear()
    r = client.post(
        f"/api/dashboards/{dash_id}/refresh",
        json={"filters": {"range": {"from": "2026-01-01", "to": "2026-03-31"}}},
    )
    assert r.status_code == 200, r.text
    executed = ctx_warehouse.queries[-1]
    bound = ctx_warehouse.parameter_sets[-1]
    assert "2026-01-01" not in executed
    assert str(bound["p_range_from"]) == "2026-01-01"


def test_an_unknown_filter_id_is_a_400_before_any_query_runs(
    client, monkeypatch, ctx_warehouse
):
    dash_id = _save(client, monkeypatch)
    _script_rewrite(monkeypatch)
    client.put(
        f"/api/dashboards/{dash_id}/filters",
        json={"filters": [{"id": "range", "kind": "date_range", "label": "Period"}]},
    )

    ctx_warehouse.queries.clear()
    r = client.post(
        f"/api/dashboards/{dash_id}/refresh",
        json={"filters": {"ghost": {"all": True}}},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "filter_unknown"
    assert ctx_warehouse.queries == []


def test_a_malformed_date_is_a_400_before_any_query_runs(
    client, monkeypatch, ctx_warehouse
):
    dash_id = _save(client, monkeypatch)
    _script_rewrite(monkeypatch)
    client.put(
        f"/api/dashboards/{dash_id}/filters",
        json={"filters": [{"id": "range", "kind": "date_range", "label": "Period"}]},
    )

    ctx_warehouse.queries.clear()
    r = client.post(
        f"/api/dashboards/{dash_id}/refresh",
        json={"filters": {"range": {"from": "2026-01-01'; DROP TABLE t --"}}},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "filter_value_invalid"
    assert ctx_warehouse.queries == []


def test_configuring_filters_on_another_orgs_dashboard_404s(
    client, db, monkeypatch, ctx_warehouse
):
    from tests.conftest import give_connection, signup

    dash_id = _save(client, monkeypatch)
    other = signup(client, org_name="Other Org")
    give_connection(db, other)

    r = client.put(
        f"/api/dashboards/{dash_id}/filters",
        json={"filters": [{"id": "range", "kind": "date_range", "label": "Period"}]},
    )
    assert r.status_code == 404 and r.json()["detail"] == "dashboard_not_found"
