"""The legacy SQLite importer.

Built against a synthetic legacy file rather than the real one, because the real
``datatalk.sqlite3`` has **0 suggestions** -- so the embedding path, the only
part of the import that does a non-trivial conversion, would get no coverage at
all from it.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from datetime import timezone

import numpy as np
import pytest
from sqlalchemy import select

from datatalk.auth import passwords
from datatalk.db import models
from datatalk.scripts.import_sqlite import _ts, open_legacy, run_import

LEGACY_SCHEMA = """
CREATE TABLE suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    embedding BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request TEXT NOT NULL,
    markdown TEXT NOT NULL,
    created_at TEXT NOT NULL,
    document TEXT,
    queries TEXT
);
CREATE TABLE qa_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    question TEXT NOT NULL,
    answer_document TEXT NOT NULL,
    queries TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE dashboards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request TEXT NOT NULL,
    title TEXT NOT NULL,
    document TEXT NOT NULL,
    queries TEXT NOT NULL,
    analysis TEXT,
    created_at TEXT NOT NULL
);
"""

VECTOR = [0.5, -0.25, 0.125, 1.0, 0.0]
BLOB = struct.pack(f"<{len(VECTOR)}f", *VECTOR)
TS = "2026-07-24T08:52:34.159654+00:00"
NAIVE_TS = "2026-07-24T08:52:34.159654"


@pytest.fixture(autouse=True)
def _fast_hashing():
    passwords.use_fast_params_for_tests()


@pytest.fixture
def legacy_path(tmp_path):
    """A legacy file shaped like the real one, including its known defects.

    Notably: report 1 predates the document/queries migration and has NULLs
    there, and qa_turn 99 points at a report that does not exist -- the old
    schema had no foreign key, so that was always possible.
    """
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO suggestions (id, text, embedding, created_at) VALUES (?,?,?,?)",
        (1, "prefer sla breaches by team", BLOB, TS),
    )
    conn.execute(
        "INSERT INTO reports (id, request, markdown, created_at, document, queries)"
        " VALUES (?,?,?,?,?,?)",
        (1, "old report", "# old", NAIVE_TS, None, None),
    )
    conn.execute(
        "INSERT INTO reports (id, request, markdown, created_at, document, queries)"
        " VALUES (?,?,?,?,?,?)",
        (
            7,  # non-contiguous on purpose: remapping must not assume 1..n
            "new report",
            "# new",
            TS,
            json.dumps({"blocks": [{"type": "heading", "text": "Hi", "level": 1}]}),
            json.dumps([{"dataset_id": "q1", "sql": "SELECT 1"}]),
        ),
    )
    conn.executemany(
        "INSERT INTO qa_turns (id, report_id, question, answer_document, queries,"
        " created_at) VALUES (?,?,?,?,?,?)",
        [
            (1, 7, "why?", json.dumps({"blocks": []}), json.dumps([]), TS),
            (99, 4242, "orphan", json.dumps({"blocks": []}), json.dumps([]), TS),
        ],
    )
    conn.execute(
        "INSERT INTO dashboards (id, request, title, document, queries, analysis,"
        " created_at) VALUES (?,?,?,?,?,?,?)",
        (1, "d", "Usage", json.dumps({"blocks": []}), json.dumps([]), "## Up", TS),
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def legacy(legacy_path):
    conn = open_legacy(legacy_path)
    yield conn
    conn.close()


def _import(db, legacy, **kw):
    return run_import(
        db,
        legacy,
        org_name=kw.pop("org_name", "Acme"),
        org_slug=kw.pop("org_slug", "acme"),
        owner_email=kw.pop("owner_email", "owner@example.com"),
        owner_password=kw.pop("owner_password", "correct-horse-battery"),
        **kw,
    )


# --- the happy path ---


def test_import_creates_org_owner_and_content(db, legacy):
    summary = _import(db, legacy)

    assert summary.created_org and summary.created_user
    assert summary.tables["reports"].imported == 2
    assert summary.tables["dashboards"].imported == 1
    assert summary.tables["suggestions"].imported == 1

    org = db.execute(select(models.Org).where(models.Org.slug == "acme")).scalar_one()
    user = db.execute(
        select(models.User).where(models.User.email == "owner@example.com")
    ).scalar_one()
    membership = db.execute(
        select(models.Membership).where(models.Membership.org_id == org.id)
    ).scalar_one()
    assert membership.role == "owner"
    assert passwords.verify_password(user.password_hash, "correct-horse-battery")

    # Everything imported is scoped to that org and attributed to that user.
    for model in (models.Report, models.Dashboard, models.Suggestion, models.QATurn):
        rows = db.execute(select(model)).scalars().all()
        assert rows
        assert all(r.org_id == org.id for r in rows)
        assert all(r.created_by_user_id == user.id for r in rows)


def test_report_ids_are_remapped_and_qa_turns_follow(db, legacy):
    """The old ids are not preserved, so the turn must point at the new id."""
    _import(db, legacy)

    report = db.execute(
        select(models.Report).where(models.Report.legacy_id == 7)
    ).scalar_one()
    turn = db.execute(select(models.QATurn)).scalar_one()

    assert report.id != 7, "ids are reassigned by the sequence, not carried over"
    assert turn.report_id == report.id
    assert turn.legacy_id == 1


def test_orphaned_qa_turns_are_counted_and_skipped(db, legacy):
    """The old schema had no FK; the new composite FK would reject these."""
    summary = _import(db, legacy)

    assert summary.tables["qa_turns"].orphaned == 1
    assert summary.tables["qa_turns"].imported == 1
    assert len(db.execute(select(models.QATurn)).scalars().all()) == 1


def test_null_document_and_queries_become_empty_containers(db, legacy):
    """Those columns arrived in a later migration; early rows have NULL."""
    _import(db, legacy)
    old = db.execute(
        select(models.Report).where(models.Report.legacy_id == 1)
    ).scalar_one()
    assert old.document == {}
    assert old.queries == []


def test_naive_timestamps_are_read_as_utc(db, legacy):
    _import(db, legacy)
    old = db.execute(
        select(models.Report).where(models.Report.legacy_id == 1)
    ).scalar_one()
    assert old.created_at.tzinfo is not None
    assert old.created_at.astimezone(timezone.utc).hour == 8


# --- embeddings: the path the real file cannot cover ---


def test_embedding_blob_survives_the_round_trip(db, legacy):
    _import(db, legacy)
    row = db.execute(select(models.Suggestion)).scalar_one()

    assert row.embedding_dim == len(VECTOR)
    assert row.embedding_model  # recorded, so mixed-model vectors are filterable
    restored = np.frombuffer(row.embedding, dtype=np.float32)
    assert restored.tolist() == pytest.approx(VECTOR)


# --- idempotency ---


def test_second_run_imports_nothing_new(db, legacy):
    first = _import(db, legacy)
    second = _import(db, legacy)

    assert second.created_org is False and second.created_user is False
    for name in ("suggestions", "reports", "qa_turns", "dashboards"):
        assert second.tables[name].imported == 0, f"{name} was imported twice"
        assert second.tables[name].skipped == first.tables[name].imported

    assert len(db.execute(select(models.Report)).scalars().all()) == 2


def test_second_run_still_remaps_qa_turns(db, legacy_path):
    """Regression guard: the id map must be seeded from already-imported rows.

    If it were built only from rows inserted *this* run, a re-import would find
    no reports and declare every turn an orphan.
    """
    first_conn = open_legacy(legacy_path)
    try:
        _import(db, first_conn)
    finally:
        first_conn.close()

    second_conn = open_legacy(legacy_path)
    try:
        summary = _import(db, second_conn)
    finally:
        second_conn.close()

    assert summary.tables["qa_turns"].skipped == 1
    assert summary.tables["qa_turns"].orphaned == 1


def test_import_is_scoped_to_its_own_org(db, legacy):
    """Two imports of the same file into two orgs stay separate."""
    _import(db, legacy)
    _import(db, legacy, org_slug="globex", org_name="Globex",
            owner_email="other@example.com")

    orgs = db.execute(select(models.Org)).scalars().all()
    assert len(orgs) == 2
    for org in orgs:
        reports = db.execute(
            select(models.Report).where(models.Report.org_id == org.id)
        ).scalars().all()
        assert len(reports) == 2, "each org gets its own copy, with its own ids"

    # Same legacy_id in both orgs -- the unique index is per-org, not global.
    assert len(db.execute(select(models.Report)).scalars().all()) == 4


# --- guards ---


def test_existing_user_does_not_need_a_password(db, legacy):
    _import(db, legacy)
    summary = _import(db, legacy, org_slug="globex", org_name="Globex",
                      owner_password=None)
    assert summary.created_user is False


def test_missing_user_without_a_password_is_refused(db, legacy):
    with pytest.raises(SystemExit, match="owner-password"):
        _import(db, legacy, owner_password=None)


def test_source_file_is_opened_read_only(legacy_path):
    conn = open_legacy(legacy_path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM reports")
    conn.close()


def test_unparseable_timestamp_does_not_abort_the_import():
    """One bad audit field must not cost the whole row."""
    assert _ts("not a date").tzinfo is not None
    assert _ts(None).tzinfo is not None
