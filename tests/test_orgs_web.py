"""Org membership, org switching, and per-org data source endpoints.

These were covered only by the blanket "every route needs auth" walk in
test_auth_web.py. The source endpoints are load-bearing -- they are the only way
an org gets a warehouse, and the only thing standing between a new org and the
deployment's own -- so they get real tests.

An org holds several named sources of either engine, so the CRUD tests run over
both types and the collection semantics (naming, defaults, deletion) are pinned
here.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from datatalk.auth import orgs as orgs_svc
from datatalk.db import models
from tests.conftest import TEST_PASSWORD, give_connection, signup

# Every endpoint that reaches a warehouse, with a minimal valid body.
# /api/analyze is deliberately absent: it critiques text it is given and runs
# zero SQL, so it follows the Postgres-only rule instead (see below).
WAREHOUSE_ROUTES = [
    ("get", "/api/health", None),
    ("get", "/api/schema", None),
    ("post", "/api/report", {"request": "revenue"}),
    ("post", "/api/dashboard", {"request": "revenue"}),
]


# --- the 409 no_connection contract -------------------------------------------


@pytest.mark.parametrize("method,path,body", WAREHOUSE_ROUTES)
def test_warehouse_routes_409_without_connection(
    connectionless_client, method, path, body
):
    """A new org must never reach the deployment's env warehouse.

    Before require_connection was wired, the settings overlay fell back to the
    env CLICKHOUSE_* values and these returned 200 with another tenant's data.
    """
    resp = getattr(connectionless_client, method)(path, json=body) if body else (
        getattr(connectionless_client, method)(path)
    )
    assert resp.status_code == 409, f"{path} -> {resp.status_code} {resp.text}"
    assert resp.json()["detail"] == "no_connection"


def test_postgres_routes_still_work_without_connection(connectionless_client):
    """The library and settings must load, or the user cannot fix the problem."""
    for path in ("/api/reports", "/api/dashboards", "/api/suggestions"):
        assert connectionless_client.get(path).status_code == 200, path


def test_a_connectionless_org_can_analyze_pasted_text(
    connectionless_client, monkeypatch
):
    """analyze critiques the text it is given and runs no SQL, so gating it on
    a warehouse would lock a new org out of the one feature that needs none."""
    import datatalk.web.app as web

    monkeypatch.setattr(web, "analyze_report", lambda text, **kw: "## Fine")
    resp = connectionless_client.post("/api/analyze", json={"text": "Q3 was up"})
    assert resp.status_code == 200
    assert resp.json() == {"analysis": "## Fine", "source": "external"}


def test_a_dead_embeddings_endpoint_degrades_analyze_instead_of_500ing(
    auth_client, monkeypatch
):
    import datatalk.web.app as web
    from datatalk.memory.store import MemoryStore

    seen = {}

    def boom(self, *a, **kw):
        raise RuntimeError("embeddings down")

    monkeypatch.setattr(MemoryStore, "retrieve_suggestion_texts", boom)
    monkeypatch.setattr(
        web,
        "analyze_report",
        lambda text, memory_suggestions=None, **kw: (
            seen.update(suggestions=memory_suggestions) or "## Still fine"
        ),
    )

    resp = auth_client.post("/api/analyze", json={"text": "Q3 was up"})
    assert resp.status_code == 200
    assert resp.json()["analysis"] == "## Still fine"
    assert seen["suggestions"] == []


def test_deleting_the_last_source_re_closes_the_door(auth_client, db):
    org_id = auth_client.org_id
    assert auth_client.get("/api/schema").status_code != 409

    cid = orgs_svc.list_connections(db, org_id)[0].id
    assert auth_client.delete(f"/api/orgs/{org_id}/connections/{cid}").status_code == 204

    resp = auth_client.get("/api/schema")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "no_connection"


def test_me_reports_connection_state(connectionless_client, db):
    """The frontend boots off this flag to decide whether to nudge to settings."""
    assert connectionless_client.get("/api/auth/me").json()["connection"]["configured"] is False

    give_connection(db, connectionless_client.org_id)
    assert connectionless_client.get("/api/auth/me").json()["connection"]["configured"] is True


# --- org listing and switching ------------------------------------------------


def _make_second_org(db, client, *, role="owner", name="Second Org"):
    """Create another org the current user belongs to, and return it."""
    user_id = client.get("/api/auth/me").json()["user"]["id"]
    org = models.Org(name=name, slug=orgs_svc.unique_slug(db, name))
    db.add(org)
    db.flush()
    db.add(models.Membership(org_id=org.id, user_id=user_id, role=role))
    db.flush()
    return org


def test_list_orgs_marks_the_current_one(auth_client, db):
    second = _make_second_org(db, auth_client)

    orgs = auth_client.get("/api/orgs").json()["orgs"]
    by_id = {o["id"]: o for o in orgs}

    assert by_id[str(auth_client.org_id)]["is_current"] is True
    assert by_id[str(second.id)]["is_current"] is False


def test_switch_org_changes_the_session(auth_client, db):
    second = _make_second_org(db, auth_client)

    resp = auth_client.post(f"/api/orgs/{second.id}/switch")
    assert resp.status_code == 200
    assert resp.json()["org"]["id"] == str(second.id)

    # Server-side state, not a cookie: the next request acts as the new org.
    assert auth_client.get("/api/auth/me").json()["org"]["id"] == str(second.id)
    assert auth_client.get("/api/orgs").json()["orgs"]


def test_switch_to_an_org_you_do_not_belong_to_is_403(auth_client, db):
    stranger = models.Org(name="Stranger", slug=orgs_svc.unique_slug(db, "Stranger"))
    db.add(stranger)
    db.flush()

    resp = auth_client.post(f"/api/orgs/{stranger.id}/switch")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "not_a_member"


def test_switching_org_carries_the_new_orgs_connection(auth_client, db):
    """The tenant boundary must move with the switch, not lag behind it."""
    second = _make_second_org(db, auth_client)
    auth_client.post(f"/api/orgs/{second.id}/switch")

    # The second org has no connection of its own.
    assert auth_client.get("/api/schema").status_code == 409

    give_connection(db, second.id, host="second.test")
    assert auth_client.get("/api/schema").status_code != 409


def test_create_org(auth_client):
    resp = auth_client.post("/api/orgs", json={"name": "Brand New"})
    assert resp.status_code == 201
    assert resp.json()["org"]["role"] == "owner"
    assert resp.json()["org"]["slug"] == "brand-new"


def test_create_org_requires_a_name(auth_client):
    assert auth_client.post("/api/orgs", json={"name": "   "}).status_code == 400


# --- cross-org access ---------------------------------------------------------


def test_another_orgs_connection_is_404_not_403(auth_client, api_client, db):
    """404 so the response never confirms that another org exists."""
    other = models.Org(name="Other", slug=orgs_svc.unique_slug(db, "Other"))
    db.add(other)
    db.flush()
    give_connection(db, other.id, host="secret.internal")

    victim = orgs_svc.list_connections(db, other.id)[0]

    assert auth_client.get(f"/api/orgs/{other.id}/connections").status_code == 404
    resp = auth_client.delete(f"/api/orgs/{other.id}/connections/{victim.id}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "org_not_found"

    resp = auth_client.put(
        f"/api/orgs/{other.id}/connections/{victim.id}",
        json={"type": "clickhouse", "name": "pwned", "host": "attacker.test"},
    )
    assert resp.status_code == 404

    # And the other org's source is untouched.
    assert orgs_svc.default_connection(db, other.id).host == "secret.internal"


# --- the admin gate -----------------------------------------------------------


@pytest.fixture
def member_client(auth_client, db):
    """The same user acting in an org where they are only a 'member'."""
    org = _make_second_org(db, auth_client, role="member", name="Member Org")
    auth_client.post(f"/api/orgs/{org.id}/switch")
    auth_client.org_id = org.id
    return auth_client


def test_members_cannot_write_sources(member_client, db):
    org_id = member_client.org_id
    existing = give_connection(db, org_id, host="readable.test")
    body = {"type": "clickhouse", "name": "attempt", "host": "h.test", "password": "p"}

    assert member_client.post(f"/api/orgs/{org_id}/connections", json=body).status_code == 403
    assert member_client.put(
        f"/api/orgs/{org_id}/connections/{existing.id}", json=body
    ).status_code == 403
    assert member_client.post(
        f"/api/orgs/{org_id}/connections/test", json=body
    ).status_code == 403
    assert member_client.delete(
        f"/api/orgs/{org_id}/connections/{existing.id}"
    ).status_code == 403


def test_members_can_still_read_sources(member_client, db):
    give_connection(db, member_client.org_id, host="readable.test")
    resp = member_client.get(f"/api/orgs/{member_client.org_id}/connections")
    assert resp.status_code == 200
    assert resp.json()["connections"][0]["host"] == "readable.test"


# --- the password never leaves the server -------------------------------------


def _body(**over):
    base = {
        "type": "clickhouse",
        "name": "warehouse",
        "host": "h.test",
        "user": "u",
        "database": "d",
    }
    base.update(over)
    return base


def test_source_responses_never_contain_the_password(auth_client, db):
    org_id = auth_client.org_id
    secret = "sup3r-s3cret-passphrase"

    created = auth_client.post(
        f"/api/orgs/{org_id}/connections", json=_body(password=secret)
    )
    assert created.status_code == 201, created.text
    assert secret not in created.text
    assert created.json()["has_password"] is True

    listed = auth_client.get(f"/api/orgs/{org_id}/connections")
    assert secret not in listed.text
    assert all("password" not in c for c in listed.json()["connections"])

    # Stored encrypted, but readable back through the column type.
    stored = [c for c in orgs_svc.list_connections(db, org_id) if c.name == "warehouse"]
    assert stored[0].password == secret


def test_update_without_a_password_keeps_the_stored_one(auth_client, db):
    org_id = auth_client.org_id
    created = auth_client.post(
        f"/api/orgs/{org_id}/connections", json=_body(password="original")
    ).json()

    # The UI never receives the password, so it cannot echo it back on edit.
    resp = auth_client.put(
        f"/api/orgs/{org_id}/connections/{created['id']}",
        json=_body(host="moved.test"),
    )
    assert resp.status_code == 200
    assert resp.json()["host"] == "moved.test"

    stored = orgs_svc.get_connection(db, org_id, UUID(created["id"]))
    assert stored.password == "original"


def test_listing_is_empty_for_an_unconfigured_org(connectionless_client):
    resp = connectionless_client.get(
        f"/api/orgs/{connectionless_client.org_id}/connections"
    )
    assert resp.status_code == 200
    assert resp.json() == {"connections": []}


# --- the collection: naming, engines, defaults --------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"type": "clickhouse", "name": "events", "host": "ch.test"},
        {"type": "postgres", "name": "billing", "host": "pg.test"},
    ],
)
def test_creating_a_source_of_either_engine(auth_client, db, body):
    resp = auth_client.post(f"/api/orgs/{auth_client.org_id}/connections", json=body)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["type"] == body["type"]
    # Each engine brings its own defaults; sharing them would silently point a
    # Postgres source at ClickHouse's port.
    assert out["port"] == (8123 if body["type"] == "clickhouse" else 5432)


def test_an_org_can_hold_several_sources(auth_client, db):
    org_id = auth_client.org_id
    auth_client.post(f"/api/orgs/{org_id}/connections", json=_body(name="events"))
    auth_client.post(
        f"/api/orgs/{org_id}/connections",
        json=_body(name="billing", type="postgres", host="pg.test"),
    )

    names = [c["name"] for c in auth_client.get(
        f"/api/orgs/{org_id}/connections"
    ).json()["connections"]]
    # The fixture's "default" plus both new ones.
    assert set(names) == {"default", "events", "billing"}


def test_source_names_are_unique_per_org(auth_client):
    org_id = auth_client.org_id
    auth_client.post(f"/api/orgs/{org_id}/connections", json=_body(name="events"))

    resp = auth_client.post(f"/api/orgs/{org_id}/connections", json=_body(name="events"))
    assert resp.status_code == 409
    assert resp.json()["detail"] == "duplicate_source_name"


@pytest.mark.parametrize("name", ["Has Spaces", "UPPER", "9leading", "", "with-dash"])
def test_source_names_must_be_plain_identifiers(auth_client, name):
    """The agent types this name, so anything it could mangle is rejected."""
    resp = auth_client.post(
        f"/api/orgs/{auth_client.org_id}/connections", json=_body(name=name)
    )
    assert resp.status_code == 422


def test_the_first_source_becomes_the_default(connectionless_client):
    org_id = connectionless_client.org_id
    resp = connectionless_client.post(
        f"/api/orgs/{org_id}/connections", json=_body(name="only", is_default=False)
    )
    # Requested false, but something has to resolve for a source-less query.
    assert resp.json()["is_default"] is True


def test_promoting_a_source_demotes_the_previous_default(auth_client, db):
    org_id = auth_client.org_id
    created = auth_client.post(
        f"/api/orgs/{org_id}/connections", json=_body(name="events", is_default=True)
    ).json()
    assert created["is_default"] is True

    defaults = [c for c in orgs_svc.list_connections(db, org_id) if c.is_default]
    assert [c.name for c in defaults] == ["events"]


def test_deleting_the_default_promotes_another(auth_client, db):
    """Something must stay resolvable, or every unqualified query starts failing."""
    org_id = auth_client.org_id
    auth_client.post(f"/api/orgs/{org_id}/connections", json=_body(name="events"))
    original = [c for c in orgs_svc.list_connections(db, org_id) if c.is_default][0]

    assert auth_client.delete(
        f"/api/orgs/{org_id}/connections/{original.id}"
    ).status_code == 204

    remaining = orgs_svc.list_connections(db, org_id)
    assert len(remaining) == 1
    assert remaining[0].is_default is True


def test_deleting_an_unknown_source_is_404(auth_client):
    resp = auth_client.delete(
        f"/api/orgs/{auth_client.org_id}/connections/{uuid4()}"
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "connection_not_found"


# --- connection test endpoint -------------------------------------------------


def test_failed_connection_test_is_a_200_not_a_502(auth_client):
    """A failed test is a successful API call -- the UI shows the driver error."""
    resp = auth_client.post(
        f"/api/orgs/{auth_client.org_id}/connections/test",
        json=_body(host="nonexistent.invalid", port=9, password="p"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]


def test_a_failed_postgres_test_is_also_a_200(auth_client):
    """The adapter wraps psycopg's errors, so the shape must not change by engine."""
    resp = auth_client.post(
        f"/api/orgs/{auth_client.org_id}/connections/test",
        json=_body(type="postgres", host="nonexistent.invalid", port=9, password="p"),
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_connection_test_does_not_save(auth_client, db):
    before = {c.name for c in orgs_svc.list_connections(db, auth_client.org_id)}
    auth_client.post(
        f"/api/orgs/{auth_client.org_id}/connections/test",
        json=_body(name="candidate", host="candidate.invalid", password="p"),
    )
    after = {c.name for c in orgs_svc.list_connections(db, auth_client.org_id)}
    assert after == before


# --- the introspection scope --------------------------------------------------


def test_the_scope_round_trips_through_create_and_update(auth_client, db):
    org_id = auth_client.org_id

    created = auth_client.post(
        f"/api/orgs/{org_id}/connections",
        json=_body(
            name="scoped",
            password="p",
            introspect_databases=["analytics", "billing"],
            introspect_tables=["analytics.events", "analytics.sessions"],
        ),
    )
    assert created.status_code == 201
    assert created.json()["introspect_databases"] == ["analytics", "billing"]
    assert created.json()["introspect_tables"] == [
        "analytics.events",
        "analytics.sessions",
    ]

    updated = auth_client.put(
        f"/api/orgs/{org_id}/connections/{created.json()['id']}",
        json=_body(name="scoped", introspect_databases=["billing"], introspect_tables=[]),
    )
    assert updated.status_code == 200
    assert updated.json()["introspect_databases"] == ["billing"]
    # An emptied selection must actually clear, not fall through to "unchanged".
    assert updated.json()["introspect_tables"] == []


def test_an_omitted_scope_leaves_the_stored_one_alone(auth_client, db):
    """The form always sends the scope, but a scripted caller need not."""
    org_id = auth_client.org_id
    created = auth_client.post(
        f"/api/orgs/{org_id}/connections",
        json=_body(name="keeper", password="p", introspect_databases=["analytics"]),
    )
    updated = auth_client.put(
        f"/api/orgs/{org_id}/connections/{created.json()['id']}",
        json=_body(name="keeper"),
    )
    assert updated.json()["introspect_databases"] == ["analytics"]


# --- discovery endpoint -------------------------------------------------------


def test_failed_discovery_is_a_200_not_a_502(auth_client):
    """Same contract as test: the picker shows the driver error in place."""
    resp = auth_client.post(
        f"/api/orgs/{auth_client.org_id}/connections/discover",
        json=_body(host="nonexistent.invalid", port=9, password="p"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]


def test_discovery_does_not_save(auth_client, db):
    before = {c.name for c in orgs_svc.list_connections(db, auth_client.org_id)}
    auth_client.post(
        f"/api/orgs/{auth_client.org_id}/connections/discover",
        json=_body(name="candidate", host="candidate.invalid", password="p"),
    )
    after = {c.name for c in orgs_svc.list_connections(db, auth_client.org_id)}
    assert after == before


def test_members_cannot_browse_a_servers_contents(member_client):
    """Discovery lists table names, so it is an admin route like test is."""
    resp = member_client.post(
        f"/api/orgs/{member_client.org_id}/connections/discover",
        json=_body(host="h.test", password="p"),
    )
    assert resp.status_code == 403


def test_discovering_another_orgs_source_is_404_not_403(auth_client, db):
    other = models.Org(name="Other D", slug=orgs_svc.unique_slug(db, "Other D"))
    db.add(other)
    db.flush()
    give_connection(db, other.id, host="secret.internal")

    resp = auth_client.post(
        f"/api/orgs/{other.id}/connections/discover",
        json=_body(host="secret.internal", password="p"),
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "org_not_found"
