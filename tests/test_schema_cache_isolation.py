"""Regression tests for the cross-tenant schema-cache leak.

``_SCHEMA_CACHE`` used to be a dict with a single hardcoded ``"context"`` key
shared by every caller. Since the schema context embeds table names, column
names *and three real sample rows per table*, one org's data would surface in
another org's prompts. These tests pin the fix.
"""

from __future__ import annotations

import threading
from uuid import uuid4

import pytest

from datatalk.db import introspect
from tests.conftest import make_ctx, make_settings


@pytest.fixture(autouse=True)
def _clear_cache():
    introspect._SCHEMA_CACHE.clear()
    yield
    introspect._SCHEMA_CACHE.clear()


def _ctx_for(org_id, **setting_overrides):
    # A fake ClickHouse client: get_schema_context passes ctx.clickhouse into
    # introspect(), so resolving it must not open a real connection.
    return make_ctx(
        org_id=org_id,
        clickhouse=object(),
        settings=make_settings(**setting_overrides),
    )


def _stub_introspect(monkeypatch, table_for):
    """Make introspect() return a distinct table name per ClickHouse database."""

    def fake(client, settings, *, with_samples=True):
        return [
            introspect.Table(
                database=settings.clickhouse_database,
                name=table_for(settings),
                columns=[introspect.Column(name="secret_col", type="String")],
                sample_rows=[{"secret_col": f"{settings.clickhouse_database}-row"}],
            )
        ]

    monkeypatch.setattr(introspect, "introspect", fake)


def test_two_orgs_never_share_a_schema_context(monkeypatch):
    """The core leak: org B must not see org A's tables or sample rows."""
    _stub_introspect(monkeypatch, lambda s: f"tbl_{s.clickhouse_database}")

    org_a = _ctx_for(uuid4(), CLICKHOUSE_DATABASE="acme")
    org_b = _ctx_for(uuid4(), CLICKHOUSE_DATABASE="globex")

    context_a = introspect.get_schema_context(org_a)
    context_b = introspect.get_schema_context(org_b)

    assert "tbl_acme" in context_a and "acme-row" in context_a
    assert "tbl_globex" in context_b and "globex-row" in context_b
    # The leak, stated directly.
    assert "acme" not in context_b
    assert "globex" not in context_a
    assert len(introspect._SCHEMA_CACHE) == 2


def test_same_org_hits_the_cache(monkeypatch):
    calls = []

    def fake(client, settings, *, with_samples=True):
        calls.append(settings.clickhouse_database)
        return []

    monkeypatch.setattr(introspect, "introspect", fake)
    ctx = _ctx_for(uuid4())

    introspect.get_schema_context(ctx)
    introspect.get_schema_context(ctx)

    assert len(calls) == 1, "second call must be served from cache"


def test_force_refresh_reintrospects(monkeypatch):
    calls = []
    monkeypatch.setattr(
        introspect,
        "introspect",
        lambda c, s, **kw: calls.append(1) or [],
    )
    ctx = _ctx_for(uuid4())

    introspect.get_schema_context(ctx)
    introspect.get_schema_context(ctx, force_refresh=True)

    assert len(calls) == 2


def test_same_org_different_introspection_settings_are_separate_entries(monkeypatch):
    """Identical credentials but different allowlists must not share a cache
    entry, or one config sees tables the other deliberately excluded."""
    _stub_introspect(monkeypatch, lambda s: "shared_table")

    org_id = uuid4()
    broad = _ctx_for(org_id)
    narrow = _ctx_for(org_id, INTROSPECT_DATABASES="only_this_one")

    introspect.get_schema_context(broad)
    introspect.get_schema_context(narrow)

    assert broad.fingerprint != narrow.fingerprint
    assert len(introspect._SCHEMA_CACHE) == 2


def test_changing_credentials_invalidates_by_fingerprint(monkeypatch):
    """Rotating a password must not serve the schema fetched with the old one."""
    _stub_introspect(monkeypatch, lambda s: f"tbl_{s.clickhouse_password}")

    org_id = uuid4()
    before = _ctx_for(org_id, CLICKHOUSE_PASSWORD="old")
    after = _ctx_for(org_id, CLICKHOUSE_PASSWORD="new")

    assert "tbl_old" in introspect.get_schema_context(before)
    assert "tbl_new" in introspect.get_schema_context(after)


def test_invalidate_schema_drops_only_that_org(monkeypatch):
    _stub_introspect(monkeypatch, lambda s: "t")

    org_a, org_b = uuid4(), uuid4()
    ctx_a = _ctx_for(org_a, CLICKHOUSE_DATABASE="a")
    ctx_b = _ctx_for(org_b, CLICKHOUSE_DATABASE="b")
    introspect.get_schema_context(ctx_a)
    introspect.get_schema_context(ctx_b)

    introspect.invalidate_schema(org_a)

    remaining = list(introspect._SCHEMA_CACHE)
    assert len(remaining) == 1
    assert remaining[0][0] == org_b


def test_expired_entries_are_reintrospected(monkeypatch):
    calls = []
    monkeypatch.setattr(
        introspect, "introspect", lambda c, s, **kw: calls.append(1) or []
    )
    monkeypatch.setattr(introspect, "_SCHEMA_TTL", -1.0)  # everything is stale
    ctx = _ctx_for(uuid4())

    introspect.get_schema_context(ctx)
    introspect.get_schema_context(ctx)

    assert len(calls) == 2


def test_concurrent_access_from_worker_threads_is_safe(monkeypatch):
    """Agents call this from daemon threads; the cache must not corrupt."""
    _stub_introspect(monkeypatch, lambda s: f"tbl_{s.clickhouse_database}")

    contexts = [_ctx_for(uuid4(), CLICKHOUSE_DATABASE=f"db{i}") for i in range(8)]
    barrier = threading.Barrier(len(contexts))
    results: dict[int, str] = {}
    lock = threading.Lock()

    def worker(i, ctx):
        barrier.wait(timeout=5)
        context = introspect.get_schema_context(ctx)
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
    assert len(introspect._SCHEMA_CACHE) == 8
