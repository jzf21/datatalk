"""Per-org client registry: keying, invalidation, and concurrent creation."""

from __future__ import annotations

import threading

import pytest

from datatalk import clients
from datatalk.config import Settings
from datatalk.context import TenantContext


@pytest.fixture(autouse=True)
def _clear_registries():
    clients.close_all()
    yield
    clients.close_all()


def _settings(**over) -> Settings:
    base = {
        "CLICKHOUSE_HOST": "ch.example",
        "CLICKHOUSE_PORT": 8123,
        "CLICKHOUSE_USER": "reader",
        "CLICKHOUSE_PASSWORD": "pw",
        "CLICKHOUSE_DATABASE": "analytics",
        "CLICKHOUSE_SECURE": False,
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "",
    }
    base.update(over)
    return Settings(**base)


# --- fingerprinting ---


def test_same_connection_settings_share_a_fingerprint():
    assert clients.fingerprint(_settings()) == clients.fingerprint(_settings())


@pytest.mark.parametrize(
    "override",
    [
        {"CLICKHOUSE_HOST": "other.example"},
        {"CLICKHOUSE_PORT": 9000},
        {"CLICKHOUSE_USER": "someone-else"},
        {"CLICKHOUSE_PASSWORD": "rotated"},
        {"CLICKHOUSE_DATABASE": "other_db"},
        {"CLICKHOUSE_SECURE": True},
    ],
)
def test_any_connection_change_changes_the_fingerprint(override):
    assert clients.fingerprint(_settings()) != clients.fingerprint(_settings(**override))


@pytest.mark.parametrize(
    "override",
    [
        {"INTROSPECT_DATABASES": "sales"},
        {"INTROSPECT_EXCLUDE_TABLE_PATTERNS": "backup"},
        {"INTROSPECT_SAMPLE_ROWS": 10},
        {"INTROSPECT_MAX_TABLES": 5},
    ],
)
def test_introspection_settings_are_in_the_fingerprint(override):
    """Two orgs with identical credentials but different allowlists must not
    share a schema cache entry, or one sees the other's excluded tables."""
    assert clients.fingerprint(_settings()) != clients.fingerprint(_settings(**override))


def test_openai_fingerprint_tracks_key_and_base_url():
    a = _settings()
    assert clients.openai_fingerprint(a) == clients.openai_fingerprint(_settings())
    assert clients.openai_fingerprint(a) != clients.openai_fingerprint(
        _settings(OPENAI_API_KEY="sk-other")
    )
    assert clients.openai_fingerprint(a) != clients.openai_fingerprint(
        _settings(OPENAI_BASE_URL="https://compat.example/v1/")
    )


# --- caching + invalidation ---


def test_client_is_cached_per_fingerprint(monkeypatch):
    built = []
    monkeypatch.setattr(clients, "create_client", lambda s: built.append(s) or object())

    s = _settings()
    first = clients.clickhouse_for(s)
    second = clients.clickhouse_for(s)

    assert first is second
    assert len(built) == 1


def test_different_orgs_get_different_clients(monkeypatch):
    monkeypatch.setattr(clients, "create_client", lambda s: object())

    org_a = clients.clickhouse_for(_settings(CLICKHOUSE_DATABASE="org_a"))
    org_b = clients.clickhouse_for(_settings(CLICKHOUSE_DATABASE="org_b"))

    assert org_a is not org_b


def test_invalidate_forces_a_rebuild_without_closing(monkeypatch):
    """Invalidation must not close(): a worker thread started before the edit
    may still be mid-query against the old client."""

    class FakeClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(clients, "create_client", lambda s: FakeClient())

    s = _settings()
    fp = clients.fingerprint(s)
    original = clients.clickhouse_for(s, fp)

    clients.invalidate_clickhouse(fp)
    rebuilt = clients.clickhouse_for(s, fp)

    assert rebuilt is not original
    assert original.closed is False


def test_invalidating_one_org_leaves_others_cached(monkeypatch):
    monkeypatch.setattr(clients, "create_client", lambda s: object())

    a, b = _settings(CLICKHOUSE_DATABASE="a"), _settings(CLICKHOUSE_DATABASE="b")
    client_a, client_b = clients.clickhouse_for(a), clients.clickhouse_for(b)

    clients.invalidate_clickhouse(clients.fingerprint(a))

    assert clients.clickhouse_for(a) is not client_a
    assert clients.clickhouse_for(b) is client_b


def test_close_all_closes_and_clears(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(clients, "create_client", lambda s: FakeClient())
    created = clients.clickhouse_for(_settings())

    clients.close_all()

    assert created.closed is True
    assert clients._CH == {}


def test_concurrent_first_use_yields_one_shared_client(monkeypatch):
    """N threads racing on a cold cache must all end up with the same object."""
    barrier = threading.Barrier(8)

    def slow_create(settings):
        barrier.wait(timeout=5)  # force maximum overlap
        return object()

    monkeypatch.setattr(clients, "create_client", slow_create)

    s = _settings()
    results: list[object] = []
    lock = threading.Lock()

    def worker():
        client = clients.clickhouse_for(s)
        with lock:
            results.append(client)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(results) == 8
    assert len(set(map(id, results))) == 1, "all threads must share one client"


# --- TenantContext wiring ---


def test_context_resolves_clients_through_the_registry(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(clients, "create_client", lambda s: sentinel)

    ctx = TenantContext.from_settings(_settings(), org_id=TenantContext.from_env().org_id)

    assert ctx.clickhouse is sentinel
    assert ctx.fingerprint == clients.fingerprint(ctx.settings)


def test_overrides_bypass_the_registry(monkeypatch):
    def explode(_settings):
        raise AssertionError("registry must not be consulted when overridden")

    monkeypatch.setattr(clients, "create_client", explode)

    fake_ch, fake_oa = object(), object()
    ctx = TenantContext.for_test(clickhouse=fake_ch, openai=fake_oa, settings=_settings())

    assert ctx.clickhouse is fake_ch
    assert ctx.openai is fake_oa


def test_with_settings_refingerprints():
    ctx = TenantContext.for_test(settings=_settings())
    moved = ctx.with_settings(_settings(CLICKHOUSE_DATABASE="elsewhere"))

    assert moved.fingerprint != ctx.fingerprint
    assert moved.org_id == ctx.org_id


def test_context_is_frozen():
    """Frozen is what makes it safe to hand to a worker thread."""
    ctx = TenantContext.for_test(settings=_settings())
    with pytest.raises(Exception):
        ctx.org_id = None  # type: ignore[misc]
