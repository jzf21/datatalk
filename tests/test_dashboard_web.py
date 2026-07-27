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
