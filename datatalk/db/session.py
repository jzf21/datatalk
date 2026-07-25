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

from datatalk.config import get_settings


class DatabaseNotConfiguredError(RuntimeError):
    """DATABASE_URL is unset or not a Postgres URL."""


def _scrub_nuls(obj: Any) -> Any:
    """Strip NUL bytes, which are legal in ClickHouse strings but not in JSONB.

    Materialized documents embed real ClickHouse cell values. SQLite TEXT
    accepted ``\\u0000``; Postgres JSONB rejects it outright.
    """
    if isinstance(obj, str):
        return obj.replace("\x00", "")
    if isinstance(obj, dict):
        return {_scrub_nuls(k): _scrub_nuls(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub_nuls(v) for v in obj]
    return obj


def _json_dumps(obj: Any) -> str:
    """Serializer for every JSONB column.

    ``default=str`` is the project-wide convention (ClickHouse rows carry
    datetimes and Decimals). Setting it on the engine means the convention holds
    automatically everywhere instead of being re-applied at each call site.
    """
    return json.dumps(_scrub_nuls(obj), default=str)


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

    Only for short handlers. Do NOT use for the streaming endpoints: yield-
    dependency teardown happens after the response body is fully consumed, so
    this would pin a pooled connection for the entire multi-minute generation.
    """
    with session_scope() as session:
        yield session


def reset_engine() -> None:
    """Drop the cached engine and sessionmaker (tests, config changes)."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
