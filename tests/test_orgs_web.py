"""Org membership, org switching, and per-org ClickHouse connection endpoints.

These were covered only by the blanket "every route needs auth" walk in
test_auth_web.py. The connection endpoints are load-bearing now -- they are the
only way an org gets a warehouse, and the only thing standing between a new org
and the deployment's own -- so they get real tests.
"""

from __future__ import annotations

import pytest

from datatalk.auth import orgs as orgs_svc
from datatalk.db import models
from tests.conftest import TEST_PASSWORD, give_connection, signup

# Every endpoint that reaches ClickHouse, with a minimal valid body.
CLICKHOUSE_ROUTES = [
    ("get", "/api/health", None),
    ("get", "/api/schema", None),
    ("post", "/api/report", {"request": "revenue"}),
    ("post", "/api/dashboard", {"request": "revenue"}),
    ("post", "/api/analyze", {"text": "hi"}),
]


# --- the 409 no_connection contract -------------------------------------------


@pytest.mark.parametrize("method,path,body", CLICKHOUSE_ROUTES)
def test_clickhouse_routes_409_without_connection(
    connectionless_client, method, path, body
):
    """A new org must never reach the deployment's env warehouse.

    Before require_connection was wired, effective_settings fell back to the
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


def test_deleting_the_connection_re_closes_the_door(auth_client, db):
    org_id = auth_client.org_id
    assert auth_client.get("/api/schema").status_code != 409

    assert auth_client.delete(f"/api/orgs/{org_id}/connection").status_code == 204

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

    for method in ("get", "delete"):
        resp = getattr(auth_client, method)(f"/api/orgs/{other.id}/connection")
        assert resp.status_code == 404, method
        assert resp.json()["detail"] == "org_not_found"

    resp = auth_client.put(
        f"/api/orgs/{other.id}/connection",
        json={"host": "attacker.test", "user": "root", "password": "x"},
    )
    assert resp.status_code == 404

    # And the other org's connection is untouched.
    assert orgs_svc.default_connection(db, other.id).host == "secret.internal"


# --- the admin gate -----------------------------------------------------------


@pytest.fixture
def member_client(auth_client, db):
    """The same user acting in an org where they are only a 'member'."""
    org = _make_second_org(db, auth_client, role="member", name="Member Org")
    auth_client.post(f"/api/orgs/{org.id}/switch")
    auth_client.org_id = org.id
    return auth_client


def test_members_cannot_write_the_connection(member_client):
    org_id = member_client.org_id
    body = {"host": "h.test", "user": "u", "password": "p"}

    assert member_client.put(f"/api/orgs/{org_id}/connection", json=body).status_code == 403
    assert member_client.post(f"/api/orgs/{org_id}/connection/test", json=body).status_code == 403
    assert member_client.delete(f"/api/orgs/{org_id}/connection").status_code == 403


def test_members_can_still_read_the_connection(member_client, db):
    give_connection(db, member_client.org_id, host="readable.test")
    resp = member_client.get(f"/api/orgs/{member_client.org_id}/connection")
    assert resp.status_code == 200
    assert resp.json()["host"] == "readable.test"


# --- the password never leaves the server -------------------------------------


def test_connection_responses_never_contain_the_password(auth_client, db):
    org_id = auth_client.org_id
    secret = "sup3r-s3cret-passphrase"

    put = auth_client.put(
        f"/api/orgs/{org_id}/connection",
        json={"host": "h.test", "user": "u", "password": secret, "database": "d"},
    )
    assert put.status_code == 200
    assert secret not in put.text
    assert put.json()["has_password"] is True

    get = auth_client.get(f"/api/orgs/{org_id}/connection")
    assert secret not in get.text
    assert "password" not in get.json()

    # Stored encrypted, but readable back through the column type.
    assert orgs_svc.default_connection(db, org_id).password == secret


def test_put_without_a_password_keeps_the_stored_one(auth_client, db):
    org_id = auth_client.org_id
    auth_client.put(
        f"/api/orgs/{org_id}/connection",
        json={"host": "h.test", "user": "u", "password": "original", "database": "d"},
    )

    # The UI never receives the password, so it cannot echo it back on edit.
    resp = auth_client.put(
        f"/api/orgs/{org_id}/connection",
        json={"host": "moved.test", "user": "u", "database": "d"},
    )
    assert resp.status_code == 200
    assert resp.json()["host"] == "moved.test"

    connection = orgs_svc.default_connection(db, org_id)
    assert connection.password == "original"


def test_get_connection_reports_unconfigured(connectionless_client):
    resp = connectionless_client.get(f"/api/orgs/{connectionless_client.org_id}/connection")
    assert resp.status_code == 200
    assert resp.json() == {"configured": False}


# --- connection test endpoint -------------------------------------------------


def test_failed_connection_test_is_a_200_not_a_502(auth_client):
    """A failed test is a successful API call -- the UI shows the driver error."""
    resp = auth_client.post(
        f"/api/orgs/{auth_client.org_id}/connection/test",
        json={"host": "nonexistent.invalid", "port": 9, "user": "u", "password": "p"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]


def test_connection_test_does_not_save(auth_client, db):
    auth_client.post(
        f"/api/orgs/{auth_client.org_id}/connection/test",
        json={"host": "candidate.invalid", "user": "u", "password": "p"},
    )
    # The stored connection is the fixture's, untouched by the test call.
    assert orgs_svc.default_connection(db, auth_client.org_id).host == "clickhouse.test"
