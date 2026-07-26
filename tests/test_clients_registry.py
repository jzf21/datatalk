"""Per-source warehouse registry: keying, invalidation, and concurrent creation."""

from __future__ import annotations

import threading

import pytest

from datatalk import clients, warehouse
from datatalk.config import Settings
from datatalk.context import SourceRef, TenantContext
from datatalk.warehouse import WarehouseSpec


@pytest.fixture(autouse=True)
def _clear_registries():
    clients.close_all()
    yield
    clients.close_all()


def _spec(**over) -> WarehouseSpec:
    base = dict(
        type="clickhouse",
        host="ch.example",
        port=8123,
        username="reader",
        password="pw",
        database="analytics",
        secure=False,
    )
    base.update(over)
    return WarehouseSpec(**base)


def _settings(**over) -> Settings:
    base = {"OPENAI_API_KEY": "sk-test", "OPENAI_BASE_URL": ""}
    base.update(over)
    return Settings(**base)


def _ctx(*specs: tuple[str, WarehouseSpec], **kw) -> TenantContext:
    return TenantContext.from_sources(
        tuple(
            SourceRef.from_spec(spec, name=name, is_default=(i == 0))
            for i, (name, spec) in enumerate(specs)
        ),
        org_id=TenantContext.from_env().org_id,
        settings=_settings(),
        **kw,
    )


# --- fingerprinting ---


def test_same_connection_settings_share_a_fingerprint():
    assert warehouse.fingerprint(_spec()) == warehouse.fingerprint(_spec())


@pytest.mark.parametrize(
    "override",
    [
        {"host": "other.example"},
        {"port": 9000},
        {"username": "someone-else"},
        {"password": "rotated"},
        {"database": "other_db"},
        {"secure": True},
        {"sslmode": "require"},
        # Without this, a ClickHouse and a Postgres source on the same
        # host:port would collide onto one cached client.
        {"type": "postgres"},
    ],
)
def test_any_connection_change_changes_the_fingerprint(override):
    assert warehouse.fingerprint(_spec()) != warehouse.fingerprint(_spec(**override))


@pytest.mark.parametrize(
    "override",
    [
        {"introspect_databases": ("sales",)},
        {"introspect_exclude_patterns": ("backup",)},
        {"introspect_sample_rows": 10},
        {"introspect_max_tables": 5},
    ],
)
def test_introspection_settings_are_in_the_fingerprint(override):
    """Two orgs with identical credentials but different allowlists must not
    share a schema cache entry, or one sees the other's excluded tables."""
    assert warehouse.fingerprint(_spec()) != warehouse.fingerprint(_spec(**override))


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


def test_warehouse_is_cached_per_fingerprint(monkeypatch):
    built = []
    monkeypatch.setattr(
        clients, "create_warehouse", lambda s: built.append(s) or object()
    )

    spec = _spec()
    first = clients.warehouse_for(spec)
    second = clients.warehouse_for(spec)

    assert first is second
    assert len(built) == 1


def test_different_sources_get_different_warehouses(monkeypatch):
    monkeypatch.setattr(clients, "create_warehouse", lambda s: object())

    a = clients.warehouse_for(_spec(database="org_a"))
    b = clients.warehouse_for(_spec(database="org_b"))

    assert a is not b


def test_invalidate_forces_a_rebuild_without_closing(monkeypatch):
    """Invalidation must not close(): a worker thread started before the edit
    may still be mid-query against the old client."""

    class FakeClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(clients, "create_warehouse", lambda s: FakeClient())

    spec = _spec()
    fp = warehouse.fingerprint(spec)
    original = clients.warehouse_for(spec, fp)

    clients.invalidate_warehouse(fp)
    rebuilt = clients.warehouse_for(spec, fp)

    assert rebuilt is not original
    assert original.closed is False


def test_invalidating_one_source_leaves_others_cached(monkeypatch):
    monkeypatch.setattr(clients, "create_warehouse", lambda s: object())

    a, b = _spec(database="a"), _spec(database="b")
    wh_a, wh_b = clients.warehouse_for(a), clients.warehouse_for(b)

    clients.invalidate_warehouse(warehouse.fingerprint(a))

    assert clients.warehouse_for(a) is not wh_a
    assert clients.warehouse_for(b) is wh_b


def test_close_all_closes_and_clears(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(clients, "create_warehouse", lambda s: FakeClient())
    created = clients.warehouse_for(_spec())

    clients.close_all()

    assert created.closed is True
    assert clients._WH == {}


def test_concurrent_first_use_yields_one_shared_warehouse(monkeypatch):
    """N threads racing on a cold cache must all end up with the same object."""
    barrier = threading.Barrier(8)

    def slow_create(spec):
        barrier.wait(timeout=5)  # force maximum overlap
        return object()

    monkeypatch.setattr(clients, "create_warehouse", slow_create)

    spec = _spec()
    results: list[object] = []
    lock = threading.Lock()

    def worker():
        results.append(clients.warehouse_for(spec))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(results) == 8
    assert len(set(map(id, results))) == 1, "all threads must share one warehouse"


# --- TenantContext wiring ---


def test_context_resolves_warehouses_through_the_registry(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(clients, "create_warehouse", lambda s: sentinel)

    ctx = _ctx(("main", _spec()))

    assert ctx.warehouse() is sentinel
    assert ctx.warehouse("main") is sentinel


def test_each_source_resolves_to_its_own_warehouse(monkeypatch):
    monkeypatch.setattr(clients, "create_warehouse", lambda s: object())

    ctx = _ctx(
        ("events", _spec(database="events")),
        ("billing", _spec(type="postgres", port=5432, database="billing")),
    )

    assert ctx.warehouse("events") is not ctx.warehouse("billing")
    # No argument means the default, which is the first source.
    assert ctx.warehouse() is ctx.warehouse("events")


def test_unknown_source_names_what_does_exist(monkeypatch):
    """The message is fed back to the model, so it has to be actionable."""
    from datatalk.context import UnknownSourceError

    monkeypatch.setattr(clients, "create_warehouse", lambda s: object())
    ctx = _ctx(("events", _spec()), ("billing", _spec(database="b")))

    with pytest.raises(UnknownSourceError) as exc:
        ctx.warehouse("warehouse_of_dreams")

    assert "events" in str(exc.value) and "billing" in str(exc.value)


def test_a_connectionless_org_cannot_reach_any_warehouse(monkeypatch):
    """The env CLICKHOUSE_* values must be structurally unreachable here."""
    from datatalk.context import NoConnectionError

    def explode(_spec):
        raise AssertionError("no source is configured; nothing may be dialled")

    monkeypatch.setattr(clients, "create_warehouse", explode)
    ctx = _ctx()

    assert ctx.has_connection is False
    with pytest.raises(NoConnectionError):
        ctx.warehouse()


def test_overrides_bypass_the_registry(monkeypatch):
    def explode(_spec):
        raise AssertionError("registry must not be consulted when overridden")

    monkeypatch.setattr(clients, "create_warehouse", explode)

    fake_wh, fake_oa = object(), object()
    ctx = TenantContext.for_test(
        warehouses={"main": fake_wh}, openai=fake_oa, settings=_settings()
    )

    assert ctx.warehouse("main") is fake_wh
    assert ctx.openai is fake_oa


def test_the_context_fingerprint_covers_every_source():
    """It keys the combined catalog, so adding or editing a source must move it."""
    one = _ctx(("events", _spec()))
    two = _ctx(("events", _spec()), ("billing", _spec(database="b")))
    edited = _ctx(("events", _spec(password="rotated")))

    assert one.fingerprint != two.fingerprint
    assert one.fingerprint != edited.fingerprint
    assert one.fingerprint == _ctx(("events", _spec())).fingerprint


def test_context_is_frozen():
    """Frozen is what makes it safe to hand to a worker thread."""
    ctx = TenantContext.for_test(settings=_settings())
    with pytest.raises(Exception):
        ctx.org_id = None  # type: ignore[misc]
