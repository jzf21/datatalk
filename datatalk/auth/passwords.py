"""Password hashing (argon2id).

argon2-cffi directly rather than passlib: passlib's last release was 2020 and it
breaks against bcrypt>=4.1. bcrypt itself silently truncates at 72 bytes, which
argon2id does not.

Hashing here is intentionally expensive (~50-80ms, 64 MiB). Two consequences:

* **Auth endpoints must be ``def``, never ``async def``**, so FastAPI runs them
  in the threadpool. On the event loop this would freeze the whole app for the
  duration of every login attempt.
* Tests must call :func:`use_fast_params_for_tests`, or a suite with dozens of
  logins crawls.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# OWASP baseline: m=64 MiB, t=2, p=1.
_HASHER = PasswordHasher(time_cost=2, memory_cost=64 * 1024, parallelism=1)

# Verified against unknown emails so a login attempt costs the same whether or
# not the account exists -- otherwise response time leaks account existence.
_DUMMY_HASH = _HASHER.hash("dummy-password-for-timing-equalization")

MIN_PASSWORD_LENGTH = 10
# Cap before hashing: argon2 reads the whole input, so an unbounded password is
# a trivial CPU/memory DoS.
MAX_PASSWORD_BYTES = 1024


class PasswordPolicyError(ValueError):
    """The supplied password fails policy (too short, or absurdly long)."""


def use_fast_params_for_tests() -> None:
    """Drop to deliberately weak parameters. Never call this in production."""
    global _HASHER, _DUMMY_HASH
    _HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    _DUMMY_HASH = _HASHER.hash("dummy-password-for-timing-equalization")


def validate_password(password: str) -> None:
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError("Password is too long.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )


def hash_password(password: str) -> str:
    validate_password(password)
    return _HASHER.hash(password)


def _safe_verify(stored_hash: str, password: str) -> bool:
    try:
        return _HASHER.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def verify_password(stored_hash: str | None, password: str) -> bool:
    """Check a password, burning equivalent time when the user doesn't exist."""
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        return False
    if stored_hash is None:
        _safe_verify(_DUMMY_HASH, password)  # constant-ish time
        return False
    return _safe_verify(stored_hash, password)


def needs_rehash(stored_hash: str) -> bool:
    """True when the hash predates the current parameters."""
    try:
        return _HASHER.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True
