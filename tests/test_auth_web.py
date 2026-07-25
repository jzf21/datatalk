"""Authentication at the HTTP boundary.

The most valuable test here is `test_every_api_route_requires_auth`: it walks
the real route table, so a future endpoint that forgets its guard fails the
suite rather than shipping open.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from datatalk.auth import passwords
from datatalk.db import models
from datatalk.web import app as web
from datatalk.web.deps import get_db

PASSWORD = "correct-horse-battery"

# Everything reachable without a session. Anything else must 401.
PUBLIC_PATHS = {
    "/",
    "/api/auth/signup",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/me",
}


@pytest.fixture(autouse=True)
def _fast_hashing():
    passwords.use_fast_params_for_tests()


@pytest.fixture
def client(db, monkeypatch):
    """A TestClient whose requests share the test's rolled-back transaction."""
    web.app.dependency_overrides[get_db] = lambda: db
    # The app's lifespan checks migrations and sweeps sessions; the fixtures
    # already migrated, and running it here would use a different connection.
    with TestClient(web.app) as c:
        yield c
    web.app.dependency_overrides.clear()


@pytest.fixture
def signed_up(client):
    resp = client.post(
        "/api/auth/signup",
        json={"email": "owner@example.com", "password": PASSWORD, "org_name": "Acme"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- the guard net ---


def test_every_api_route_requires_auth(client):
    """Walk everything the app actually serves; each /api route must 401.

    Driven off the OpenAPI schema rather than ``app.routes``: this FastAPI
    version keeps included routers as opaque wrappers, so the route list is not
    flat and a naive walk silently checks nothing.
    """
    paths = client.get("/openapi.json").json()["paths"]
    checked = 0
    for path, operations in paths.items():
        if not path.startswith("/api") or path in PUBLIC_PATHS:
            continue
        for method in sorted(m.upper() for m in operations if m.upper() not in {"HEAD", "OPTIONS"}):
            # Fill path params with a syntactically valid value.
            concrete = path.replace("{report_id}", "1").replace("{dashboard_id}", "1")
            concrete = concrete.replace("{suggestion_id}", "1")
            concrete = concrete.replace(
                "{org_id}", "00000000-0000-0000-0000-000000000000"
            )
            resp = client.request(method, concrete, json={})
            assert resp.status_code == 401, (
                f"{method} {concrete} returned {resp.status_code}, expected 401. "
                "A new endpoint is missing its auth dependency."
            )
            assert resp.json()["detail"] == "not_authenticated"
            checked += 1

    assert checked > 10, "route table looks empty; the test is not proving anything"


# --- signup / login / logout ---


def test_signup_creates_user_org_and_session(client, db, signed_up):
    assert signed_up["org"]["role"] == "owner"
    assert signed_up["user"]["email"] == "owner@example.com"

    me = client.get("/api/auth/me").json()
    assert me["authenticated"] is True
    assert me["org"]["name"] == "Acme"
    # No ClickHouse connection yet -- the UI uses this to prompt for setup.
    assert me["connection"]["configured"] is False


def test_signup_rejects_duplicate_email(client, signed_up):
    resp = client.post(
        "/api/auth/signup", json={"email": "OWNER@example.com", "password": PASSWORD}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "email_taken"


def test_signup_enforces_minimum_password_length(client):
    resp = client.post("/api/auth/signup", json={"email": "a@b.com", "password": "short"})
    assert resp.status_code == 400


def test_signup_can_be_disabled(client, monkeypatch):
    from datatalk.config import get_settings

    monkeypatch.setenv("DATATALK_ALLOW_OPEN_SIGNUP", "false")
    get_settings.cache_clear()
    try:
        resp = client.post(
            "/api/auth/signup", json={"email": "x@y.com", "password": PASSWORD}
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "signup_disabled"
    finally:
        get_settings.cache_clear()


def test_login_succeeds_and_authorizes_api_calls(client, signed_up):
    client.post("/api/auth/logout")
    assert client.get("/api/reports").status_code == 401

    resp = client.post(
        "/api/auth/login", json={"email": "owner@example.com", "password": PASSWORD}
    )
    assert resp.status_code == 200
    assert resp.json()["org"]["name"] == "Acme"
    assert client.get("/api/reports").status_code == 200


def test_wrong_password_and_unknown_email_are_indistinguishable(client, signed_up):
    wrong = client.post(
        "/api/auth/login", json={"email": "owner@example.com", "password": "wrong-password"}
    )
    unknown = client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "invalid_credentials"}


def test_logout_revokes_the_session_server_side(client, db, signed_up):
    assert client.get("/api/reports").status_code == 200
    assert db.query(models.AuthSession).count() == 1

    assert client.post("/api/auth/logout").status_code == 204

    assert db.query(models.AuthSession).count() == 0, "session row must be deleted"
    assert client.get("/api/reports").status_code == 401


def test_login_issues_a_new_token_each_time(client, db, signed_up):
    """Never reuse a pre-auth token (session fixation)."""
    first = client.cookies.get("dt_session")
    client.post("/api/auth/logout")
    client.post(
        "/api/auth/login", json={"email": "owner@example.com", "password": PASSWORD}
    )
    assert client.cookies.get("dt_session") != first


def test_session_cookie_is_httponly_and_lax(client):
    resp = client.post(
        "/api/auth/signup", json={"email": "flags@example.com", "password": PASSWORD}
    )
    cookie_header = resp.headers.get("set-cookie", "").lower()
    assert "httponly" in cookie_header, "JS must never be able to read the session"
    assert "samesite=lax" in cookie_header, "withholds the cookie from cross-site XHR"
    # Secure defaults off: on plain-HTTP dev the browser would silently drop the
    # cookie, producing a login that 200s and then 401s on every request.
    assert "secure" not in cookie_header


def test_me_is_public_and_reports_unauthenticated(client):
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": False}


def test_expired_session_is_rejected(client, db, signed_up):
    from datetime import datetime, timedelta, timezone

    row = db.query(models.AuthSession).one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.flush()

    assert client.get("/api/reports").status_code == 401
    assert db.query(models.AuthSession).count() == 0, "expired rows are swept on sight"


# --- csrf ---


def test_foreign_origin_is_rejected_on_writes(client, signed_up):
    resp = client.post(
        "/api/feedback",
        json={"text": "hi"},
        headers={"Origin": "https://evil.test", "Host": "testserver"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "cross_origin_request"


def test_configured_frontend_origin_is_allowed(client, signed_up):
    """The Next.js frontend is a different origin and must still work."""
    resp = client.get(
        "/api/reports", headers={"Origin": "http://localhost:3000"}
    )
    assert resp.status_code == 200
