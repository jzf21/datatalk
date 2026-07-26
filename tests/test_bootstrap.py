"""Startup bootstrap of the configured org + owner.

.env.example advertised these three variables as "creates this org + owner on
startup if absent" while nothing read them. They matter most exactly where they
were missing: with DATATALK_ALLOW_OPEN_SIGNUP=false there is no other way to
create the first account.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from datatalk.auth import orgs as orgs_svc
from datatalk.auth import passwords
from datatalk.config import Settings
from datatalk.db import models


def _settings(**kw) -> Settings:
    base = {
        "DATATALK_BOOTSTRAP_ORG_NAME": "Bootstrapped",
        "DATATALK_BOOTSTRAP_ADMIN_EMAIL": "admin@example.com",
        "DATATALK_BOOTSTRAP_ADMIN_PASSWORD": "correct-horse-battery",
    }
    base.update(kw)
    return Settings(**base)


def _user(db, email):
    return db.execute(
        select(models.User).where(func.lower(models.User.email) == email.lower())
    ).scalar_one_or_none()


def test_creates_org_user_and_membership(db):
    made = orgs_svc.bootstrap_from_settings(db, _settings())
    assert made is not None

    user = _user(db, "admin@example.com")
    assert user is not None
    org = db.execute(
        select(models.Org).where(models.Org.name == "Bootstrapped")
    ).scalar_one()
    membership = orgs_svc.get_membership(db, org.id, user.id)
    assert membership.role == "owner"
    assert passwords.verify_password(user.password_hash, "correct-horse-battery")


def test_is_idempotent(db):
    orgs_svc.bootstrap_from_settings(db, _settings())
    assert orgs_svc.bootstrap_from_settings(db, _settings()) is None

    assert db.execute(select(func.count()).select_from(models.Org)).scalar() == 1
    assert db.execute(select(func.count()).select_from(models.User)).scalar() == 1
    assert db.execute(select(func.count()).select_from(models.Membership)).scalar() == 1


def test_does_nothing_when_unconfigured(db):
    assert orgs_svc.bootstrap_from_settings(db, _settings(
        DATATALK_BOOTSTRAP_ADMIN_PASSWORD="")) is None
    assert orgs_svc.bootstrap_from_settings(db, _settings(
        DATATALK_BOOTSTRAP_ORG_NAME="")) is None
    assert orgs_svc.bootstrap_from_settings(db, _settings(
        DATATALK_BOOTSTRAP_ADMIN_EMAIL="")) is None
    assert db.execute(select(func.count()).select_from(models.Org)).scalar() == 0


def test_adopts_an_account_that_already_signed_up(db):
    """Must not collide with the case-insensitive unique index on email."""
    existing = models.User(
        email="Admin@Example.COM",
        password_hash=passwords.hash_password("their-own-password"),
    )
    db.add(existing)
    db.flush()

    orgs_svc.bootstrap_from_settings(db, _settings())

    assert db.execute(select(func.count()).select_from(models.User)).scalar() == 1
    org = db.execute(select(models.Org)).scalar_one()
    assert orgs_svc.get_membership(db, org.id, existing.id) is not None
    # Their existing password is left alone.
    assert passwords.verify_password(existing.password_hash, "their-own-password")


def test_a_bad_password_fails_loudly(db):
    """Starting anyway would leave a closed-signup deployment with no way in."""
    with pytest.raises(passwords.PasswordPolicyError):
        orgs_svc.bootstrap_from_settings(
            db, _settings(DATATALK_BOOTSTRAP_ADMIN_PASSWORD="short")
        )
