"""CORS middleware: the Next.js frontend calls the API cross-origin."""

import importlib

from fastapi.testclient import TestClient

import datatalk.config as config_mod
import datatalk.web.app as web

ALLOWED = "http://localhost:3000"
DENIED = "https://evil.example"

_PREFLIGHT = {
    "Origin": ALLOWED,
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "content-type",
}


def test_preflight_from_allowed_origin_is_permitted():
    resp = TestClient(web.app).options("/api/report", headers=_PREFLIGHT)
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ALLOWED
    # JSON bodies are not a safelisted content type, so this header is required
    # or the preflight fails with a misleading bare "network error".
    assert "content-type" in resp.headers["access-control-allow-headers"].lower()
    assert "POST" in resp.headers["access-control-allow-methods"]


def test_preflight_from_unknown_origin_is_not_allowed():
    resp = TestClient(web.app).options(
        "/api/report", headers={**_PREFLIGHT, "Origin": DENIED}
    )
    assert "access-control-allow-origin" not in resp.headers


def test_error_responses_still_carry_cors_headers():
    # The frontend must be able to read {"detail": ...} off an error response.
    # /api/report is authenticated now, so an unauthenticated call is the
    # simplest error to provoke -- and the 401 the frontend must read to know
    # it should show the login screen.
    resp = TestClient(web.app).post(
        "/api/report", json={"request": "  "}, headers={"Origin": ALLOWED}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "not_authenticated"
    assert resp.headers["access-control-allow-origin"] == ALLOWED


def test_credentials_are_allowed_so_cookies_reach_a_cross_origin_frontend():
    """Cookie auth cannot work cross-origin without this. Safe only because
    allow_origins is an exact allowlist, never a wildcard."""
    resp = TestClient(web.app).options("/api/report", headers=_PREFLIGHT)
    assert resp.headers.get("access-control-allow-credentials") == "true"
    assert resp.headers["access-control-allow-origin"] == ALLOWED
    assert resp.headers["access-control-allow-origin"] != "*"


def test_empty_origin_setting_disables_cors(monkeypatch):
    monkeypatch.setenv("DATATALK_CORS_ORIGINS", "")
    config_mod.get_settings.cache_clear()
    try:
        reloaded = importlib.reload(web)
        resp = TestClient(reloaded.app).options("/api/report", headers=_PREFLIGHT)
        assert "access-control-allow-origin" not in resp.headers
    finally:
        monkeypatch.undo()
        config_mod.get_settings.cache_clear()
        importlib.reload(web)


def test_origin_list_strips_whitespace_and_trailing_slash():
    settings = config_mod.Settings(
        DATATALK_CORS_ORIGINS=" http://a.test/ , http://b.test ,, "
    )
    assert settings.cors_origin_list == ["http://a.test", "http://b.test"]
