"""Jira as a synced source, end to end against a real sync store.

The sync store is a second database on the test server (``<test db>_sync``),
created on demand. Like the app DB, it is only reachable when
``DATATALK_TEST_DATABASE_URL`` is set; unlike it, what a test writes there is
real and is swept after every test -- schemas and roles do not roll back.

The test that matters most is the first one: a source's reader role cannot see
another source's rows. Every other isolation property in DataTalk is enforced
by our code; this one is enforced by the server, and this test is what proves
the grants actually say what the docstrings claim.
"""

from __future__ import annotations

import json
from functools import partial

import psycopg
import pytest
from sqlalchemy.engine import make_url

from datatalk.auth import orgs as orgs_svc
from datatalk.config import get_settings
from datatalk.db import models
from datatalk.integrations import syncstore
from datatalk.integrations.jira import service as jira_service
from datatalk.integrations.jira import sync as jira_sync
from datatalk.integrations.jira.client import JiraClient
from tests.conftest import give_connection, signup
from tests.jira_fake import FakeJira, make_issue, points_change, sprint, sprint_change

TOKEN = "jira-api-token-DO-NOT-LEAK"


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(scope="session")
def sync_store_url(database_url) -> str:
    u = make_url(database_url)
    name = f"{u.database}_sync"
    with psycopg.connect(
        host=u.host, port=u.port, user=u.username, password=u.password,
        dbname=u.database, autocommit=True,
    ) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            c.execute(f'CREATE DATABASE "{name}"')
    return u.set(database=name).render_as_string(hide_password=False)


@pytest.fixture
def store(sync_store_url, monkeypatch):
    """The sync store configured, and emptied again afterwards."""
    monkeypatch.setenv("DATATALK_SYNC_DATABASE_URL", sync_store_url)
    get_settings.cache_clear()
    yield sync_store_url
    with syncstore.admin_connection() as conn:
        for schema in syncstore.managed_schemas(conn):
            syncstore.drop(conn, schema, syncstore.role_for_schema(schema))
    get_settings.cache_clear()


@pytest.fixture
def fake_jira(monkeypatch):
    """Every JiraClient in the process talks to this fake, with no real sleeps."""
    fake = FakeJira(
        [
            make_issue(
                1, "ABC-1", created="2024-03-01 09:00", updated="2024-03-01 10:00",
                points=3, sprints=[{"id": 7, "name": "Sprint 7", "state": "active"}],
                transitions=[("2024-03-01 10:00", "1", "3", "alice")],
                worklogs=[(501, "2024-03-01 09:30", "alice", 1800)],
            ),
            make_issue(2, "ABC-2", updated="2024-03-02 10:00", status="Done",
                       category="Done", assignee="bob",
                       transitions=[("2024-03-02 10:00", "3", "10001", "bob")]),
            make_issue(3, "XYZ-1", updated="2024-03-03 10:00", assignee=None),
        ]
    )
    patched = partial(JiraClient, transport=fake.transport, sleep=lambda s: None)
    # Both call sites: the sync, and the route's connection test.
    monkeypatch.setattr(jira_sync, "JiraClient", lambda *a, transport=None, sleep=None, **k: patched(*a, **k))
    import datatalk.web.routes_orgs as routes

    monkeypatch.setattr(routes.jira_client, "JiraClient", patched)
    return fake


def _jira_source(db, org, name="jira"):
    conn = give_connection(
        db, org.id, name=name, type="jira", host="acme.atlassian.net",
        database="jira", is_default=False,
    )
    conn.username = "bot@example.com"
    conn.password = TOKEN
    db.flush()
    jira_service.provision(db, conn)
    return conn


def _reader(db, conn) -> psycopg.Connection:
    """Connect exactly as the agent would: from the resolved spec."""
    spec = orgs_svc.spec_from_connection(conn)
    return psycopg.connect(
        host=spec.host, port=spec.port, user=spec.username, password=spec.password,
        dbname=spec.database, autocommit=True,
    )


def _sync(org, conn, **kw):
    return jira_service.sync_connection(org.id, conn.id, **kw)


# --- isolation: enforced by the server ---------------------------------------


def test_a_reader_role_sees_its_own_schema_and_nothing_else(db, store, org_a, org_b, fake_jira):
    mine = _jira_source(db, org_a)
    theirs = _jira_source(db, org_b)
    _sync(org_a, mine)
    _sync(org_b, theirs)
    their_schema = theirs.sync_state.schema_name

    with _reader(db, mine) as r:
        # Its own data, even unqualified (search_path is set on the role).
        assert r.execute("SELECT count(*) FROM issues").fetchone()[0] == 3

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            r.execute(f"SELECT count(*) FROM {their_schema}.issues")


@pytest.mark.parametrize(
    "stmt",
    [
        "INSERT INTO issues (id, key, project_key, created, updated) "
        "VALUES (99, 'Z-1', 'Z', now(), now())",
        "DELETE FROM issues",
        "CREATE TABLE sneaky (x int)",
        "CREATE TABLE public.sneaky (x int)",
        "CREATE TEMP TABLE sneaky (x int)",
    ],
)
def test_a_reader_role_cannot_write_anywhere(db, store, org_a, fake_jira, stmt):
    conn = _jira_source(db, org_a)
    _sync(org_a, conn)
    with _reader(db, conn) as r, pytest.raises(psycopg.Error):
        r.execute(stmt)


@pytest.mark.parametrize(
    "table", ["users", "auth_sessions", "org_warehouse_connections", "source_sync_state"]
)
def test_a_reader_cannot_read_the_app_database(db, store, org_a, database_url, table):
    """On a shared server Postgres grants CONNECT to PUBLIC by default, so the
    role may be able to *connect* to the app database (docs/jira.md says how to
    revoke that). What it must never do is read it: no app table grants PUBLIC
    anything, and this pins that."""
    conn = _jira_source(db, org_a)
    spec = orgs_svc.spec_from_connection(conn)
    app_db = make_url(database_url).database
    try:
        r = psycopg.connect(
            host=spec.host, port=spec.port, user=spec.username,
            password=spec.password, dbname=app_db, autocommit=True,
        )
    except psycopg.OperationalError:
        return  # CONNECT revoked: stronger still
    with r, pytest.raises(psycopg.errors.InsufficientPrivilege):
        r.execute(f"SELECT * FROM public.{table} LIMIT 1")


def test_the_agent_spec_never_carries_the_jira_token(db, store, org_a):
    conn = _jira_source(db, org_a)
    spec = orgs_svc.spec_from_connection(conn)
    assert spec.type == "jira"
    assert TOKEN not in repr(spec)
    assert TOKEN not in json.dumps(spec.__dict__, default=str)
    assert spec.username == conn.sync_state.role_name
    assert "acme.atlassian.net" not in (spec.host, spec.database)
    assert TOKEN not in json.dumps(conn.to_public_dict(), default=str)
    assert conn.sync_state.role_password not in json.dumps(conn.to_public_dict(), default=str)


def test_an_unprovisioned_source_is_unreachable_not_local(db, org_a, monkeypatch):
    """No store configured: the source resolves to a host that cannot resolve,
    never to an empty host (libpq's local socket)."""
    monkeypatch.setenv("DATATALK_SYNC_DATABASE_URL", "")
    get_settings.cache_clear()
    conn = give_connection(db, org_a.id, name="jira", type="jira",
                           host="acme.atlassian.net", is_default=False)
    spec = orgs_svc.spec_from_connection(conn)
    assert spec.host == syncstore.UNREACHABLE_HOST
    get_settings.cache_clear()


# --- the sync ----------------------------------------------------------------


def test_first_sync_is_full_and_fills_every_table(db, store, org_a, fake_jira):
    conn = _jira_source(db, org_a)
    state = _sync(org_a, conn)

    assert state["status"] == "ok"
    assert state["stats"]["full"] is True
    assert state["stats"]["tables"] == {
        "projects": 2, "users": 3, "issues": 3, "status_changes": 2,
        "boards": 1, "sprints": 1, "issue_sprints": 1,
        # ABC-1 was created into sprint 7; each transition also moved assignee.
        "sprint_events": 1, "field_changes": 2, "worklogs": 1,
    }
    assert state["stats"]["boards_available"] is True
    assert conn.sync_state.cursor is not None
    assert conn.sync_state.last_synced_at is not None
    with _reader(db, conn) as r:
        assert r.execute(
            "SELECT key, status_category, story_points FROM issues ORDER BY id"
        ).fetchall() == [("ABC-1", "To Do", 3), ("ABC-2", "Done", None), ("XYZ-1", "To Do", None)]
        assert r.execute("SELECT synced_at IS NOT NULL FROM _sync_meta").fetchone()[0]


def test_incremental_sync_rereads_only_what_changed(db, store, org_a, fake_jira):
    conn = _jira_source(db, org_a)
    _sync(org_a, conn)

    # ABC-1 moves to Done; a new issue appears. Both are after the cursor.
    fake_jira.issues[0] = make_issue(
        1, "ABC-1", created="2024-03-01 09:00", updated="2024-03-05 10:00",
        status="Done", category="Done",
        transitions=[("2024-03-01 10:00", "1", "3", "alice"),
                     ("2024-03-05 10:00", "3", "10001", "alice")],
    )
    fake_jira.issues.append(make_issue(4, "ABC-4", updated="2024-03-06 10:00"))
    state = _sync(org_a, conn)

    assert state["stats"]["full"] is False
    # The two changes, plus XYZ-1 again: it sits at the cursor, inside the
    # deliberate overlap. Re-reading is harmless; missing an edit is not.
    assert state["stats"]["issues_synced"] == 3
    assert fake_jira.searches()[-1].startswith('updated >= "2024/03/03 09:58"')
    with _reader(db, conn) as r:
        assert r.execute("SELECT count(*) FROM issues").fetchone()[0] == 4
        # Children are replaced, not appended: two transitions, not three.
        assert r.execute(
            "SELECT count(*) FROM status_changes WHERE issue_key = 'ABC-1'"
        ).fetchone()[0] == 2
        # Its worklog is gone from Jira, so it is gone here too.
        assert r.execute("SELECT count(*) FROM worklogs").fetchone()[0] == 0


def test_only_a_full_sync_notices_a_deleted_issue(db, store, org_a, fake_jira):
    conn = _jira_source(db, org_a)
    _sync(org_a, conn)
    del fake_jira.issues[2]  # XYZ-1 deleted in Jira

    _sync(org_a, conn)
    with _reader(db, conn) as r:
        assert r.execute("SELECT count(*) FROM issues").fetchone()[0] == 3

    state = _sync(org_a, conn, full=True)
    assert state["stats"]["issues_deleted"] == 1
    with _reader(db, conn) as r:
        assert r.execute("SELECT count(*) FROM issues").fetchone()[0] == 2
        # Its project had no other issues, so it goes too.
        assert r.execute("SELECT array_agg(key) FROM projects").fetchone()[0] == ["ABC"]


def test_a_failed_sync_records_the_error_and_keeps_the_cursor(db, store, org_a, fake_jira):
    conn = _jira_source(db, org_a)
    _sync(org_a, conn)
    cursor = conn.sync_state.cursor

    fake_jira.status_code_override = 500
    with pytest.raises(Exception):
        _sync(org_a, conn)

    assert conn.sync_state.last_status == "error"
    assert "500" in conn.sync_state.last_error
    assert conn.sync_state.cursor == cursor


def test_a_second_concurrent_sync_is_refused_without_touching_state(db, store, org_a, fake_jira):
    conn = _jira_source(db, org_a)
    with syncstore.admin_connection() as holder, syncstore.source_lock(
        holder, conn.sync_state.schema_name
    ):
        with pytest.raises(syncstore.SyncInProgressError):
            _sync(org_a, conn)
    assert conn.sync_state.last_status == "never"


def test_a_sync_repairs_a_store_that_lost_the_schema(db, store, org_a, fake_jira):
    conn = _jira_source(db, org_a)
    _sync(org_a, conn)
    jira_service.deprovision(conn.sync_state.schema_name, conn.sync_state.role_name)

    state = _sync(org_a, conn)  # incremental cursor, but no tables -> full rebuild
    assert state["stats"]["full"] is True
    with _reader(db, conn) as r:
        assert r.execute("SELECT count(*) FROM issues").fetchone()[0] == 3


# --- the agent path ----------------------------------------------------------


def test_the_agent_queries_jira_through_the_ordinary_sql_path(db, store, org_a, user_a, fake_jira):
    from datatalk.agent.executor import run_sql
    from datatalk.warehouse import catalog

    give_connection(db, org_a.id, name="main")
    conn = _jira_source(db, org_a)
    _sync(org_a, conn)
    ctx = orgs_svc.build_tenant_context(db, org=org_a, user=user_a, role="owner")

    text = catalog.build_catalog(ctx)
    assert "jira" in text and "issues" in text and "status_changes" in text
    # Labelled by the SQL it speaks: `[jira]` beside ClickHouse sources got
    # ClickHouse syntax aimed at PostgreSQL.
    assert 'SOURCE "jira" [postgres, synced from Jira]' in text
    assert "write PostgreSQL there even when other sources are ClickHouse" in text
    # Shared with the main (ClickHouse) source's org, but the PostgreSQL hint
    # appears once, and carries the two constraints that failed in practice.
    assert text.count("PostgreSQL dialect") == 1
    assert "count(DISTINCT (a, b))" in text and "must be parenthesized" in text

    detail = catalog.describe_table(ctx, "jira", "issues")
    assert "status_category" in detail and "One of 'To Do'" in detail  # column comment

    schema = conn.sync_state.schema_name
    result = run_sql(
        f"SELECT status_category, count(*) AS n FROM {schema}.issues "
        "GROUP BY 1 ORDER BY 1",
        ctx=ctx,
        source="jira",
    )
    assert result.to_records() == [
        {"status_category": "Done", "n": 1},
        {"status_category": "To Do", "n": 2},
    ]


# --- routes ------------------------------------------------------------------

_BODY = {
    "type": "jira",
    "name": "jira",
    "host": "https://acme.atlassian.net/jira/",
    "user": "bot@example.com",
    "password": TOKEN,
    "scope_query": "project = ABC",
}


def test_creating_a_jira_source_provisions_it(auth_client, db, store):
    resp = auth_client.post(f"/api/orgs/{auth_client.org_id}/connections", json=_BODY)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["host"] == "acme.atlassian.net"  # normalized
    assert body["scope_query"] == "project = ABC"
    assert body["sync"]["status"] == "never"
    assert "Jira (acme.atlassian.net)" in body["description"]
    assert TOKEN not in resp.text

    with syncstore.admin_connection() as c:
        assert len(syncstore.managed_schemas(c)) == 1


def test_a_host_off_the_allowlist_is_refused(auth_client, store):
    resp = auth_client.post(
        f"/api/orgs/{auth_client.org_id}/connections",
        json={**_BODY, "host": "169.254.169.254"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "jira_host_not_allowed"


def test_without_a_sync_store_jira_is_refused_not_redirected(auth_client, monkeypatch):
    monkeypatch.setenv("DATATALK_SYNC_DATABASE_URL", "")
    get_settings.cache_clear()
    resp = auth_client.post(f"/api/orgs/{auth_client.org_id}/connections", json=_BODY)
    get_settings.cache_clear()
    assert resp.status_code == 409
    assert resp.json()["detail"] == "sync_store_unconfigured"


def test_a_source_cannot_change_between_synced_and_live(auth_client, db, store):
    created = auth_client.post(
        f"/api/orgs/{auth_client.org_id}/connections", json=_BODY
    ).json()
    resp = auth_client.put(
        f"/api/orgs/{auth_client.org_id}/connections/{created['id']}",
        json={"type": "postgres", "name": "jira", "host": "db", "user": "u", "database": "d"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "source_type_immutable"


def test_changing_the_scope_forces_a_full_resync(auth_client, db, store, fake_jira):
    org_id = auth_client.org_id
    created = auth_client.post(f"/api/orgs/{org_id}/connections", json=_BODY).json()
    conn = orgs_svc.get_connection(db, org_id, created["id"])
    jira_service.sync_connection(org_id, conn.id)
    assert conn.sync_state.cursor is not None

    auth_client.put(
        f"/api/orgs/{org_id}/connections/{created['id']}",
        json={**_BODY, "password": None, "scope_query": "project = XYZ"},
    )
    assert conn.sync_state.cursor is None


def test_deleting_a_jira_source_drops_its_schema_and_role(auth_client, db, store):
    created = auth_client.post(
        f"/api/orgs/{auth_client.org_id}/connections", json=_BODY
    ).json()
    from uuid import UUID

    _, role = syncstore.names_for(UUID(created["id"]))
    resp = auth_client.delete(f"/api/orgs/{auth_client.org_id}/connections/{created['id']}")
    assert resp.status_code == 204
    with syncstore.admin_connection() as c:
        assert syncstore.managed_schemas(c) == []
        # Roles are cluster-wide, so a developer's own synced sources share
        # this namespace: check this source's role, not "no roles at all".
        assert c.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone()[0] == 0


def test_testing_a_jira_connection_signs_in_and_counts(auth_client, store, fake_jira):
    resp = auth_client.post(
        f"/api/orgs/{auth_client.org_id}/connections/test", json=_BODY
    )
    body = resp.json()
    assert body["ok"] is True, body
    assert body["account"] == "Sync Bot"
    assert body["issue_count"] == 3


def test_the_sync_endpoint_streams_progress_then_the_state(auth_client, db, store, fake_jira):
    org_id = auth_client.org_id
    created = auth_client.post(f"/api/orgs/{org_id}/connections", json=_BODY).json()
    resp = auth_client.post(f"/api/orgs/{org_id}/connections/{created['id']}/sync")
    assert resp.status_code == 200
    events = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    kinds = [e["kind"] for e in events if e["kind"] != "ping"]
    assert kinds[0] == "start" and "page" in kinds
    assert kinds[-2:] == ["synced", "done"]
    assert events[-2]["data"]["status"] == "ok"


def test_syncing_another_orgs_source_is_a_404(auth_client, db, store, org_b):
    theirs = _jira_source(db, org_b)
    resp = auth_client.post(
        f"/api/orgs/{auth_client.org_id}/connections/{theirs.id}/sync"
    )
    assert resp.status_code == 404


def test_syncing_a_live_source_is_a_404(auth_client, db, store):
    live = orgs_svc.list_connections(db, auth_client.org_id)[0]
    resp = auth_client.post(f"/api/orgs/{auth_client.org_id}/connections/{live.id}/sync")
    assert resp.status_code == 404


def test_members_cannot_sync(api_client, db, store):
    org_id = signup(api_client)
    conn = _jira_source(db, db.get(models.Org, org_id))
    membership = db.query(models.Membership).filter_by(org_id=org_id).one()
    membership.role = "member"
    db.flush()
    resp = api_client.post(f"/api/orgs/{org_id}/connections/{conn.id}/sync")
    assert resp.status_code == 403


def test_a_saved_dashboard_over_jira_refreshes_to_the_latest_sync(
    db, store, org_a, user_a, fake_jira
):
    """The point of sync-to-Postgres: refresh re-runs captured SQL with no
    Jira-specific path, so a new sync is all it takes for new numbers."""
    from datatalk.agent.blocks import Document, Stat, materialize
    from datatalk.agent.executor import run_sql
    from datatalk.dashboards import refresh as refresh_svc
    from datatalk.memory.store import SavedDashboard

    conn = _jira_source(db, org_a)
    _sync(org_a, conn)
    ctx = orgs_svc.build_tenant_context(db, org=org_a, user=user_a, role="owner")
    sql = f"SELECT count(*) AS open FROM {conn.sync_state.schema_name}.issues " \
          "WHERE status_category <> 'Done'"

    authoring = Document(blocks=[Stat(dataset_id="q1", value_col="open", label="Open")])
    first = run_sql(sql, ctx=ctx, source="jira")
    saved = SavedDashboard(
        id=1, request="r", title="t", created_at="2026-09-23T00:00:00+00:00",
        document=materialize(authoring, {"q1": first}),
        queries=[{"dataset_id": "q1", "source": "jira", "sql": sql,
                  "columns": ["open"], "row_count": 1}],
        authoring_document=authoring,
    )
    assert saved.document.blocks[0].value == 2

    fake_jira.issues.append(make_issue(9, "ABC-9", updated="2024-03-09 10:00"))
    _sync(org_a, conn)

    out = refresh_svc.refresh_dashboard(saved, ctx=ctx)
    assert out.partial is False
    assert out.document.blocks[0].value == 3


def test_the_sql_the_postgres_hint_recommends_actually_runs(db, store, org_a, user_a, fake_jira):
    """A hint that recommends invalid SQL is worse than none."""
    from datatalk.agent.executor import run_sql

    conn = _jira_source(db, org_a)
    _sync(org_a, conn)
    ctx = orgs_svc.build_tenant_context(db, org=org_a, user=user_a, role="owner")
    sc = conn.sync_state.schema_name

    distinct = run_sql(
        f"SELECT count(DISTINCT (issue_id, changed_at)) AS n FROM {sc}.status_changes",
        ctx=ctx, source="jira",
    )
    assert distinct.to_records() == [{"n": 2}]

    union = run_sql(
        f"(SELECT key FROM {sc}.issues ORDER BY key LIMIT 1) UNION ALL "
        f"(SELECT key FROM {sc}.issues ORDER BY key DESC LIMIT 1)",
        ctx=ctx, source="jira",
    )
    assert [r["key"] for r in union.to_records()] == ["ABC-1", "XYZ-1"]


# --- history: what sprint and estimate reports are built from ----------------


def _history_issues():
    s8 = sprint(8, "closed", "2024-03-04 09:00", "2024-03-18 09:00", "2024-03-18 10:00")
    s9 = sprint(9, "active", "2024-03-18 11:00", "2024-04-01 09:00")
    return [
        # Created into sprint 8, carried over into 9 at 8's close.
        make_issue(
            10, "ABC-10", created="2024-03-01 09:00", updated="2024-03-18 10:00",
            points=5, sprints=[s8, s9],
            history=[("2024-03-18 10:00", "sam", [sprint_change([8], [8, 9])])],
        ),
        # Added to 8 mid-sprint, re-estimated 2 -> 3, then removed again.
        make_issue(
            11, "ABC-11", created="2024-03-01 09:00", updated="2024-03-09 09:00",
            points=3, sprints=[],
            history=[
                ("2024-03-06 09:00", "sam", [sprint_change([], [8])]),
                ("2024-03-07 09:00", "sam", [points_change(2, 3)]),
                ("2024-03-09 09:00", "sam", [sprint_change([8], [])]),
            ],
        ),
    ]


def test_sprint_membership_history_is_reconstructed(db, store, org_a, fake_jira):
    fake_jira.issues = _history_issues()
    conn = _jira_source(db, org_a)
    _sync(org_a, conn)

    with _reader(db, conn) as r:
        events = r.execute(
            "SELECT issue_key, sprint_id, action, to_char(changed_at AT TIME ZONE 'UTC', "
            "'MM-DD HH24:MI') FROM sprint_events ORDER BY changed_at, issue_key, sprint_id"
        ).fetchall()
        # Removed from sprint 8 and in no sprint now: the sprint row survives
        # the prune because its history still references it.
        sprint_ids = [row[0] for row in r.execute("SELECT id FROM sprints ORDER BY id")]
        points = r.execute(
            "SELECT from_value::numeric, to_value::numeric FROM field_changes "
            "WHERE field = 'story_points'"
        ).fetchall()

    assert events == [
        ("ABC-10", 8, "added", "03-01 09:00"),     # created into it: no changelog item
        ("ABC-11", 8, "added", "03-06 09:00"),     # scope added mid-sprint
        ("ABC-11", 8, "removed", "03-09 09:00"),   # and removed again
        ("ABC-10", 9, "added", "03-18 10:00"),     # carry-over
    ]
    assert sprint_ids == [8, 9]
    assert points == [(2, 3)]


def test_an_issue_with_no_sprint_changes_starts_in_its_current_sprints(db, store, org_a, fake_jira):
    conn = _jira_source(db, org_a)
    _sync(org_a, conn)  # ABC-1: sprint 7 in its field, no Sprint changelog item
    with _reader(db, conn) as r:
        assert r.execute(
            "SELECT issue_key, sprint_id, action, author_id FROM sprint_events"
        ).fetchall() == [("ABC-1", 7, "added", None)]


def test_boards_fill_sprints_missing_a_board(db, store, org_a, fake_jira):
    fake_jira.board_sprints = {1: [{"id": 7, "originBoardId": 1}]}
    conn = _jira_source(db, org_a)
    _sync(org_a, conn)
    with _reader(db, conn) as r:
        assert r.execute("SELECT id, name, board_type, project_key FROM boards").fetchall() == [
            (1, "ABC board", "scrum", "ABC")
        ]
        assert r.execute("SELECT board_id FROM sprints WHERE id = 7").fetchone() == (1,)
        assert r.execute("SELECT time_zone FROM _sync_meta").fetchone() == ("UTC",)


@pytest.mark.parametrize("status", [403, 404])
def test_no_jira_software_is_not_a_failed_sync(db, store, org_a, fake_jira, status):
    fake_jira.agile_status = status
    conn = _jira_source(db, org_a)
    state = _sync(org_a, conn)
    assert state["status"] == "ok"
    assert state["stats"]["boards_available"] is False
    assert state["stats"]["tables"]["boards"] == 0
    assert state["stats"]["tables"]["issues"] == 3


def test_a_v1_store_is_rebuilt_and_fully_resynced(db, store, org_a, fake_jira):
    conn = _jira_source(db, org_a)
    _sync(org_a, conn)
    with syncstore.admin_connection() as c:
        c.execute(
            f"UPDATE {conn.sync_state.schema_name}._sync_meta SET schema_version = 1"
        )
    state = _sync(org_a, conn)  # would be incremental, but the schema is stale
    assert state["stats"]["full"] is True
    assert state["stats"]["tables"]["sprint_events"] == 1
