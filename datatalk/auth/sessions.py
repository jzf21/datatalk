"""Opaque server-side sessions.

Not JWTs. Logout, password change and removal-from-org must revoke *now*, and
org switching is server-side state (``auth_sessions.current_org_id``). With a
JWT, a user removed from org A keeps acting as org A until the token expires --
a tenant-isolation hole, which is the thing this whole change exists to close.

The raw token goes in the cookie; only its sha256 is stored, so a leaked
database dump cannot be replayed as a login.
"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from datatalk.config import get_settings
from datatalk.db import models

_TOKEN_BYTES = 32  # 256 bits
# Refresh last_seen_at at most this often, to avoid a write on every request.
_TOUCH_INTERVAL = timedelta(hours=1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def _clean_ip(value: str | None) -> str | None:
    """Drop anything that is not a real IP.

    ``request.client.host`` is not guaranteed to be one -- unix sockets and some
    proxies produce hostnames -- and the column is INET, so storing a bad value
    fails the whole login rather than losing one audit field.
    """
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def create_session(
    db: Session,
    *,
    user_id: UUID,
    org_id: UUID | None,
    user_agent: str | None = None,
    ip: str | None = None,
) -> str:
    """Create a session row and return the raw token for the cookie.

    Always called fresh on login -- never reuse a pre-auth token (session
    fixation).
    """
    settings = get_settings()
    raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    db.add(
        models.AuthSession(
            id=hash_token(raw_token),
            user_id=user_id,
            current_org_id=org_id,
            expires_at=_now() + timedelta(days=settings.session_ttl_days),
            user_agent=(user_agent or "")[:500] or None,
            ip=_clean_ip(ip),
        )
    )
    db.flush()
    return raw_token


def load_session(db: Session, raw_token: str | None) -> models.AuthSession | None:
    """Return a live session for this token, refreshing it lazily.

    Returns None for absent, unknown or expired tokens; expired rows are deleted
    on sight.
    """
    if not raw_token:
        return None

    row = db.execute(
        select(models.AuthSession).where(models.AuthSession.id == hash_token(raw_token))
    ).scalar_one_or_none()
    if row is None:
        return None

    if row.expires_at <= _now():
        db.delete(row)
        db.flush()
        return None

    now = _now()
    if now - row.last_seen_at > _TOUCH_INTERVAL:
        row.last_seen_at = now
        # Rolling expiry: active sessions keep sliding forward.
        row.expires_at = now + timedelta(days=get_settings().session_ttl_days)
        db.flush()
    return row


def revoke_session(db: Session, raw_token: str | None) -> bool:
    if not raw_token:
        return False
    result = db.execute(
        delete(models.AuthSession).where(models.AuthSession.id == hash_token(raw_token))
    )
    return bool(result.rowcount)


def revoke_all_for_user(db: Session, user_id: UUID) -> int:
    """Used on password change and when a user is removed from an org."""
    result = db.execute(
        delete(models.AuthSession).where(models.AuthSession.user_id == user_id)
    )
    return int(result.rowcount or 0)


def sweep_expired(db: Session) -> int:
    result = db.execute(
        delete(models.AuthSession).where(models.AuthSession.expires_at <= _now())
    )
    return int(result.rowcount or 0)


def set_current_org(db: Session, session_row: models.AuthSession, org_id: UUID) -> None:
    session_row.current_org_id = org_id
    db.flush()
