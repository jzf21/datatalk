"""Dashboard persistence tests (fake embedder, temp DB — no API)."""

import numpy as np

import datatalk.memory.store as store_mod
from datatalk.agent.blocks import Document, Heading, Row, Stat, Table, materialize
from datatalk.agent.executor import QueryResult
from datatalk.memory.store import MemoryStore


def _fake_embed(texts, ctx=None):
    return [np.array([0.01], dtype=np.float32).tolist() for _ in texts]


def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "embed", _fake_embed)
    return MemoryStore(path=str(tmp_path / "mem.sqlite3"))


def _dashboard_doc():
    ds = QueryResult(columns=["metric", "current"], rows=[["rev", 120]],
                     row_count=1, truncated=False, sql="x")
    return materialize(
        Document(blocks=[
            Heading(text="KPIs", level=1),
            Row(children=[Stat(dataset_id="q1", value_col="current", label="Revenue")]),
            Table(dataset_id="q1", columns=["metric", "current"]),
        ]),
        {"q1": ds},
    )


def test_save_and_get_dashboard_round_trip(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    doc = _dashboard_doc()
    queries = [{"dataset_id": "q1", "sql": "x", "row_count": 1, "columns": ["metric", "current"]}]
    saved = s.save_dashboard("revenue dashboard", doc, queries)

    fetched = s.get_dashboard(saved.id)
    assert fetched is not None
    assert fetched.document.to_dict() == doc.to_dict()
    assert fetched.queries == queries
    assert fetched.title == "revenue dashboard"
    assert fetched.analysis is None
    assert s.get_dashboard(9999) is None


def test_title_derived_from_request_when_omitted(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    saved = s.save_dashboard("  " + "x" * 200, _dashboard_doc(), [])
    assert 0 < len(saved.title) <= 80


def test_set_dashboard_analysis_persists(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    saved = s.save_dashboard("d", _dashboard_doc(), [])
    s.set_dashboard_analysis(saved.id, "## Findings\nUp.")
    assert s.get_dashboard(saved.id).analysis == "## Findings\nUp."


def test_list_dashboards_newest_first(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    a = s.save_dashboard("first", _dashboard_doc(), [])
    b = s.save_dashboard("second", _dashboard_doc(), [])
    ids = [d.id for d in s.list_dashboards()]
    assert ids[0] == b.id and ids[1] == a.id


def test_create_table_idempotent_across_stores(tmp_path, monkeypatch):
    path = str(tmp_path / "mem.sqlite3")
    monkeypatch.setattr(store_mod, "embed", _fake_embed)
    s1 = MemoryStore(path=path)
    s1.save_dashboard("d", _dashboard_doc(), [])
    s1.close()
    # A second store on the same file re-runs CREATE TABLE IF NOT EXISTS cleanly.
    s2 = MemoryStore(path=path)
    assert len(s2.list_dashboards()) == 1
