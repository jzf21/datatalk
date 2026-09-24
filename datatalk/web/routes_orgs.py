"""Org membership and per-org data source management.

An org holds any number of named sources, each ClickHouse, Postgres, or Jira.
The agent picks between them per query, so there is no "current" source to
switch: these routes are plain CRUD over the collection.

A Jira source is *synced*, not queried live (see ``integrations/``): creating
one provisions its schema and read-only role in the sync store, deleting one
drops them, and ``POST .../sync`` refreshes it.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from datatalk import clients, warehouse
from datatalk.auth import orgs as orgs_svc
from datatalk.auth import sessions as sessions_svc
from datatalk.config import get_settings
from datatalk.db import models
from datatalk.integrations import syncstore
from datatalk.integrations.jira import client as jira_client
from datatalk.integrations.jira import service as jira_service
from datatalk.integrations.jira.sync import build_jql
from datatalk.memory.datacontext import invalidate_context
from datatalk.warehouse import catalog
from datatalk.web.deps import (
    RequestContext,
    csrf_guard,
    get_auth_session,
    get_current_user,
    get_db,
    get_request_ctx,
    require_admin,
    require_same_org,
)
from datatalk.web.streaming import MEDIA_TYPE, drain, ndjson

log = logging.getLogger(__name__)

# Router-level dependencies, so a route added here cannot forget either guard.
router = APIRouter(
    prefix="/api/orgs",
    tags=["orgs"],
    dependencies=[Depends(get_current_user), Depends(csrf_guard)],
)


class CreateOrgRequest(BaseModel):
    name: str


class _ConnectionBase(BaseModel):
    # The handle the LLM types in run_sql(source=...), so it is a plain
    # identifier: no spaces or punctuation for the model to mangle.
    name: str = Field(
        default="default", pattern=r"^[a-z][a-z0-9_]{0,39}$", max_length=40
    )
    # Free text describing what lives here. Goes into the schema catalog and is
    # the main signal the model uses to route a question to the right source.
    description: str = Field(default="", max_length=500)
    is_default: bool = False
    host: str
    user: str
    # Omitted or null means "keep the stored password" -- the UI never receives
    # the current one, so it cannot echo it back.
    password: str | None = None
    database: str
    secure: bool = False
    # Introspection scope. ``introspect_databases`` is a namespace allowlist
    # (ClickHouse databases, Postgres schemas); ``introspect_tables`` holds
    # qualified ``namespace.table`` entries and narrows *within* a namespace --
    # one named there shows only its listed tables, one absent shows all of
    # them. Both empty means "everything", which is what every source did
    # before the scope picker existed.
    introspect_databases: list[str] | None = Field(default=None, max_length=200)
    introspect_tables: list[str] | None = Field(default=None, max_length=2000)
    introspect_exclude_patterns: list[str] | None = None


class ClickHouseConnectionRequest(_ConnectionBase):
    type: Literal["clickhouse"] = "clickhouse"
    port: int = Field(default=8123, ge=1, le=65535)
    user: str = "default"
    database: str = "default"


class PostgresConnectionRequest(_ConnectionBase):
    type: Literal["postgres"] = "postgres"
    port: int = Field(default=5432, ge=1, le=65535)
    user: str = "postgres"
    database: str = "postgres"
    sslmode: Literal[
        "disable", "allow", "prefer", "require", "verify-ca", "verify-full"
    ] | None = None


class JiraConnectionRequest(_ConnectionBase):
    """A Jira Cloud site, reached as ``user`` (the account email) with
    ``password`` (an API token). ``host`` is the site, e.g. ``acme.atlassian.net``;
    a pasted URL is reduced to it, and anything off the allowlist is refused.

    ``port``/``database``/``secure`` exist only to share the base model: the
    agent never connects to this host -- it reads the synced copy.
    """

    type: Literal["jira"] = "jira"
    port: int = Field(default=443, ge=1, le=65535)
    database: str = "jira"
    secure: bool = True
    # JQL narrowing what is synced. Its own ORDER BY, if any, is ignored.
    scope_query: str | None = Field(default=None, max_length=2000)


# A discriminated union rather than one model with a `type` field: the port,
# user and database defaults genuinely differ per engine, Postgres alone has
# sslmode, and Jira alone has a scope query.
ConnectionRequest = Annotated[
    Union[
        ClickHouseConnectionRequest, PostgresConnectionRequest, JiraConnectionRequest
    ],
    Field(discriminator="type"),
]


# Both guards live in web/deps.py, beside the error-code table they implement,
# so routes_datacontext can reuse them without importing this module.
_require_admin = require_admin
_require_same_org = require_same_org


@router.get("")
def list_orgs(
    user: models.User = Depends(get_current_user),
    rctx: RequestContext = Depends(get_request_ctx),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    out = []
    for membership in orgs_svc.list_memberships(db, user.id):
        org = db.get(models.Org, membership.org_id)
        out.append(
            {
                "id": str(org.id),
                "slug": org.slug,
                "name": org.name,
                "role": membership.role,
                "is_current": org.id == rctx.tenant.org_id,
            }
        )
    return {"orgs": out}


@router.post("", status_code=201)
def create_org(
    req: CreateOrgRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Org name is required.")

    org = models.Org(name=name, slug=orgs_svc.unique_slug(db, name))
    db.add(org)
    db.flush()
    db.add(models.Membership(org_id=org.id, user_id=user.id, role="owner"))
    db.flush()
    return {"org": {"id": str(org.id), "slug": org.slug, "name": org.name, "role": "owner"}}


@router.post("/{org_id}/switch")
def switch_org(
    org_id: UUID,
    auth_session: models.AuthSession | None = Depends(get_auth_session),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    membership = orgs_svc.get_membership(db, org_id, user.id)
    if membership is None:
        raise HTTPException(status_code=403, detail="not_a_member")

    sessions_svc.set_current_org(db, auth_session, org_id)
    org = db.get(models.Org, org_id)
    return {
        "org": {"id": str(org.id), "slug": org.slug, "name": org.name, "role": membership.role}
    }


@router.get("/{org_id}/connections")
def list_connections(
    org_id: UUID,
    rctx: RequestContext = Depends(get_request_ctx),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_same_org(rctx, org_id)
    return {
        # never includes any password
        "connections": [
            c.to_public_dict() for c in orgs_svc.list_connections(db, org_id)
        ]
    }


@router.post("/{org_id}/connections", status_code=201)
def create_connection(
    org_id: UUID,
    req: ConnectionRequest,
    rctx: RequestContext = Depends(get_request_ctx),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_same_org(rctx, org_id)
    _require_admin(rctx)

    _check_jira(req, need_store=True)
    existing = orgs_svc.list_connections(db, org_id)
    if any(c.name == req.name for c in existing):
        raise HTTPException(status_code=409, detail="duplicate_source_name")

    connection = models.OrgWarehouseConnection(org_id=org_id, host=req.host)
    db.add(connection)
    _apply(connection, req, rctx)
    db.flush()
    if connection.type == "jira":
        # Before the default promotion below, so a store failure leaves
        # nothing half-made: the request's transaction rolls back whole.
        try:
            jira_service.provision(db, connection)
        except Exception as exc:  # noqa: BLE001 - the store's error, verbatim, to an admin
            raise HTTPException(
                status_code=502, detail="sync_store_error"
            ) from exc
    # The first source an org adds is its default whatever the request says --
    # ctx.warehouse(None) has to resolve to something.
    if req.is_default or not existing:
        _set_default(connection, db)

    _invalidate(org_id, rctx.tenant, db)
    return connection.to_public_dict()


@router.put("/{org_id}/connections/{connection_id}")
def update_connection(
    org_id: UUID,
    connection_id: UUID,
    req: ConnectionRequest,
    rctx: RequestContext = Depends(get_request_ctx),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_same_org(rctx, org_id)
    _require_admin(rctx)

    connection = orgs_svc.get_connection(db, org_id, connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    # A synced source and a live one differ in what they own (a schema and a
    # role in the sync store); converting between them is a delete and an add.
    if (connection.type == "jira") != (req.type == "jira"):
        raise HTTPException(status_code=409, detail="source_type_immutable")
    _check_jira(req)
    clash = [
        c
        for c in orgs_svc.list_connections(db, org_id)
        if c.name == req.name and c.id != connection.id
    ]
    if clash:
        raise HTTPException(status_code=409, detail="duplicate_source_name")

    _apply(connection, req, rctx)
    db.flush()
    if req.is_default:
        _set_default(connection, db)

    _invalidate(org_id, rctx.tenant, db)
    return connection.to_public_dict()


@router.post("/{org_id}/connections/test")
def test_connection(
    org_id: UUID,
    req: ConnectionRequest,
    rctx: RequestContext = Depends(get_request_ctx),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Try a candidate source without saving or caching it.

    Returns 200 with ``ok: false`` on failure: a failed *test* is a successful
    API call, and making the UI parse a 502 to show "wrong password" is worse.
    """
    _require_same_org(rctx, org_id)
    _require_admin(rctx)
    _check_jira(req)

    if req.type == "jira":
        return _test_jira(_candidate(db, org_id, req))

    spec = orgs_svc.spec_from_connection(_candidate(db, org_id, req))

    wh = None
    try:
        # Deliberately a throwaway warehouse, never the registry: a failed test
        # must not poison the live entry for this org.
        wh = warehouse.create(spec)
        info = wh.ping()
        tables = wh.introspect(with_samples=False)
        return {
            "ok": True,
            "version": info["version"],
            "database": info["database"],
            "table_count": len(tables),
        }
    except Exception as exc:  # noqa: BLE001 - surface any driver error to the UI
        return {"ok": False, "error": str(exc)}
    finally:
        if wh is not None:
            wh.close()


@router.post("/{org_id}/connections/discover")
def discover_connection(
    org_id: UUID,
    req: ConnectionRequest,
    rctx: RequestContext = Depends(get_request_ctx),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Browse the namespaces and tables a candidate source can see.

    This is what the scope picker lists, so it deliberately reports what the
    saved scope *excludes* -- a picker restricted to the current selection could
    never be used to widen it. Like ``test``, it takes a whole connection body
    rather than an id, so the picker works on a source that has not been saved
    yet, and it returns 200 with ``ok: false`` on a driver error.
    """
    _require_same_org(rctx, org_id)
    _require_admin(rctx)

    if req.type == "jira":
        # Nothing to pick: a Jira source's tables are the fixed synced set, and
        # its scope is the JQL, not a table selection.
        return {"ok": True, "truncated": False, "databases": []}

    spec = orgs_svc.spec_from_connection(_candidate(db, org_id, req))

    wh = None
    try:
        wh = warehouse.create(spec)
        namespaces, truncated = wh.discover()
        return {
            "ok": True,
            "truncated": truncated,
            "databases": [
                {
                    "name": ns.namespace,
                    "tables": [
                        {"name": t.name, "rows": t.total_rows, "comment": t.comment}
                        for t in ns.tables
                    ],
                }
                for ns in namespaces
            ],
        }
    except Exception as exc:  # noqa: BLE001 - surface any driver error to the UI
        return {"ok": False, "error": str(exc)}
    finally:
        if wh is not None:
            wh.close()


def _candidate(
    db: Session, org_id: UUID, req: Any
) -> models.OrgWarehouseConnection:
    """An unsaved row for a candidate source, for ``test`` and ``discover``.

    Never added to the session: both callers want a spec to connect with, not a
    row. A ``None`` password means "keep the stored one", matched by name since
    a candidate has no id yet.
    """
    password = req.password
    if password is None:
        stored = next(
            (c for c in orgs_svc.list_connections(db, org_id) if c.name == req.name),
            None,
        )
        password = stored.password if stored else ""

    return models.OrgWarehouseConnection(
        org_id=org_id,
        name=req.name,
        type=req.type,
        host=req.host,
        port=req.port,
        username=req.user,
        password=password,
        database=req.database,
        secure=req.secure,
        sslmode=getattr(req, "sslmode", None),
        introspect_databases=req.introspect_databases or [],
        introspect_tables=req.introspect_tables or [],
        introspect_exclude_patterns=req.introspect_exclude_patterns or [],
        scope_query=getattr(req, "scope_query", None),
    )


@router.delete("/{org_id}/connections/{connection_id}", status_code=204)
def delete_connection(
    org_id: UUID,
    connection_id: UUID,
    rctx: RequestContext = Depends(get_request_ctx),
    db: Session = Depends(get_db),
) -> None:
    _require_same_org(rctx, org_id)
    _require_admin(rctx)

    connection = orgs_svc.get_connection(db, org_id, connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="connection_not_found")

    was_default = connection.is_default
    state = connection.sync_state
    stored = (state.schema_name, state.role_name) if state is not None else None
    db.delete(connection)
    db.flush()
    if stored is not None:
        # Best-effort: a store that is down must not make a source undeletable.
        # Whatever survives is swept by `datatalk-sync --gc`.
        try:
            jira_service.deprovision(*stored)
        except Exception:  # noqa: BLE001
            log.warning("could not drop sync-store schema %s; run datatalk-sync --gc", stored[0])

    if was_default:
        # Something must remain resolvable as the default, or every query that
        # names no source starts failing.
        remaining = orgs_svc.list_connections(db, org_id)
        if remaining:
            remaining[0].is_default = True
            db.flush()

    _invalidate(org_id, rctx.tenant, db)


def _apply(
    connection: models.OrgWarehouseConnection,
    req: Any,
    rctx: RequestContext,
) -> None:
    """Copy a validated request onto a row. Does not touch ``is_default``."""
    # A new row leaves is_default as None until the column's server default
    # (true) applies at flush -- which violates the one-default-per-org partial
    # unique index before _set_default gets a chance to demote the old one.
    if connection.is_default is None:
        connection.is_default = False

    if req.type == "jira":
        # Rows already stored were selected by the old site/account/scope; the
        # next sync must re-read everything rather than extend them.
        scope = (req.scope_query or "").strip() or None
        if (connection.host, connection.username, connection.scope_query) != (
            req.host,
            req.user,
            scope,
        ):
            jira_service.reset_cursor(connection)
        connection.scope_query = scope
    else:
        connection.scope_query = None

    connection.name = req.name
    connection.description = req.description or _default_description(req)
    connection.type = req.type
    connection.host = req.host
    connection.port = req.port
    connection.username = req.user
    connection.database = req.database
    connection.secure = req.secure
    connection.sslmode = getattr(req, "sslmode", None)
    if req.password is not None:
        connection.password = req.password  # encrypted by the column type
    if req.introspect_databases is not None:
        connection.introspect_databases = req.introspect_databases
    if req.introspect_tables is not None:
        connection.introspect_tables = req.introspect_tables
    if req.introspect_exclude_patterns is not None:
        connection.introspect_exclude_patterns = req.introspect_exclude_patterns
    connection.updated_by_user_id = rctx.user.id


def _set_default(connection: models.OrgWarehouseConnection, db: Session) -> None:
    """Promote one source, demoting the rest.

    Demote-then-flush-then-promote: a partial unique index enforces one default
    per org, so setting the new one first would violate it mid-transaction.
    """
    for other in orgs_svc.list_connections(db, connection.org_id):
        if other.id != connection.id and other.is_default:
            other.is_default = False
    db.flush()
    connection.is_default = True
    db.flush()


def _invalidate(org_id: UUID, tenant, db: Session) -> None:
    """Drop cached clients and schema for an org whose sources changed.

    Without this the org keeps querying its old warehouse, and keeps seeing the
    old schema context, until the process restarts. ``tenant`` is the context
    built *before* the mutation, so its per-source fingerprints are the stale
    registry keys -- every one is dropped, since a rename or a default change
    can move which source a query resolves to.

    The context model goes too: its ``covers`` entries name sources, so a rename
    changes which file ``describe_source`` attaches.
    """
    for ref in tenant.sources:
        clients.invalidate_warehouse(ref.fingerprint)
    catalog.invalidate_schema(org_id)
    invalidate_context(org_id)


# --- Jira ---------------------------------------------------------------------


def _check_jira(req: Any, *, need_store: bool = False) -> None:
    """Normalize a Jira request's site in place, or refuse it. No-op otherwise.

    The allowlist is the SSRF guard: this server makes authenticated requests
    to the host, so an admin must not be able to point it at the deployment's
    own network.
    """
    if req.type != "jira":
        return
    settings = get_settings()
    try:
        req.host = jira_client.normalize_site(
            req.host, settings.jira_allowed_host_suffix_list
        )
    except jira_client.JiraHostNotAllowedError:
        raise HTTPException(status_code=400, detail="jira_host_not_allowed") from None
    if need_store and not syncstore.is_configured(settings):
        raise HTTPException(status_code=409, detail="sync_store_unconfigured")


def _default_description(req: Any) -> str:
    """What the model routes on when the admin wrote nothing. Only Jira gets
    one: a SQL source's contents cannot be guessed from its credentials."""
    if req.type == "jira":
        return (
            f"Jira ({req.host}): issues, status history, sprints and worklogs, "
            "synced periodically -- not live."
        )
    return ""


def _test_jira(candidate: models.OrgWarehouseConnection) -> dict[str, Any]:
    """Can we sign in, and how much would the scope sync? 200 either way."""
    try:
        with jira_client.JiraClient(
            candidate.host, candidate.username, candidate.password or ""
        ) as jc:
            me = jc.myself() or {}
            jql = build_jql(candidate.scope_query, None, me.get("timeZone"))
            count = jc.approximate_count(jql.rsplit(" ORDER BY ", 1)[0])
        return {
            "ok": True,
            "version": "Jira Cloud",
            "database": candidate.host,
            "account": me.get("displayName") or me.get("emailAddress") or "",
            "issue_count": count,
            "table_count": count,
        }
    except Exception as exc:  # noqa: BLE001 - surface any Jira error to the UI
        return {"ok": False, "error": str(exc)}


@router.post("/{org_id}/connections/{connection_id}/sync")
def sync_connection(
    org_id: UUID,
    connection_id: UUID,
    full: bool = Query(default=False),
    rctx: RequestContext = Depends(get_request_ctx),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Refresh a synced source, streaming NDJSON progress.

    Everything that can 4xx is decided before the stream opens, as in
    ``routes_datacontext.generate``. "Already syncing" cannot be: it is an
    advisory lock in the sync store, shared with other workers and with cron,
    so it arrives as an ``error`` event carrying ``code: sync_in_progress``.
    """
    _require_same_org(rctx, org_id)
    _require_admin(rctx)
    if not syncstore.is_configured():
        raise HTTPException(status_code=409, detail="sync_store_unconfigured")
    connection = orgs_svc.get_connection(db, org_id, connection_id)
    if connection is None or connection.type != "jira" or connection.sync_state is None:
        raise HTTPException(status_code=404, detail="connection_not_found")

    def stream():
        rctx.release_db()
        q: queue.Queue = queue.Queue()
        holder: dict[str, Any] = {}

        def worker() -> None:
            try:
                holder["result"] = jira_service.sync_connection(
                    org_id,
                    connection_id,
                    full=full,
                    on_event=lambda kind, data: q.put((kind, data)),
                )
            except syncstore.SyncInProgressError:
                holder["error"] = {
                    "code": "sync_in_progress",
                    "message": "This source is already syncing.",
                }
            except Exception as exc:  # noqa: BLE001 - surfaced as an error event
                holder["error"] = {"code": "sync_failed", "message": str(exc)}
            finally:
                q.put(None)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        for kind, data in drain(q):
            # Our own "done" is the terminal event; the sync's is progress.
            yield ndjson("progress" if kind == "done" else kind, data)
        t.join()
        if "result" in holder:
            yield ndjson("synced", holder["result"])
        elif "error" in holder:
            yield ndjson("error", holder["error"])
        yield ndjson("done", {})

    return StreamingResponse(stream(), media_type=MEDIA_TYPE)
