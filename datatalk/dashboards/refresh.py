"""Re-run a saved dashboard's captured queries and re-materialize its document.

The whole feature rests on one identity: a refresh is what
:func:`~datatalk.agent.dashboard.generate_dashboard` does at its last step, and
nothing else. It calls the same :func:`~datatalk.agent.blocks.materialize` on the
same authoring document, against datasets carrying the same ``dataset_id``s. No
second rendering path exists, so there is no way for a refreshed number to be
produced differently from a generated one -- which matters more here than
anywhere, because the project's core rule is that the LLM never types a number
and this path has no LLM in it at all.

Two failure postures, both borrowed from code that already had to solve them:

* **A dead query degrades its own blocks, never the dashboard.** A failed dataset
  is simply absent from the map handed to ``materialize``, which already turns an
  unresolvable reference into an italic note rather than raising. That is the
  same posture ``warehouse.catalog`` takes when one source will not introspect.
* **A refresh in flight blocks a second one** for the same dashboard, per
  process, exactly as a context-model generation does.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

from datatalk.agent.blocks import (
    Document,
    dematerialize,
    frozen_stat_count,
    materialize,
)
from datatalk.agent.executor import UnsafeSQLError, run_sql
from datatalk.context import NoConnectionError, UnknownSourceError
from datatalk.dashboards.filters import build_bindings
from datatalk.warehouse.base import QueryResult, WarehouseError

if TYPE_CHECKING:
    from datatalk.context import TenantContext
    from datatalk.memory.store import SavedDashboard

# Wall-clock budget for the whole refresh. Each individual query is already
# capped by its source's `sql_timeout_seconds` (30s by default); this bounds the
# batch so one slow warehouse cannot hold a request open indefinitely.
REFRESH_DEADLINE_S = 60.0

# Matches the Analyst's own fan-out. Dashboards run 10-16 widgets off a handful
# of datasets, so this is about not stampeding one warehouse, not throughput.
_MAX_CONCURRENCY = 4


# --- one-refresh-at-a-time, per dashboard ------------------------------------
#
# Keyed by (org_id, dashboard_id), never by dashboard_id alone: ids are per
# table, not per org, so a bare id would let one tenant's refresh lock another
# tenant's dashboard -- and the resulting 409 would confirm that a dashboard with
# that id is active in a foreign org. Per process, like the context-model guard:
# it exists to absorb a double-click and a runaway poll, which is what actually
# happens; two uvicorn workers racing is not worth a database lock.

_RUNNING: set[tuple[UUID, int]] = set()
_RUNNING_LOCK = threading.Lock()


def try_acquire(org_id: UUID, dashboard_id: int) -> bool:
    with _RUNNING_LOCK:
        key = (org_id, dashboard_id)
        if key in _RUNNING:
            return False
        _RUNNING.add(key)
        return True


def release(org_id: UUID, dashboard_id: int) -> None:
    with _RUNNING_LOCK:
        _RUNNING.discard((org_id, dashboard_id))


@dataclass
class DatasetStatus:
    """What happened to one captured query during a refresh."""

    dataset_id: str
    source: str | None
    status: str  # ok | unavailable | rejected | error | timeout
    reason: str = ""
    message: str = ""
    row_count: int = 0
    truncated: bool = False
    filtered: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "dataset_id": self.dataset_id,
            "source": self.source,
            "status": self.status,
            "ok": self.status == "ok",
        }
        if self.status == "ok":
            out["row_count"] = self.row_count
            out["truncated"] = self.truncated
            out["filtered"] = self.filtered
        else:
            out["reason"] = self.reason
            out["message"] = self.message
        return out


@dataclass
class RefreshResult:
    document: Document
    datasets: list[DatasetStatus] = field(default_factory=list)
    # Stat tiles a legacy dashboard could not rebuild (see blocks.dematerialize).
    frozen_stats: int = 0
    exact: bool = True
    # Datasets no filter could be bound to, so their numbers ignore the current
    # selection. Reported rather than hidden: a widget silently unaffected by a
    # filter is the failure mode this feature exists to avoid.
    unfiltered: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return (
            any(d.status != "ok" for d in self.datasets)
            or self.frozen_stats > 0
            or bool(self.unfiltered)
        )


def _classify(exc: Exception) -> tuple[str, str]:
    """(status, reason) for one query's failure. Never re-raised."""
    if isinstance(exc, UnknownSourceError):
        # The source was renamed or deleted after the dashboard was saved.
        return "unavailable", "unknown_source"
    if isinstance(exc, NoConnectionError):
        return "unavailable", "no_connection"
    if isinstance(exc, UnsafeSQLError):
        # The stored SQL no longer passes the guardrails -- they can only get
        # stricter, so this means the rules tightened under an old dashboard.
        return "rejected", "unsafe_sql"
    if isinstance(exc, WarehouseError):
        return "error", "query_failed"
    return "error", "refresh_failed"


def _run_one(
    entry: dict[str, Any],
    *,
    ctx: "TenantContext",
    bindings: dict[str, Any] | None,
) -> tuple[str, QueryResult | None, DatasetStatus]:
    """Execute one captured query. Returns (dataset_id, result-or-None, status)."""
    dataset_id = str(entry.get("dataset_id") or "")
    source = entry.get("source")
    sql = entry.get("sql") or ""
    parameters = None
    filtered = False

    if bindings is not None:
        bound = bindings.get(dataset_id)
        if bound is not None:
            sql, parameters = bound.sql, bound.parameters
            filtered = True

    try:
        # Straight through run_sql, so validate_sql + ensure_limit run again on
        # the exact text about to execute. Free defence in depth: the guardrails
        # may have tightened since this SQL was captured, and with parameters
        # bound by the driver the statement does not vary with user input, so
        # what is validated is what runs, every time.
        result = run_sql(sql, ctx=ctx, source=source, parameters=parameters)
    except Exception as exc:  # noqa: BLE001 - every failure is per-dataset
        status, reason = _classify(exc)
        return dataset_id, None, DatasetStatus(
            dataset_id=dataset_id,
            source=source,
            status=status,
            reason=reason,
            message=str(exc)[:500],
        )

    return dataset_id, result, DatasetStatus(
        dataset_id=dataset_id,
        source=source,
        status="ok",
        row_count=result.row_count,
        truncated=result.truncated,
        filtered=filtered,
    )


def refresh_dashboard(
    saved: "SavedDashboard",
    *,
    ctx: "TenantContext",
    selections: dict[str, Any] | None = None,
    bindings: dict[str, Any] | None = None,
    deadline_s: float = REFRESH_DEADLINE_S,
) -> RefreshResult:
    """Re-execute ``saved``'s captured queries and re-materialize its document.

    ``selections`` are the client's filter choices, keyed by filter id; they are
    coerced and bound here. ``bindings`` lets a caller pass already-bound queries
    directly (tests do). Neither means every query runs exactly as captured.

    Never raises for a query-level problem -- see :class:`DatasetStatus`. It can
    still raise :class:`~datatalk.context.NoConnectionError` if the org has no
    sources at all, which the endpoint pre-empts with ``require_connection``.
    """
    if bindings is None and selections:
        # Resolve here rather than at the endpoint so the dialect lookup and the
        # per-dataset degradation live beside the execution they affect.
        dialects = {}
        for entry in saved.queries or []:
            dataset_id = entry.get("dataset_id")
            if not dataset_id:
                continue
            try:
                dialects[dataset_id] = ctx.warehouse(entry.get("source")).dialect
            except Exception:  # noqa: BLE001 - a dead source fails per-query below
                continue
        binding_set = build_bindings(saved.filters or {}, selections, dialects)
        bindings = binding_set.bound
        unfiltered = binding_set.unfiltered
    else:
        unfiltered = []

    authoring = saved.authoring_document
    exact = saved.is_refreshable
    if not exact:
        # Saved before the authoring document was kept. Tables and charts come
        # back exactly; stat tiles keep their old values rather than being
        # guessed at.
        authoring = dematerialize(saved.document)

    entries = [q for q in (saved.queries or []) if q.get("dataset_id")]
    datasets: dict[str, QueryResult] = {}
    statuses: dict[str, DatasetStatus] = {}

    if entries:
        started = time.monotonic()
        with ThreadPoolExecutor(
            max_workers=min(_MAX_CONCURRENCY, len(entries))
        ) as pool:
            futures = {
                pool.submit(_run_one, e, ctx=ctx, bindings=bindings): e
                for e in entries
            }
            remaining = max(0.0, deadline_s - (time.monotonic() - started))
            done, pending = wait(futures.keys(), timeout=remaining)

            for future in done:
                dataset_id, result, status = future.result()
                statuses[dataset_id] = status
                if result is not None:
                    datasets[dataset_id] = result

            for future in pending:
                # Cancel what has not started; a query already in flight is
                # bounded by its own sql_timeout_seconds and is left to finish
                # into a discarded result. Holding the request (and the
                # per-dashboard guard) until every thread joins would let one
                # hung warehouse lock the dashboard out entirely.
                future.cancel()
                entry = futures[future]
                dataset_id = str(entry.get("dataset_id") or "")
                statuses[dataset_id] = DatasetStatus(
                    dataset_id=dataset_id,
                    source=entry.get("source"),
                    status="timeout",
                    reason="deadline_exceeded",
                    message=f"Not finished within {int(deadline_s)}s.",
                )

    document = materialize(authoring, datasets)
    ordered = [statuses[e["dataset_id"]] for e in entries if e["dataset_id"] in statuses]

    return RefreshResult(
        document=document,
        datasets=ordered,
        frozen_stats=0 if exact else frozen_stat_count(document),
        exact=exact,
        unfiltered=unfiltered,
    )
