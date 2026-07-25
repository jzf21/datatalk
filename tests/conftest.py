"""Shared test fixtures.

The ``FakeOpenAI`` scripted client used to be copy-pasted into four agent test
files, and each of those files monkeypatched ``get_openai`` on every agent
module it touched. Now that agents resolve their clients through a
``TenantContext``, one fake context replaces all of that.
"""

from __future__ import annotations

import contextlib
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from datatalk.config import Settings, get_settings
from datatalk.context import TenantContext
from datatalk.db import models

# --- scripted OpenAI double ---------------------------------------------------


def fn_call(call_id: str, sql: str):
    """A tool call asking to run ``sql``."""
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name="run_sql", arguments=json.dumps({"sql": sql})),
    )


def message(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def response(msg):
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class FakeCompletions:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = 0

    def create(self, **kwargs):
        resp = self._scripted[self.calls]
        self.calls += 1
        return resp


class FakeEmbeddings:
    """Keyword -> direction embedder, so cosine assertions stay readable."""

    VOCAB = ["sla", "revenue", "bug", "account", "sprint"]

    def create(self, model=None, input=None):
        vectors = []
        for text in input or []:
            v = np.zeros(len(self.VOCAB), dtype=np.float32)
            for i, word in enumerate(self.VOCAB):
                if word in text.lower():
                    v[i] += 1.0
            if not v.any():
                v[0] = 0.01
            vectors.append(SimpleNamespace(embedding=v.tolist()))
        return SimpleNamespace(data=vectors)


class FakeOpenAI:
    def __init__(self, scripted=()):
        self.chat = SimpleNamespace(completions=FakeCompletions(scripted))
        self.embeddings = FakeEmbeddings()


# --- contexts -----------------------------------------------------------------


def make_settings(**overrides) -> Settings:
    base = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_MODEL": "test-model",
        "CLICKHOUSE_HOST": "clickhouse.test",
        "CLICKHOUSE_DATABASE": "testdb",
    }
    base.update(overrides)
    return Settings(**base)


def make_ctx(*, openai=None, clickhouse=None, settings=None, **kw) -> TenantContext:
    """A tenant context wired to fakes -- the single agent-test injection point."""
    return TenantContext.for_test(
        openai=openai if openai is not None else FakeOpenAI(),
        clickhouse=clickhouse,
        settings=settings or make_settings(),
        **kw,
    )


@pytest.fixture
def fake_openai():
    return FakeOpenAI()


@pytest.fixture
def ctx(fake_openai) -> TenantContext:
    """Default context with an embedding-capable fake OpenAI and no ClickHouse."""
    return make_ctx(openai=fake_openai)


@pytest.fixture(autouse=True)
def _clear_client_registries():
    """Stop cached clients leaking between tests."""
    from datatalk import clients

    clients.close_all()
    yield
    clients.close_all()


@pytest.fixture(autouse=True)
def _clear_schema_cache():
    from datatalk.db import introspect

    introspect._SCHEMA_CACHE.clear()
    yield
    introspect._SCHEMA_CACHE.clear()


# --- database -----------------------------------------------------------------
#
# Real Postgres, no SQLite fallback. JSONB, BYTEA, TIMESTAMPTZ, partial and
# functional indexes, and composite FKs to unique constraints are either absent
# or behave differently on SQLite -- and those constraints ARE the tenant
# isolation. Testing isolation against an engine that cannot express it would
# produce false confidence.

TEST_SECRET_KEY = Fernet.generate_key().decode()


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("DATATALK_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "DATATALK_TEST_DATABASE_URL is not set, so database tests are "
            "skipped. Start the bundled Postgres and point at the test DB:\n"
            "  docker compose up -d\n"
            "  export DATATALK_TEST_DATABASE_URL="
            "postgresql+psycopg://datatalk:datatalk@localhost:5433/datatalk_test"
        )
    if url == os.environ.get("DATABASE_URL"):
        pytest.fail(
            "DATATALK_TEST_DATABASE_URL must differ from DATABASE_URL -- the "
            "test suite truncates the database it runs against."
        )
    return url


@pytest.fixture(scope="session")
def engine(database_url):
    """Migrate the test database once per session, via the real migrations.

    `Base.metadata.create_all()` would be faster but would never exercise the
    migration, and drift would surface exactly in the hand-written partial
    indexes and composite FKs that enforce isolation.
    """
    os.environ["DATABASE_URL"] = database_url
    os.environ.setdefault("DATATALK_SECRET_KEY", TEST_SECRET_KEY)
    get_settings.cache_clear()

    from alembic import command

    from datatalk.db.session import get_engine, reset_engine
    from datatalk.scripts.db import alembic_config

    command.upgrade(alembic_config(database_url), "head")

    reset_engine()
    eng = get_engine()
    yield eng
    reset_engine()


@pytest.fixture
def db(engine) -> Session:
    """A session inside a transaction that is rolled back after each test.

    Nested via SAVEPOINT so code under test can commit (session_scope does)
    without escaping the outer rollback.
    """
    conn = engine.connect()
    outer = conn.begin()
    session = Session(
        bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        conn.close()


@pytest.fixture(autouse=True)
def _bind_session_scope(request, monkeypatch):
    """Make session_scope() reuse the test's transaction.

    Without this, code under test opens its own pooled connection and commits
    outside the rollback boundary, leaking rows between tests.
    """
    if "db" not in request.fixturenames:
        return
    session = request.getfixturevalue("db")

    @contextlib.contextmanager
    def _scope():
        yield session
        session.flush()  # surface constraint violations; the outer txn rolls back

    import datatalk.db.session as session_mod
    import datatalk.web.app as web_mod

    monkeypatch.setattr(session_mod, "session_scope", _scope)
    # web/app.py binds the name at import time (`from ... import session_scope`),
    # so patching only the source module would leave the streaming endpoints
    # using a real pooled session -- one that cannot see this transaction's org.
    monkeypatch.setattr(web_mod, "session_scope", _scope)


# --- tenants ------------------------------------------------------------------


def make_org(db: Session, name: str = "Acme") -> models.Org:
    org = models.Org(name=name, slug=f"{name.lower()}-{uuid4().hex[:8]}")
    db.add(org)
    db.flush()
    return org


def make_user(db: Session, org: models.Org, role: str = "owner") -> models.User:
    user = models.User(
        email=f"u-{uuid4().hex[:8]}@example.com", password_hash="not-a-real-hash"
    )
    db.add(user)
    db.flush()
    db.add(models.Membership(org_id=org.id, user_id=user.id, role=role))
    db.flush()
    return user


@pytest.fixture
def org_a(db):
    return make_org(db, "Acme")


@pytest.fixture
def org_b(db):
    return make_org(db, "Globex")


@pytest.fixture
def user_a(db, org_a):
    return make_user(db, org_a)


@pytest.fixture
def user_b(db, org_b):
    return make_user(db, org_b)


def store_for(db: Session, org, user=None, openai=None):
    """An org-scoped MemoryStore wired to a fake embedder."""
    from datatalk.memory.store import MemoryStore

    ctx = make_ctx(
        openai=openai or FakeOpenAI(),
        org_id=org.id,
        org_slug=org.slug,
        user_id=user.id if user is not None else None,
    )
    return MemoryStore(db, ctx=ctx)


@pytest.fixture
def store(db, org_a, user_a):
    return store_for(db, org_a, user_a)


@pytest.fixture
def other_store(db, org_b, user_b):
    """A second org's store -- the counterparty in every isolation test."""
    return store_for(db, org_b, user_b)


# --- authenticated HTTP client ------------------------------------------------

TEST_PASSWORD = "correct-horse-battery"


@pytest.fixture
def api_client(db):
    """TestClient sharing the test's rolled-back transaction."""
    from fastapi.testclient import TestClient

    from datatalk.auth import passwords
    from datatalk.web import app as web
    from datatalk.web.deps import get_db

    # argon2 at production parameters is ~80ms per login; a suite with dozens
    # of them would crawl.
    passwords.use_fast_params_for_tests()

    web.app.dependency_overrides[get_db] = lambda: db
    with TestClient(web.app) as client:
        yield client
    web.app.dependency_overrides.clear()


@pytest.fixture
def auth_client(api_client):
    """A client that has signed up, so it carries a live session cookie."""
    resp = api_client.post(
        "/api/auth/signup",
        json={
            "email": f"user-{uuid4().hex[:8]}@example.com",
            "password": TEST_PASSWORD,
            "org_name": "Test Org",
        },
    )
    assert resp.status_code == 201, resp.text
    return api_client
