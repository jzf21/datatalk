"""The seam between the app database and a Jira sync.

:func:`datatalk.integrations.jira.sync.run_sync` knows nothing of
``source_sync_state``; this module loads it, runs the sync, and records the
outcome -- each step in its *own* short transaction, because a sync can run for
minutes and must not pin an app-DB connection (or a row lock) while it does.
The UI endpoint and ``datatalk-sync`` both come through here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from datatalk.config import Settings, get_settings
from datatalk.db import models
from datatalk.db import session as db_session_mod
from datatalk.integrations import syncstore
from datatalk.integrations.jira.sync import JiraSourceConfig, OnEvent, run_sync

_MAX_ERROR_LEN = 500


class SourceNotFoundError(LookupError):
    """No such synced source in this org (routes turn it into a 404)."""


# --- provisioning ------------------------------------------------------------


def provision(
    db: Session,
    connection: models.OrgWarehouseConnection,
    settings: Settings | None = None,
) -> models.SourceSyncState:
    """Give a (flushed) Jira connection its schema, role and state row.

    The store is written first: if that fails, the caller's transaction rolls
    back and no state row points at a schema that does not exist. The reverse
    failure -- store written, app commit fails -- leaves an orphan schema, which
    ``datatalk-sync --gc`` removes.
    """
    schema, role = syncstore.names_for(connection.id)
    password = syncstore.new_role_password()
    with syncstore.admin_connection(settings) as conn:
        syncstore.provision(conn, schema, role, password)
    state = models.SourceSyncState(
        connection_id=connection.id,
        org_id=connection.org_id,
        schema_name=schema,
        role_name=role,
        role_password=password,
        last_status="never",
        stats={},
    )
    db.add(state)
    connection.sync_state = state
    db.flush()
    return state


def deprovision(schema: str, role: str, settings: Settings | None = None) -> None:
    with syncstore.admin_connection(settings) as conn:
        syncstore.drop(conn, schema, role)


def reset_cursor(connection: models.OrgWarehouseConnection) -> None:
    """Force the next sync to be full -- after the scope or site changed, the
    rows already stored were selected by a different question."""
    if connection.sync_state is not None:
        connection.sync_state.cursor = None


# --- running -----------------------------------------------------------------


def _load(db: Session, org_id: UUID, connection_id: UUID) -> models.OrgWarehouseConnection:
    conn = db.scalar(
        select(models.OrgWarehouseConnection).where(
            models.OrgWarehouseConnection.id == connection_id,
            models.OrgWarehouseConnection.org_id == org_id,
            models.OrgWarehouseConnection.type == "jira",
        )
    )
    if conn is None or conn.sync_state is None:
        raise SourceNotFoundError(str(connection_id))
    return conn


def sync_connection(
    org_id: UUID,
    connection_id: UUID,
    *,
    full: bool = False,
    on_event: OnEvent | None = None,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep=None,
) -> dict[str, Any]:
    """Run one sync end to end and record the outcome. Returns the public state.

    Raises :class:`SyncInProgressError` without touching the state row -- the
    run that holds the lock owns it -- and re-raises any other failure after
    recording it as ``error``.
    """
    s = settings or get_settings()
    with db_session_mod.session_scope() as db:
        conn = _load(db, org_id, connection_id)
        state = conn.sync_state
        cfg = JiraSourceConfig(
            site=conn.host,
            email=conn.username,
            api_token=conn.password or "",
            scope_jql=conn.scope_query,
            schema=state.schema_name,
            cursor=state.cursor,
        )
        role, role_password = state.role_name, state.role_password or ""

    kwargs: dict[str, Any] = {"transport": transport}
    if sleep is not None:
        kwargs["sleep"] = sleep

    def mark_running(kind: str, data: dict[str, Any]) -> None:
        # Only once the lock is held (the "start" event), so a refused second
        # sync cannot overwrite the status of the one actually running.
        if kind == "start":
            with db_session_mod.session_scope() as db:
                _load(db, org_id, connection_id).sync_state.last_status = "running"
        if on_event is not None:
            on_event(kind, data)

    try:
        # Re-assert the schema, role and grants first. Idempotent and cheap, and
        # it makes the store self-healing: a drifted grant, a restored backup,
        # or a delete whose store half ran before its app half rolled back are
        # all repaired by the next sync rather than by hand.
        with syncstore.admin_connection(s) as sc:
            syncstore.provision(sc, cfg.schema, role, role_password)
        result = run_sync(cfg, full=full, on_event=mark_running, settings=s, **kwargs)
    except syncstore.SyncInProgressError:
        raise
    except Exception as exc:
        with db_session_mod.session_scope() as db:
            st = _load(db, org_id, connection_id).sync_state
            st.last_status = "error"
            st.last_error = str(exc)[:_MAX_ERROR_LEN]
        raise

    with db_session_mod.session_scope() as db:
        st = _load(db, org_id, connection_id).sync_state
        st.cursor = result.cursor
        st.last_synced_at = datetime.now(timezone.utc)
        st.last_status = "ok"
        st.last_error = None
        st.stats = result.stats
        out = st.to_public_dict()

    # Row counts and samples are part of the cached catalog; a first sync is
    # also when the tables appear at all.
    from datatalk.warehouse import catalog

    catalog.invalidate_schema(org_id)
    return out
