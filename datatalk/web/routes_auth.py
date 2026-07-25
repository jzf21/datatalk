"""Public authentication endpoints: signup, login, logout, me.

Handlers are ``def``, never ``async def``: argon2 costs ~50-80ms of CPU, which
on the event loop would freeze the entire application for every login attempt.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from datatalk.auth import orgs as orgs_svc
from datatalk.auth import passwords, sessions as sessions_svc
from datatalk.config import get_settings
from datatalk.db import models
from datatalk.web.deps import get_db, session_cookie

router = APIRouter(prefix="/api/auth", tags=["auth"])

_MAX_FAILED_LOGINS = 10
_LOCKOUT = timedelta(minutes=15)


class SignupRequest(BaseModel):
    email: EmailStr
    password: str
    org_name: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _set_session_cookie(response: Response, raw_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.cookie_name,
        value=raw_token,
        max_age=settings.session_ttl_days * 24 * 3600,
        httponly=True,          # JS must never read it
        samesite="lax",         # withheld from cross-site XHR
        secure=settings.cookie_secure,
        path="/",
        # No domain -> host-only, so sibling subdomains cannot receive it.
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=get_settings().cookie_name, path="/", samesite="lax"
    )


def _find_user(db: Session, email: str) -> models.User | None:
    return db.execute(
        select(models.User).where(func.lower(models.User.email) == email.strip().lower())
    ).scalar_one_or_none()


def _org_payload(org: models.Org, role: str) -> dict[str, Any]:
    return {"id": str(org.id), "slug": org.slug, "name": org.name, "role": role}


def _user_payload(user: models.User) -> dict[str, Any]:
    return {"id": str(user.id), "email": user.email, "name": user.name}


@router.post("/signup", status_code=201)
def signup(
    req: SignupRequest, response: Response, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.allow_open_signup:
        raise HTTPException(status_code=403, detail="signup_disabled")

    try:
        passwords.validate_password(req.password)
    except passwords.PasswordPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if _find_user(db, req.email) is not None:
        # Signup necessarily reveals existence; login deliberately does not.
        raise HTTPException(status_code=409, detail="email_taken")

    user = models.User(
        email=req.email.strip(), password_hash=passwords.hash_password(req.password)
    )
    db.add(user)
    db.flush()

    org_name = (req.org_name or "").strip() or f"{req.email.split('@')[0]}'s org"
    org = models.Org(name=org_name, slug=orgs_svc.unique_slug(db, org_name))
    db.add(org)
    db.flush()
    db.add(models.Membership(org_id=org.id, user_id=user.id, role="owner"))
    db.flush()

    raw_token = sessions_svc.create_session(
        db,
        user_id=user.id,
        org_id=org.id,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    _set_session_cookie(response, raw_token)
    return {"user": _user_payload(user), "org": _org_payload(org, "owner")}


@router.post("/login")
def login(
    req: LoginRequest, response: Response, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    # Opportunistic cleanup; cheap and keeps the table from growing forever.
    if random.random() < 0.005:  # noqa: S311 - not security-sensitive
        sessions_svc.sweep_expired(db)

    user = _find_user(db, req.email)

    if user is not None and user.locked_until and user.locked_until > _now():
        raise HTTPException(status_code=429, detail="too_many_attempts")

    # verify_password burns equivalent time when user is None, so response
    # timing does not reveal whether the account exists.
    if not passwords.verify_password(user.password_hash if user else None, req.password):
        if user is not None:
            user.failed_logins += 1
            if user.failed_logins >= _MAX_FAILED_LOGINS:
                user.locked_until = _now() + _LOCKOUT
                user.failed_logins = 0
            db.flush()
        raise HTTPException(status_code=401, detail="invalid_credentials")

    if not user.is_active:
        raise HTTPException(status_code=401, detail="invalid_credentials")

    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(req.password)

    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = _now()
    db.flush()

    memberships = orgs_svc.list_memberships(db, user.id)
    current = memberships[0] if memberships else None

    # A brand-new token every login: never reuse a pre-auth one (fixation).
    raw_token = sessions_svc.create_session(
        db,
        user_id=user.id,
        org_id=current.org_id if current else None,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    _set_session_cookie(response, raw_token)

    orgs_out = [
        _org_payload(db.get(models.Org, m.org_id), m.role) for m in memberships
    ]
    return {
        "user": _user_payload(user),
        "org": orgs_out[0] if orgs_out else None,
        "orgs": orgs_out,
    }


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> Response:
    # Always both: delete the row AND expire the cookie.
    sessions_svc.revoke_session(db, session_cookie(request))
    _clear_session_cookie(response)
    return Response(status_code=204)


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Public on purpose: returns 200 {"authenticated": false} rather than 401,
    so the frontend can boot without a spurious error."""
    auth_session = sessions_svc.load_session(db, session_cookie(request))
    if auth_session is None:
        return {"authenticated": False}

    user = db.get(models.User, auth_session.user_id)
    if user is None or not user.is_active:
        return {"authenticated": False}

    memberships = orgs_svc.list_memberships(db, user.id)
    orgs_out = [_org_payload(db.get(models.Org, m.org_id), m.role) for m in memberships]

    current = next(
        (m for m in memberships if m.org_id == auth_session.current_org_id),
        memberships[0] if memberships else None,
    )
    current_org = _org_payload(db.get(models.Org, current.org_id), current.role) if current else None
    connection = (
        orgs_svc.default_connection(db, current.org_id) if current else None
    )

    return {
        "authenticated": True,
        "user": _user_payload(user),
        "org": current_org,
        "orgs": orgs_out,
        "connection": {"configured": connection is not None},
    }
