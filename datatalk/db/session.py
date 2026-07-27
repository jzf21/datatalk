"""Engine and session management for the application Postgres.

Sync, not async, on purpose: every handler in ``web/app.py`` is ``def`` (FastAPI
runs those in a threadpool, so blocking DB calls are already correct), and the
agents run in bare ``threading.Thread`` workers where an ``AsyncSession`` has no
event loop to attach to.

**The session rule.** A ``Session`` is not thread-safe, and the streaming
endpoints save to the database from a *rotating* anyio threadpool thread minutes
after the request handler returned. Never hand a session across that boundary --
open a fresh short-lived one with :func:`session_scope`. See
``web/app.py``'s streaming endpoints.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from datatalk import jsonsafe
from datatalk.config import get_settings


class DatabaseNotConfiguredError(RuntimeError):
    """DATABASE_URL is unset or not a Postgres URL."""


def _json_dumps(obj: Any) -> str:
    """Serializer for every JSONB column.

    Materialized documents embed real warehouse cell values, which include
    things JSONB will not accept: NUL bytes (legal in a ClickHouse string) and
    NaN/Infinity (any aggregate can produce one). Both are handled in
    :mod:`datatalk.jsonsafe`, which the NDJSON wire uses too.

    Setting this on the engine means the convention holds automatically
    everywhere instead of being re-applied at each call site.
    """
    return jsonsafe.dumps(obj)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    url = settings.database_url
    if not url:
        raise DatabaseNotConfiguredError(
            "DATABASE_URL is not set. Start the bundled Postgres with "
            "`docker compose up -d`, then copy .env.example to .env."
        )
    if not url.startswith("postgresql"):
        raise DatabaseNotConfiguredError(
            f"DATABASE_URL must be a postgresql:// URL, got {url.split(':')[0]!r}."
        )
    return create_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        # Report generation runs for minutes, during which a pooled connection
        # can go stale behind a proxy; pre_ping turns that into a retry rather
        # than a mid-stream failure.
        pool_pre_ping=True,
        pool_recycle=1800,
        json_serializer=_json_dumps,
        json_deserializer=json.loads,
        echo=settings.db_echo,
        future=True,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    # expire_on_commit=False: attributes stay readable after commit, which the
    # store relies on when building its detached return dataclasses.
    return sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transaction. Commits on success, rolls back on error, always closes."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def db_session() -> Iterator[Session]:
    """FastAPI dependency.

    Yield-dependency teardown happens after the response body is fully
    consumed, which for a streaming endpoint would pin a pooled connection for
    the entire multi-minute generation — those endpoints therefore call
    ``RequestContext.release_db()`` as the first line of their stream
    generator, once every handler-body read is done. The teardown here then
    commits and closes an already-released session, which costs a microsecond
    connection checkout and nothing else.
    """
    with session_scope() as session:
        yield session


def reset_engine() -> None:
    """Drop the cached engine and sessionmaker (tests, config changes)."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
