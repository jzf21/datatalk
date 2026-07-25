"""Cross-tenant isolation: org B must never see or mutate org A's data.

Every test here writes through one org's store and asserts the other org's
store cannot reach it. Runs against real Postgres, because several of these
guarantees are constraints rather than application logic.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from datatalk.agent.blocks import Document, Heading
from datatalk.memory.store import CrossOrgError


def _doc(title: str = "Report") -> Document:
    return Document(blocks=[Heading(level=1, text=title)])


# --- reports ---


def test_other_org_cannot_read_a_report(store, other_store):
    saved = store.save_report("secret request", _doc("Secret"))

    assert store.get_report(saved.id) is not None
    assert other_store.get_report(saved.id) is None, "cross-org read"


def test_report_lists_are_per_org(store, other_store):
    store.save_report("mine", _doc())

    assert len(store.list_reports()) == 1
    assert other_store.list_reports() == []


# --- dashboards ---


def test_other_org_cannot_read_a_dashboard(store, other_store):
    saved = store.save_dashboard("secret dash", _doc())

    assert store.get_dashboard(saved.id) is not None
    assert other_store.get_dashboard(saved.id) is None


def test_other_org_cannot_set_dashboard_analysis(store, other_store):
    saved = store.save_dashboard("d", _doc())

    # False, not a silent success: the endpoint turns this into a 404.
    assert other_store.set_dashboard_analysis(saved.id, "injected") is False
    assert store.get_dashboard(saved.id).analysis is None

    assert store.set_dashboard_analysis(saved.id, "legit") is True
    assert store.get_dashboard(saved.id).analysis == "legit"


# --- suggestions / memory ---


def test_other_org_cannot_delete_a_suggestion(store, other_store):
    suggestion = store.add_suggestion("Use the sla table for SLA metrics")

    assert other_store.delete_suggestion(suggestion.id) is False
    assert [s.id for s in store.all_suggestions()] == [suggestion.id]

    assert store.delete_suggestion(suggestion.id) is True
    assert store.all_suggestions() == []


def test_memory_retrieval_is_per_org(store, other_store):
    """The core promise: each org gets its own memory system."""
    store.add_suggestion("For SLA metrics always use jira.sla")

    assert store.retrieve_suggestion_texts("sla breaches", k=5) == [
        "For SLA metrics always use jira.sla"
    ]
    # A perfect keyword match in org A must still be invisible to org B.
    assert other_store.retrieve_suggestion_texts("sla breaches", k=5) == []


def test_suggestions_record_their_author(store, user_a):
    suggestion = store.add_suggestion("attributed hint")
    assert suggestion.created_by_user_id == user_a.id


def test_retrieval_ignores_vectors_from_another_embedding_model(store, db):
    """Mixing dimensions makes np.dot raise, which silently kills all memory
    because the web layer swallows retrieval errors."""
    store.add_suggestion("revenue guidance")
    db.execute(
        text(
            "UPDATE suggestions SET embedding_model = 'some-other-model' "
            "WHERE org_id = :org"
        ),
        {"org": store.org_id},
    )

    assert store.retrieve_suggestion_texts("revenue", k=5) == []


# --- Q&A turns ---


def test_qa_turn_cannot_attach_to_another_orgs_report(store, other_store):
    saved = store.save_report("mine", _doc())

    with pytest.raises(CrossOrgError):
        other_store.add_qa_turn(saved.id, "leak?", _doc("answer"))


def test_qa_turns_are_listed_per_org(store, other_store):
    saved = store.save_report("mine", _doc())
    store.add_qa_turn(saved.id, "how many?", _doc("42"))

    assert len(store.list_qa_turns(saved.id)) == 1
    assert other_store.list_qa_turns(saved.id) == []


def test_database_rejects_a_mismatched_qa_turn(db, store, other_store):
    """Proves fk_qa_report is live: even raw SQL bypassing the store cannot
    attach a Q&A turn across orgs."""
    saved = store.save_report("mine", _doc())
    db.flush()

    with pytest.raises(IntegrityError, match="fk_qa_report"):
        db.execute(
            text(
                "INSERT INTO qa_turns (org_id, report_id, question) "
                "VALUES (:org, :report, 'stolen')"
            ),
            {"org": other_store.org_id, "report": saved.id},
        )
        db.flush()


# --- cascade behavior ---


def test_deleting_an_org_removes_only_its_own_content(db, store, other_store, org_a):
    report = store.save_report("a", _doc())
    store.add_qa_turn(report.id, "q", _doc("a"))
    store.save_dashboard("a", _doc())
    store.add_suggestion("a hint")

    other_report = other_store.save_report("b", _doc())
    other_store.save_dashboard("b", _doc())
    db.flush()

    db.execute(text("DELETE FROM orgs WHERE id = :org"), {"org": org_a.id})
    db.flush()

    for table in ("reports", "dashboards", "suggestions", "qa_turns"):
        remaining = db.execute(
            text(f"SELECT count(*) FROM {table} WHERE org_id = :org"),
            {"org": org_a.id},
        ).scalar_one()
        assert remaining == 0, f"{table} rows survived the org delete"

    # The other org is untouched.
    assert other_store.get_report(other_report.id) is not None
    assert len(other_store.list_dashboards()) == 1


def test_deleting_a_user_keeps_the_orgs_reports(db, store, user_a):
    report = store.save_report("survives", _doc())
    db.flush()

    db.execute(text("DELETE FROM users WHERE id = :user"), {"user": user_a.id})
    db.flush()
    db.expire_all()

    still_there = store.get_report(report.id)
    assert still_there is not None, "deleting an author must not delete org content"
    assert still_there.created_by_user_id is None
