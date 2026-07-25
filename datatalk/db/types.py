"""Custom SQLAlchemy column types."""

from __future__ import annotations

from typing import Any

from sqlalchemy import LargeBinary
from sqlalchemy.types import TypeDecorator

from datatalk.security.crypto import decrypt_secret, encrypt_secret


class EncryptedStr(TypeDecorator):
    """A ``str`` attribute stored as an encrypted ``BYTEA`` column.

    Encryption lives in the column type rather than in the store so that it
    cannot be forgotten: there is no code path that writes this attribute
    without encrypting it. The trade-off -- you cannot query on the value -- is
    irrelevant for secrets.
    """

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> bytes | None:
        if value is None:
            return None
        return encrypt_secret(str(value))

    def process_result_value(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return decrypt_secret(bytes(value))
