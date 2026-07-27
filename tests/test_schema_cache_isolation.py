"""Regression tests for the cross-tenant schema-cache leak.

``_SCHEMA_CACHE`` used to be a dict with a single hardcoded ``"context"`` key
shared by every caller. Since the schema catalog embeds table names, column
names *and sample rows*, one org's data would surface in another org's prompts.
These tests pin the fix.

The cache is now keyed ``(org_id, source fingerprint)``, so with several sources
per org there is one entry per source and editing one must not invalidate the
others.
"""

from __future__ import annotations

import threading
from uuid import uuid4

import pytest

from datatalk.context import SourceRef, TenantContext
from datatalk.warehouse import WarehouseSpec, catalog
from tests.conftest import FakeOpenAI, FakeWarehouse, fake_table, make_settings


@pytest.fixture(autouse=True)
def _clear_cache():
    catalog._SCHEMA_CACHE.clear()
    yield
    catalog._SCHEMA_CACHE.clear()


def _spec(**overrides) -> WarehouseSpec:
    base = dict(
        type="clickhouse",
        host="clickhouse.test",
        port=8123,
        username="reader",
        password="pw",
        database="default",
    )
    base.update(overrides)
    return WarehouseSpec(**base)


def _ctx_for(org_id, warehouses: dict, specs: dict | None = None) -> TenantContext:
    """A context whose sources resolve to the given fakes.

    Sources are built from explicit specs rather than the synthesized ones, so
    fingerprint-sensitivity tests exercise the real hashing.
    """
    specs = specs or {}
    sources = tuple(
        SourceRef.from_spec(
            specs.get(name) or _spec(database=name),
            name=name,
            is_default=(i == 0),
        )
        for i, name in enumerate(warehouses)
    )
    return TenantContext.for_test(
        openai=FakeOpenAI(),
        warehouses=warehouses,
        sources=sources,
        settings=make_settings(),
        org_id=org_id,
    )


def _wh(table_name: str, sample: str) -> FakeWarehouse:
    return FakeWarehouse(
        tables=[
            fake_table(
                table_name,
                columns=("secret_col",),
                rows=[{"secret_col": sample}],
            )
        ]
    )


def test_two_orgs_never_share_a_schema_context():
    """The core leak: org B must not see org A's tables or sample rows."""
    org_a = _ctx_for(uuid4(), {"main": _wh("tbl_acme", "acme-row")})
    org_b = _ctx_for(uuid4(), {"main": _wh("tbl_globex", "globex-row")})

    context_a = catalog.build_catalog(org_a)
    context_b = catalog.build_catalog(org_b)

    assert "tbl_acme" in context_a
    assert "tbl_globex" in context_b
    # The leak, stated directly.
    assert "acme" not in context_b
    assert "globex" not in context_a
    assert len(catalog._SCHEMA_CACHE) == 2


def test_same_org_hits_the_cache():
    wh = _wh("t", "r")
    ctx = _ctx_for(uuid4(), {"main": wh})

    catalog.build_catalog(ctx)
    catalog.build_catalog(ctx)

    assert wh.introspections == 1, "second call must be served from cache"


def test_force_refresh_reintrospects():
    wh = _wh("t", "r")
    ctx = _ctx_for(uuid4(), {"main": wh})

    catalog.build_catalog(ctx)
    catalog.build_catalog(ctx, force_refresh=True)

    assert wh.introspections == 2


def test_same_org_different_introspection_settings_are_separate_entries():
    """Identical credentials but different allowlists must not share a cache
    entry, or one config sees tables the other deliberately excluded."""
    org_id = uuid4()
    broad = _ctx_for(org_id, {"main": _wh("shared_table", "r")})
    narrow = _ctx_for(
        org_id,
        {"main": _wh("shared_table", "r")},
        specs={"main": _spec(introspect_databases=("only_this_one",))},
    )

    catalog.build_catalog(broad)
    catalog.build_catalog(narrow)

    assert broad.fingerprint != narrow.fingerprint
    assert len(catalog._SCHEMA_CACHE) == 2


def test_same_org_different_table_scope_are_separate_entries():
    """A narrowed table selection is as much a schema change as a narrowed
    database one -- sharing an entry would serve tables the scope excludes."""
    org_id = uuid4()
    broad = _ctx_for(org_id, {"main": _wh("shared_table", "r")})
    narrow = _ctx_for(
        org_id,
        {"main": _wh("shared_table", "r")},
        specs={"main": _spec(introspect_tables=("db.only_this_table",))},
    )

    catalog.build_catalog(broad)
    catalog.build_catalog(narrow)

    assert broad.fingerprint != narrow.fingerprint
    assert len(catalog._SCHEMA_CACHE) == 2


def test_changing_credentials_invalidates_by_fingerprint():
    """Rotating a password must not serve the schema fetched with the old one."""
    org_id = uuid4()
    before = _ctx_for(
        org_id, {"main": _wh("tbl_old", "r")}, specs={"main": _spec(password="old")}
    )
    after = _ctx_for(
        org_id, {"main": _wh("tbl_new", "r")}, specs={"main": _spec(password="new")}
    )

    assert "tbl_old" in catalog.build_catalog(before)
    assert "tbl_new" in catalog.build_catalog(after)


def test_each_source_is_cached_independently():
    """An org's sources get one entry each, keyed by their own fingerprints."""
    ctx = _ctx_for(
        uuid4(),
        {"events": _wh("pageviews", "a"), "billing": _wh("invoices", "b")},
    )

    context = catalog.build_catalog(ctx)

    assert 'SOURCE "events" [clickhouse]' in context
    assert 'SOURCE "billing" [clickhouse]' in context
    assert "pageviews" in context and "invoices" in context
    assert len(catalog._SCHEMA_CACHE) == 2


def test_invalidate_schema_drops_only_that_org():
    org_a, org_b = uuid4(), uuid4()
    ctx_a = _ctx_for(org_a, {"main": _wh("a", "a")}, specs={"main": _spec(database="a")})
    ctx_b = _ctx_for(org_b, {"main": _wh("b", "b")}, specs={"main": _spec(database="b")})
    catalog.build_catalog(ctx_a)
    catalog.build_catalog(ctx_b)

    catalog.invalidate_schema(org_a)

    remaining = list(catalog._SCHEMA_CACHE)
    assert len(remaining) == 1
    assert remaining[0][0] == org_b


def test_expired_entries_are_reintrospected(monkeypatch):
    monkeypatch.setattr(catalog, "_SCHEMA_TTL", -1.0)  # everything is stale
    wh = _wh("t", "r")
    ctx = _ctx_for(uuid4(), {"main": wh})

    catalog.build_catalog(ctx)
    catalog.build_catalog(ctx)

    assert wh.introspections == 2


def test_concurrent_access_from_worker_threads_is_safe():
    """Agents call this from daemon threads; the cache must not corrupt."""
    contexts = [
        _ctx_for(
            uuid4(),
            {"main": _wh(f"tbl_db{i}", f"row{i}")},
            specs={"main": _spec(database=f"db{i}")},
        )
        for i in range(8)
    ]
    barrier = threading.Barrier(len(contexts))
    results: dict[int, str] = {}
    lock = threading.Lock()

    def worker(i, ctx):
        barrier.wait(timeout=5)
        context = catalog.build_catalog(ctx)
        with lock:
            results[i] = context

    threads = [
        threading.Thread(target=worker, args=(i, c)) for i, c in enumerate(contexts)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(results) == 8
    # Every org got its own schema, not a neighbour's.
    for i, context in results.items():
        assert f"tbl_db{i}" in context
    assert len(catalog._SCHEMA_CACHE) == 8
