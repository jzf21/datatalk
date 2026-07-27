"""Per-source warehouse and OpenAI client registries.

Replaces the process-global ``lru_cache(maxsize=1)`` singletons that used to
live in :mod:`datatalk.db.clickhouse` and :mod:`datatalk.llm.client`. Those
returned *one* client for the whole process, which under multi-tenancy means
one org querying another org's warehouse.

Why an explicit dict instead of ``lru_cache``:

* ``lru_cache`` offers only ``cache_clear()``. When one org edits a source we
  must drop *that* source's client, not every org's.
* LRU eviction gives no hook to notice a client being dropped.

Clients are keyed by *fingerprint* -- a hash of the connection parameters --
not by org id, and not by source id. Two orgs pointing at the same warehouse
with the same credentials share a client; a source that changes its credentials
gets a new one for free because its fingerprint changes; and an org with four
sources holds four entries.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from datatalk.config import Settings
from datatalk.warehouse import Warehouse, WarehouseSpec
from datatalk.warehouse import create as create_warehouse
from datatalk.warehouse import fingerprint as warehouse_fingerprint

# RLock, not Lock: _sweep() runs while a lookup already holds the lock.
_LOCK = threading.RLock()
_WH: dict[str, "_Entry"] = {}
_OA: dict[str, "_Entry"] = {}

_MAX_ENTRIES = 32
_IDLE_TTL = 30 * 60.0


@dataclass
class _Entry:
    obj: Any
    last_used: float = field(default_factory=time.monotonic)


def _sweep(registry: dict[str, _Entry]) -> None:
    """Drop idle entries, then oldest-first until under the cap.

    Caller must hold ``_LOCK``. Entries are only dropped, never closed -- see
    :func:`invalidate_clickhouse` for why.
    """
    now = time.monotonic()
    for key in [k for k, e in registry.items() if now - e.last_used > _IDLE_TTL]:
        registry.pop(key, None)
    while len(registry) > _MAX_ENTRIES:
        oldest = min(registry, key=lambda k: registry[k].last_used)
        registry.pop(oldest, None)


def _get_or_create(registry: dict[str, _Entry], key: str, factory) -> Any:
    """Look up ``key``, building it with ``factory`` on a miss.

    The lock is held only around dict access, never across ``factory()`` -- that
    does a network handshake, and holding the lock would serialize every org
    behind the slowest connection. Two concurrent misses may both build; the
    loser's object is discarded. That is the deliberate trade.
    """
    with _LOCK:
        entry = registry.get(key)
        if entry is not None:
            entry.last_used = time.monotonic()
            return entry.obj

    obj = factory()

    with _LOCK:
        existing = registry.get(key)
        if existing is not None:
            # Another thread won the race; keep one object for everyone.
            existing.last_used = time.monotonic()
            return existing.obj
        registry[key] = _Entry(obj)
        _sweep(registry)
        return obj


def openai_fingerprint(settings: Settings) -> str:
    raw = f"{settings.openai_api_key}|{settings.openai_base_url}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def warehouse_for(spec: WarehouseSpec, fp: str | None = None) -> Warehouse:
    """Return the shared warehouse for this source's connection parameters.

    ``fp`` is :func:`datatalk.warehouse.fingerprint` of ``spec``; callers that
    already computed it (every request context does) pass it in.
    """
    fp = fp or warehouse_fingerprint(spec)
    return _get_or_create(_WH, fp, lambda: create_warehouse(spec))


def openai_for(settings: Settings) -> OpenAI:
    """Return the shared OpenAI client for this key/base-url pair.

    ``openai.OpenAI`` wraps a thread-safe ``httpx.Client``, so one instance per
    (api_key, base_url) is correct and cheap to share across threads.
    """
    kwargs: dict[str, Any] = {
        "api_key": settings.openai_api_key,
        # Not part of the fingerprint: these are process-wide env settings, so
        # changing them already means a restart.
        "max_retries": settings.openai_max_retries,
        "timeout": settings.openai_timeout_seconds,
    }
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    return _get_or_create(_OA, openai_fingerprint(settings), lambda: OpenAI(**kwargs))


def invalidate_warehouse(fp: str) -> None:
    """Forget the warehouse for ``fp`` so the next lookup rebuilds it.

    Deliberately does NOT ``close()``. A daemon worker thread started before the
    source was edited still holds a strong reference and may be mid-query;
    closing underneath it raises ``ClosedPoolError`` in the middle of a report.
    The client is closed by GC once the last worker releases it.
    """
    with _LOCK:
        _WH.pop(fp, None)


def invalidate_openai(fp: str) -> None:
    with _LOCK:
        _OA.pop(fp, None)


def close_all() -> None:
    """Close and forget every cached client. For application shutdown only."""
    with _LOCK:
        for registry in (_WH, _OA):
            for entry in registry.values():
                close = getattr(entry.obj, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:  # noqa: BLE001 - shutdown is best-effort
                        pass
            registry.clear()
