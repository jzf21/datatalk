"""The sync store: where synced sources live, and who may read each one.

A separate Postgres database (``DATATALK_SYNC_DATABASE_URL``), never the app
database. Each synced source gets:

* its own **schema** (``jira_<hex12>``), owned by the store's admin role, and
* its own **login role** (``dt_src_<hex12>``) that can ``CONNECT``, use that
  one schema, and ``SELECT`` from it -- nothing else. It is created with
  ``default_transaction_read_only`` on, and the adapter opens every session
  ``read_only`` besides.

That makes tenant isolation a property the *server* enforces. The agent reaches
a Jira source with that role's credentials, so a model that types another
org's schema name gets ``permission denied`` rather than rows -- regardless of
whether any code of ours was right. It is the same posture as the Postgres
adapter's read-only session: the check that does not depend on our parser.

What the role *can* still see is ``pg_catalog``: the names of other schemas
exist there for every Postgres user, and cannot be revoked. Those names are
connection-id prefixes, not org names, and introspection already filters on
``has_schema_privilege``, so they never reach a prompt.

Identifiers are built only by :func:`names_for` from a UUID, checked again by
:func:`_check_names`, and constrained by CHECKs on ``source_sync_state``; DDL
quotes them with :class:`psycopg.sql.Identifier` regardless.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg import sql as pgsql
from sqlalchemy.engine import make_url

from datatalk.config import Settings, get_settings

SCHEMA_PREFIX = "jira_"
ROLE_PREFIX = "dt_src_"
_SCHEMA_RE = re.compile(r"^jira_[0-9a-f]{12}$")
_ROLE_RE = re.compile(r"^dt_src_[0-9a-f]{12}$")
_CONNECT_TIMEOUT_S = 10
# Per role. The adapter pool holds at most 4 per process; this leaves room for
# a few workers without letting one source exhaust the server.
_ROLE_CONNECTION_LIMIT = 16

# Where a source whose store is missing points its reader. `.invalid` is
# reserved (RFC 2606) and never resolves, so the connect fails fast and the
# catalog renders the source UNAVAILABLE -- rather than an empty host, which
# libpq would treat as "the local socket".
UNREACHABLE_HOST = "sync-store-unconfigured.invalid"


class SyncStoreNotConfiguredError(RuntimeError):
    """``DATATALK_SYNC_DATABASE_URL`` is unset. There is deliberately no fallback."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or "Synced sources need DATATALK_SYNC_DATABASE_URL (a separate "
            "Postgres database). See docs/jira.md."
        )


class SyncInProgressError(RuntimeError):
    """Another sync of the same source holds its advisory lock."""


@dataclass(frozen=True)
class Endpoint:
    """How a reader reaches the store. No credentials: those are per source."""

    host: str
    port: int
    database: str
    sslmode: str | None


def names_for(connection_id: UUID) -> tuple[str, str]:
    """``(schema, role)`` for a source. Deterministic, so a lost state row can
    still be garbage-collected by name."""
    h = connection_id.hex[:12]
    return f"{SCHEMA_PREFIX}{h}", f"{ROLE_PREFIX}{h}"


def new_role_password() -> str:
    return secrets.token_urlsafe(32)


def _check_names(schema: str, role: str | None = None) -> None:
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"not a sync-store schema name: {schema!r}")
    if role is not None and not _ROLE_RE.match(role):
        raise ValueError(f"not a sync-store role name: {role!r}")


def _same_database(a: str, b: str) -> bool:
    ua, ub = make_url(a), make_url(b)
    return (
        (ua.host or "localhost", int(ua.port or 5432), ua.database)
        == (ub.host or "localhost", int(ub.port or 5432), ub.database)
    )


def _url(settings: Settings | None) -> str:
    """The store's URL -- refusing one that names the app database.

    Every isolation argument above assumes the store holds nothing but synced
    sources. Pointed at the app database, provisioning would revoke PUBLIC
    grants there and each source role would sit beside ``users`` and
    ``auth_sessions``; that is a misconfiguration to refuse, not to run with.
    """
    s = settings or get_settings()
    url = s.sync_database_url
    if not url:
        raise SyncStoreNotConfiguredError()
    if s.database_url and _same_database(url, s.database_url):
        raise SyncStoreNotConfiguredError(
            "DATATALK_SYNC_DATABASE_URL names the app database (DATABASE_URL). "
            "The sync store must be a separate database; see docs/jira.md."
        )
    return url


def is_configured(settings: Settings | None = None) -> bool:
    try:
        _url(settings)
    except SyncStoreNotConfiguredError:
        return False
    return True


def endpoint(settings: Settings | None = None) -> Endpoint:
    """Where readers connect, parsed from the admin URL minus its credentials."""
    u = make_url(_url(settings))
    return Endpoint(
        host=u.host or "localhost",
        port=int(u.port or 5432),
        database=u.database or "",
        sslmode=(u.query or {}).get("sslmode") or None,
    )


def _admin_conninfo(settings: Settings | None) -> dict:
    u = make_url(_url(settings))
    params = {
        "host": u.host or "localhost",
        "port": int(u.port or 5432),
        "user": u.username or "",
        "password": u.password or "",
        "dbname": u.database or "",
        "connect_timeout": _CONNECT_TIMEOUT_S,
    }
    sslmode = (u.query or {}).get("sslmode")
    if sslmode:
        params["sslmode"] = sslmode
    return params


@contextmanager
def admin_connection(settings: Settings | None = None) -> Iterator[psycopg.Connection]:
    """An autocommit connection as the store's owner. Callers open their own
    transactions (``with conn.transaction():``) where atomicity matters."""
    conn = psycopg.connect(**_admin_conninfo(settings), autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


def harden(conn: psycopg.Connection) -> None:
    """Close the defaults Postgres grants to PUBLIC. Idempotent.

    Without this, every source role could CONNECT (fine) *and* create objects
    in ``public`` or temp tables -- a place to write, which a read-only source
    must not have.
    """
    db = conn.info.dbname
    conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    conn.execute(
        pgsql.SQL("REVOKE CONNECT, TEMPORARY ON DATABASE {} FROM PUBLIC").format(
            pgsql.Identifier(db)
        )
    )


def provision(
    conn: psycopg.Connection, schema: str, role: str, password: str
) -> None:
    """Create (or repair) a source's schema and reader role. Idempotent.

    Safe to re-run on an existing source: the password is reset to ``password``
    and every grant is reasserted, which is also how a drifted role is fixed.
    """
    _check_names(schema, role)
    ident_role = pgsql.Identifier(role)
    ident_schema = pgsql.Identifier(schema)
    db = pgsql.Identifier(conn.info.dbname)

    with conn.transaction():
        harden(conn)
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(
            pgsql.SQL(
                verb + " ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT {}"
            ).format(
                ident_role,
                pgsql.Literal(password),
                pgsql.Literal(_ROLE_CONNECTION_LIMIT),
            )
        )
        conn.execute(
            pgsql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(
                ident_role
            )
        )
        conn.execute(
            pgsql.SQL("ALTER ROLE {} SET search_path = {}").format(
                ident_role, ident_schema
            )
        )
        conn.execute(pgsql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(ident_schema))
        conn.execute(
            pgsql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(db, ident_role)
        )
        conn.execute(
            pgsql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(ident_schema, ident_role)
        )
        conn.execute(
            pgsql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                ident_schema, ident_role
            )
        )
        # Tables the sync creates later (first sync, or a schema-version
        # rebuild) inherit the grant without a second provisioning pass.
        conn.execute(
            pgsql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}"
            ).format(ident_schema, ident_role)
        )


def drop(conn: psycopg.Connection, schema: str, role: str) -> None:
    """Remove a source's schema and role. Idempotent; missing is fine."""
    _check_names(schema, role)
    ident_role = pgsql.Identifier(role)
    with conn.transaction():
        conn.execute(
            pgsql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                pgsql.Identifier(schema)
            )
        )
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone()
        if exists:
            # The schema's default privileges went with the schema; the
            # database grant is the one dependency left, and DROP ROLE refuses
            # while it exists. Revoked explicitly rather than DROP OWNED BY,
            # which needs the role's own privileges -- a non-superuser admin
            # holding only CREATEROLE does not have them.
            conn.execute(
                pgsql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(
                    pgsql.Identifier(conn.info.dbname), ident_role
                )
            )
            conn.execute(pgsql.SQL("DROP ROLE {}").format(ident_role))


def managed_schemas(conn: psycopg.Connection) -> list[str]:
    """Every schema in the store that this module could have created."""
    rows = conn.execute(
        "SELECT nspname FROM pg_namespace WHERE left(nspname, 5) = 'jira_' "
        "ORDER BY nspname"
    ).fetchall()
    return [r[0] for r in rows if _SCHEMA_RE.match(r[0])]


def role_for_schema(schema: str) -> str:
    _check_names(schema)
    return ROLE_PREFIX + schema[len(SCHEMA_PREFIX):]


@contextmanager
def source_lock(conn: psycopg.Connection, schema: str) -> Iterator[None]:
    """Hold the per-source sync lock for the duration of the block.

    A session-level advisory lock in the store, not a per-process flag: the UI
    button, a second uvicorn worker and a cron ``datatalk-sync`` must all see
    the same lock. Try-once rather than wait -- a second sync of the same
    source would only redo the first one's work.
    """
    _check_names(schema)
    got = conn.execute(
        "SELECT pg_try_advisory_lock(hashtext(%s))", (schema,)
    ).fetchone()[0]
    if not got:
        raise SyncInProgressError(schema)
    try:
        yield
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (schema,))
        except Exception:  # noqa: BLE001 - closing the session releases it anyway
            pass
