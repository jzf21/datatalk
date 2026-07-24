"""Memory store tests with a deterministic fake embedder (no API, temp DB)."""

import sqlite3

import numpy as np

import datatalk.memory.store as store_mod
from datatalk.agent.blocks import Document, Heading, Paragraph, Table
from datatalk.memory.store import MemoryStore


def _fake_embed_factory():
    """Map keywords to distinct directions so cosine similarity is predictable."""
    vocab = ["sla", "revenue", "bug", "account", "sprint"]

    def fake_embed(texts):
        out = []
        for t in texts:
            v = np.zeros(len(vocab), dtype=np.float32)
            low = t.lower()
            for i, w in enumerate(vocab):
                if w in low:
                    v[i] += 1.0
            if not v.any():
                v[0] = 0.01  # avoid zero vector
            out.append(v.tolist())
        return out

    return fake_embed


def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "embed", _fake_embed_factory())
    return MemoryStore(path=str(tmp_path / "mem.sqlite3"))


def test_add_and_list_suggestions(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    s.add_suggestion("For SLA metrics use the jira.sla table")
    s.add_suggestion("Revenue lives in billing.invoices")
    assert len(s.all_suggestions()) == 2


def test_retrieval_ranks_relevant_first(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    s.add_suggestion("For SLA metrics use the jira.sla table")
    s.add_suggestion("Revenue lives in billing.invoices")
    s.add_suggestion("Bug counts come from jira.bugs")

    top = s.retrieve_suggestion_texts("report on SLA breaches", k=1)
    assert top == ["For SLA metrics use the jira.sla table"]


def test_delete_suggestion(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    added = s.add_suggestion("account grouping should use account_id")
    s.delete_suggestion(added.id)
    assert s.all_suggestions() == []


def _sample_doc():
    from datatalk.agent.executor import QueryResult

    ds = QueryResult(
        columns=["month", "issues"],
        rows=[["2026-01", 42]],
        row_count=1,
        truncated=False,
        sql="SELECT ...",
    )
    from datatalk.agent.blocks import materialize

    return materialize(
        Document(
            blocks=[
                Heading(text="Report", level=1),
                Paragraph(text="Summary."),
                Table(dataset_id="q1", columns=["month", "issues"]),
            ]
        ),
        {"q1": ds},
    )


def test_save_and_get_report(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    doc = _sample_doc()
    queries = [{"dataset_id": "q1", "sql": "SELECT ...", "row_count": 1, "columns": ["month", "issues"]}]
    saved = s.save_report("count issues", doc, queries)

    fetched = s.get_report(saved.id)
    assert fetched is not None
    # Document + queries round-trip.
    assert fetched.document.to_dict() == doc.to_dict()
    assert fetched.queries == queries
    # markdown is derived from the document (Analyze/memory keep working).
    assert "# Report" in fetched.markdown
    assert "2026-01 | 42" in fetched.markdown
    assert s.get_report(9999) is None


def test_qa_turns_crud(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    saved = s.save_report("a report", _sample_doc(), [])
    assert s.list_qa_turns(saved.id) == []

    answer = Document(blocks=[Paragraph(text="Because of X.")])
    q = [{"dataset_id": "q1", "sql": "SELECT 1", "row_count": 1, "columns": ["x"]}]
    turn = s.add_qa_turn(saved.id, "Why?", answer, q)

    turns = s.list_qa_turns(saved.id)
    assert len(turns) == 1
    assert turns[0].id == turn.id
    assert turns[0].question == "Why?"
    assert turns[0].answer_document.to_dict() == answer.to_dict()
    assert turns[0].queries == q


def test_migration_adds_columns_without_data_loss(tmp_path, monkeypatch):
    """An old DB with a reports table lacking document/queries migrates cleanly."""
    db_path = str(tmp_path / "old.sqlite3")
    old = sqlite3.connect(db_path)
    old.executescript(
        """
        CREATE TABLE reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request TEXT NOT NULL,
            markdown TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO reports (request, markdown, created_at)
        VALUES ('legacy', '# Old report', '2026-01-01T00:00:00+00:00');
        """
    )
    old.commit()
    old.close()

    monkeypatch.setattr(store_mod, "embed", _fake_embed_factory())
    s = MemoryStore(path=db_path)

    # Old row survives; new columns exist and default to empty/None.
    fetched = s.get_report(1)
    assert fetched is not None
    assert fetched.request == "legacy"
    assert fetched.markdown == "# Old report"
    assert fetched.document.blocks == []
    assert fetched.queries == []

    # And new saves work on the migrated DB.
    saved = s.save_report("new one", _sample_doc(), [])
    assert s.get_report(saved.id) is not None
