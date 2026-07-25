"""Request-scoped dependencies: session, user, org, tenant context.

Error bodies use stable machine codes in ``detail`` so one frontend wrapper can
branch on them:

    401 not_authenticated     -> show the login screen
    403 no_org                -> "ask an admin to invite you"
    403 forbidden             -> hide/disable admin controls
    409 no_connection         -> open the connection settings panel
    403 cross_origin_request  -> should never reach a real user
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from datatalk.auth import orgs as orgs_svc
from datatalk.auth import sessions as sessions_svc
from datatalk.config import get_settings
from datatalk.context import TenantContext
from datatalk.db import models
from datatalk.db.session import db_session
from datatalk.memory.store import MemoryStore

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class RequestContext:
    """Request-scoped bundle.

    ``tenant`` is frozen and safe to hand to a worker thread. ``db`` and
    ``store`` are NOT -- a SQLAlchemy Session is not thread-safe, and the
    streaming endpoints must open their own session for the post-generation
    write. See web/app.py.
    """

    tenant: TenantContext
    db: Session
    store: MemoryStore
    user: models.User
    org: models.Org


def get_db(db: Session = Depends(db_session)) -> Session:
    return db


def session_cookie(request: Request) -> str | None:
    return request.cookies.get(get_settings().cookie_name)


def get_auth_session(
    request: Request, db: Session = Depends(get_db)
) -> models.AuthSession | None:
    return sessions_svc.load_session(db, session_cookie(request))


def csrf_guard(request: Request) -> None:
    """Reject cross-origin state-changing requests.

    Deliberately not a double-submit token. Every mutating endpoint takes
    application/json, which a cross-origin form cannot produce; a cross-origin
    fetch needs a CORS preflight; and the session cookie is SameSite=Lax. This
    check is the cheap fourth layer, and it accepts the configured CORS origins
    so the separate frontend keeps working.
    """
    if request.method in SAFE_METHODS:
        return

    site = request.headers.get("sec-fetch-site")
    if site in {"same-origin", "none"}:
        return

    origin = request.headers.get("origin")
    if origin is None:
        return  # non-browser client (curl, tests); carries no ambient cookie

    netloc = urlparse(origin).netloc
    if netloc == request.headers.get("host", ""):
        return
    if origin.rstrip("/") in get_settings().cors_origin_list:
        return

    raise HTTPException(status_code=403, detail="cross_origin_request")


def get_current_user(
    auth_session: models.AuthSession | None = Depends(get_auth_session),
    db: Session = Depends(get_db),
) -> models.User:
    if auth_session is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    user = db.get(models.User, auth_session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="not_authenticated")
    return user


def get_current_membership(
    auth_session: models.AuthSession | None = Depends(get_auth_session),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> models.Membership:
    """Resolve the acting org: the session's, else the user's first membership.

    Re-checked on every request, so revoking membership takes effect at once
    even for a session that is already logged in.
    """
    assert auth_session is not None  # get_current_user would have raised

    if auth_session.current_org_id is not None:
        membership = orgs_svc.get_membership(db, auth_session.current_org_id, user.id)
        if membership is not None:
            return membership
        # Membership was revoked, or the org was deleted; fall through.

    memberships = orgs_svc.list_memberships(db, user.id)
    if not memberships:
        raise HTTPException(status_code=403, detail="no_org")

    membership = memberships[0]
    auth_session.current_org_id = membership.org_id
    db.flush()
    return membership


def get_tenant_ctx(
    membership: models.Membership = Depends(get_current_membership),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TenantContext:
    org = db.get(models.Org, membership.org_id)
    if org is None:
        raise HTTPException(status_code=403, detail="no_org")
    return orgs_svc.build_tenant_context(db, org=org, user=user, role=membership.role)


def get_request_ctx(
    ctx: TenantContext = Depends(get_tenant_ctx),
    membership: models.Membership = Depends(get_current_membership),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RequestContext:
    org = db.get(models.Org, membership.org_id)
    return RequestContext(
        tenant=ctx, db=db, store=MemoryStore(db, ctx=ctx), user=user, org=org
    )


def require_connection(
    rctx: RequestContext = Depends(get_request_ctx),
) -> RequestContext:
    """For endpoints that actually need ClickHouse.

    409 rather than a 502 at query time, so the UI can open the settings panel
    instead of showing a driver error. Reads the flag the context already
    resolved rather than re-querying; ``TenantContext.clickhouse`` raises
    ``NoConnectionError`` regardless, so this is the friendly path, not the
    enforcing one.

    Deliberately not applied to the pure-Postgres endpoints (``/api/reports``,
    ``/api/dashboards``, ``/api/suggestions``): a new org must be able to load
    its empty library and reach the settings panel.
    """
    if not rctx.tenant.has_connection:
        raise HTTPException(status_code=409, detail="no_connection")
    return rctx
