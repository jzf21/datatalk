"""Dashboard web endpoints (TestClient, monkeypatched agents, temp DB)."""

import json

import numpy as np
from fastapi.testclient import TestClient

import datatalk.memory.store as store_mod
import datatalk.web.app as web
from datatalk.agent.blocks import Document, Row, Stat, materialize
from datatalk.agent.dashboard import DashboardResult
from datatalk.agent.executor import QueryResult


def _fake_embed(texts):
    return [np.array([0.01], dtype=np.float32).tolist() for _ in texts]


def _doc():
    ds = QueryResult(columns=["metric", "current"], rows=[["rev", 120]],
                     row_count=1, truncated=False, sql="x")
    return materialize(
        Document(blocks=[Row(children=[Stat(dataset_id="q1", value_col="current", label="Revenue")])]),
        {"q1": ds},
    )


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "embed", _fake_embed)
    db = str(tmp_path / "mem.sqlite3")
    monkeypatch.setattr(web, "_store", lambda: store_mod.MemoryStore(path=db))
    return TestClient(web.app)


def test_dashboard_generate_saves_and_lists(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
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


def test_dashboard_generate_empty_request_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/dashboard", json={"request": "  "}).status_code == 400


def test_dashboard_analyze_persists(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    saved = store_mod.MemoryStore(path=web._store().path).save_dashboard("d", _doc(), [])

    monkeypatch.setattr(web, "analyze_dashboard", lambda document, **kw: "## Up")
    r = client.post(f"/api/dashboards/{saved.id}/analyze", json={"focus": None})
    assert r.status_code == 200 and r.json()["analysis"] == "## Up"
    # persisted
    assert client.get(f"/api/dashboards/{saved.id}").json()["analysis"] == "## Up"


def test_dashboard_analyze_missing_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "analyze_dashboard", lambda document, **kw: "x")
    assert client.post("/api/dashboards/9999/analyze", json={}).status_code == 404
