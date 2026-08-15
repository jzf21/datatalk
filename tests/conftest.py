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

# Before any Settings is built (get_settings caches). A developer's .env holds
# real Langfuse credentials, and the app lifespan configures tracing from it --
# so without this every TestClient would ship the suite's synthetic traces to a
# real project and block its teardown on a network flush. An environment
# variable, not a fixture: it must beat the .env file at settings-load time, and
# it must hold for tests that never opt in.
os.environ["LANGFUSE_TRACING_ENABLED"] = "false"

from datatalk.config import Settings, get_settings  # noqa: E402
from datatalk.context import TenantContext  # noqa: E402
from datatalk.db import models  # noqa: E402

# --- scripted OpenAI double ---------------------------------------------------


def fn_call(call_id: str, sql: str, source: str = "main"):
    """A tool call asking to run ``sql`` against ``source``."""
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name="run_sql", arguments=json.dumps({"source": source, "sql": sql})
        ),
    )


def describe_call(call_id: str, table: str, source: str = "main"):
    """A tool call asking for one table's full detail."""
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name="describe_source",
            arguments=json.dumps({"source": source, "table": table}),
        ),
    )


def message(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def response(msg, finish_reason="stop"):
    """A chat completion. ``finish_reason="length"`` is a truncated reply.

    Truncation is indistinguishable from malformed JSON at the parse layer, so
    it is the one signal that tells the dashboard author a retry is worth making
    shorter rather than merely different.
    """
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason=finish_reason)]
    )


class FakeCompletions:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = 0
        # Every create() kwargs dict, so a test can assert WHICH model a call
        # used. Without this, an agent silently falling back to the wrong model
        # is indistinguishable from one using the right one.
        self.kwargs: list[dict] = []

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        resp = self._scripted[self.calls]
        self.calls += 1
        return resp

    @property
    def models_used(self) -> list[str]:
        return [k.get("model") for k in self.kwargs]


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


# --- warehouses ----------------------------------------------------------------


class FakeWarehouse:
    """A stand-in implementing the Warehouse protocol.

    Records every statement it was asked to run, so a multi-source test can
    assert not just *that* a query ran but *which warehouse* it reached -- the
    thing that actually goes wrong when source routing breaks.
    """

    def __init__(
        self,
        *,
        dialect=None,
        tables=(),
        columns=("n",),
        rows=((1,),),
        fail=None,
        version="1.0-fake",
        database="fake",
    ):
        from datatalk.warehouse.clickhouse import CLICKHOUSE_DIALECT

        self.dialect = dialect or CLICKHOUSE_DIALECT
        self.spec = None
        self._tables = list(tables)
        self._columns = list(columns)
        self._rows = [list(r) for r in rows]
        # An exception instance to raise from every call, for dead-source tests.
        self.fail = fail
        self.version = version
        self.database = database
        self.queries: list[str] = []
        # Recorded alongside the SQL, one entry per query. The filter tests
        # assert that a value reached the driver as a *parameter* and never
        # appeared in the statement text, which is only checkable if both
        # halves are kept.
        self.parameter_sets: list[dict | None] = []
        self.introspections = 0
        self.closed = False

    def set_rows(self, columns, rows):
        """Change what the next query returns, for refresh tests."""
        self._columns = list(columns)
        self._rows = [list(r) for r in rows]

    def _maybe_fail(self):
        if self.fail is not None:
            raise self.fail

    def ping(self):
        self._maybe_fail()
        return {"version": self.version, "database": self.database}

    def query(self, sql, *, timeout_s, max_rows, parameters=None):
        from datatalk.warehouse.base import QueryResult

        self._maybe_fail()
        self.queries.append(sql)
        self.parameter_sets.append(dict(parameters) if parameters else None)
        rows = self._rows[:max_rows]
        return QueryResult(
            columns=list(self._columns),
            rows=[list(r) for r in rows],
            row_count=len(rows),
            truncated=len(self._rows) > max_rows,
            sql=sql,
        )

    def introspect(self, *, with_samples=True):
        self._maybe_fail()
        self.introspections += 1
        return list(self._tables)

    def close(self):
        self.closed = True


def fake_table(name, *, database="db", columns=("id", "value"), rows=None):
    from datatalk.warehouse.base import Column, Table

    return Table(
        database=database,
        name=name,
        columns=[Column(name=c, type="String") for c in columns],
        sample_rows=rows or [],
    )


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


def make_ctx(*, openai=None, warehouses=None, settings=None, **kw) -> TenantContext:
    """A tenant context wired to fakes -- the single agent-test injection point.

    ``warehouses`` maps source name to a stand-in. It defaults to a single
    source called "main", which is what a normally configured org looks like;
    pass several keys to exercise multi-source routing, or ``{}`` for the
    connectionless case.
    """
    if warehouses is None:
        warehouses = {"main": FakeWarehouse()}
    return TenantContext.for_test(
        openai=openai if openai is not None else FakeOpenAI(),
        warehouses=warehouses,
        settings=settings or make_settings(),
        **kw,
    )


@pytest.fixture
def fake_openai():
    return FakeOpenAI()


@pytest.fixture
def ctx(fake_openai) -> TenantContext:
    """Default context: an embedding-capable fake OpenAI and one fake source."""
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
    from datatalk.warehouse import catalog

    catalog._SCHEMA_CACHE.clear()
    yield
    catalog._SCHEMA_CACHE.clear()


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


def signup(api_client, *, org_name="Test Org"):
    """Sign up a fresh user+org on ``api_client`` and return the org id."""
    resp = api_client.post(
        "/api/auth/signup",
        json={
            "email": f"user-{uuid4().hex[:8]}@example.com",
            "password": TEST_PASSWORD,
            "org_name": org_name,
        },
    )
    assert resp.status_code == 201, resp.text
    from uuid import UUID

    return UUID(api_client.get("/api/auth/me").json()["org"]["id"])


def give_connection(
    db,
    org_id,
    *,
    name="default",
    type="clickhouse",
    host="clickhouse.test",
    database="default",
    is_default=True,
    description="",
):
    """Attach a data source so the org is fully configured.

    The host is deliberately unreachable: the endpoints these fixtures cover
    never dial it, and a source that resolved would make the tests depend on a
    live warehouse.
    """
    from datatalk.auth import orgs as orgs_svc

    if is_default:
        # One default per org is a partial unique index, so demote first.
        for other in orgs_svc.list_connections(db, org_id):
            if other.is_default:
                other.is_default = False
        db.flush()
    conn = models.OrgWarehouseConnection(
        org_id=org_id,
        name=name,
        type=type,
        description=description,
        host=host,
        port=8443 if type == "clickhouse" else 5432,
        username="reader",
        password="secret",
        database=database,
        secure=True,
        is_default=is_default,
    )
    db.add(conn)
    db.flush()
    return conn


@pytest.fixture
def connectionless_client(api_client):
    """Signed up, but with no data source -- the 409 counterparty.

    This is a brand-new org's real state, so it is what the ``no_connection``
    tests exercise.
    """
    api_client.org_id = signup(api_client)
    return api_client


@pytest.fixture
def ctx_warehouse(monkeypatch):
    """The warehouse every source in a web test resolves to.

    Patched at the client registry rather than via
    ``TenantContext.warehouse_overrides``, so the request builds its tenant
    context exactly as a real one does -- name resolution, fingerprinting and
    all -- and only the final dial is faked. Tests that need a source to be
    unreachable set ``.fail``; tests that need different numbers on a second
    read call ``.set_rows``.
    """
    from datatalk import clients

    warehouse = FakeWarehouse(columns=["metric", "current"], rows=[["rev", 120]])
    monkeypatch.setattr(clients, "create_warehouse", lambda spec: warehouse)
    clients.close_all()  # drop anything a previous test cached under this fp
    return warehouse


@pytest.fixture
def auth_client(api_client, db):
    """A signed-up client whose org has one data source configured.

    The source matters: endpoints that reach a warehouse 409 without one, and
    this fixture stands in for a normal, fully set-up org. Tests that want the
    unconfigured case use ``connectionless_client``.
    """
    org_id = signup(api_client)
    give_connection(db, org_id)
    api_client.org_id = org_id
    return api_client
