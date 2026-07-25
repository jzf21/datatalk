"""Symmetric encryption for secrets stored at rest.

Used for per-org ClickHouse passwords, which must be *decryptable* (we hand them
to the driver). User passwords are a different problem entirely -- those are
one-way argon2 hashes, see :mod:`datatalk.auth.passwords`.

``MultiFernet`` gives key rotation for three extra lines: the first key in
``DATATALK_SECRET_KEY`` encrypts, every key decrypts. Rotate by prepending a new
key and leaving the old one in place until everything is re-encrypted.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from datatalk.config import get_settings

GENERATE_HINT = (
    'python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)


class MissingSecretKeyError(RuntimeError):
    """DATATALK_SECRET_KEY is unset. Never silently fall back to plaintext."""


class SecretDecryptionError(RuntimeError):
    """A stored secret could not be decrypted with any configured key."""


@lru_cache(maxsize=1)
def get_fernet() -> MultiFernet:
    raw = get_settings().datatalk_secret_key
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise MissingSecretKeyError(
            "DATATALK_SECRET_KEY is not set, so per-org ClickHouse passwords "
            f"cannot be encrypted. Generate one with:\n  {GENERATE_HINT}"
        )
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except (ValueError, TypeError) as exc:
        raise MissingSecretKeyError(
            "DATATALK_SECRET_KEY is not a valid Fernet key (expected urlsafe "
            f"base64, 32 bytes). Generate one with:\n  {GENERATE_HINT}"
        ) from exc


def encrypt_secret(plaintext: str) -> bytes:
    return get_fernet().encrypt(plaintext.encode())


def decrypt_secret(token: bytes) -> str:
    try:
        return get_fernet().decrypt(bytes(token)).decode()
    except InvalidToken as exc:
        raise SecretDecryptionError(
            "Stored secret could not be decrypted. DATATALK_SECRET_KEY has "
            "probably changed; the affected orgs must re-enter their "
            "ClickHouse credentials."
        ) from exc
