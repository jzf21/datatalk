"""Secret encryption: round-trip, rotation, and fail-closed behavior."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from datatalk.config import get_settings
from datatalk.security import crypto

KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()


@pytest.fixture
def with_key(monkeypatch):
    """Install one or more Fernet keys and clear the cached MultiFernet."""

    def _install(*keys: str):
        monkeypatch.setenv("DATATALK_SECRET_KEY", ",".join(keys))
        get_settings.cache_clear()
        crypto.get_fernet.cache_clear()

    yield _install
    get_settings.cache_clear()
    crypto.get_fernet.cache_clear()


def test_round_trip(with_key):
    with_key(KEY_A)
    token = crypto.encrypt_secret("hunter2")
    assert isinstance(token, bytes)
    assert b"hunter2" not in token  # actually encrypted, not encoded
    assert crypto.decrypt_secret(token) == "hunter2"


def test_ciphertext_differs_each_time(with_key):
    """Fernet includes a random IV, so equal plaintexts must not collide."""
    with_key(KEY_A)
    assert crypto.encrypt_secret("same") != crypto.encrypt_secret("same")


def test_unicode_and_empty_round_trip(with_key):
    with_key(KEY_A)
    for value in ["", "pä$$ word ✓", "x" * 4096]:
        assert crypto.decrypt_secret(crypto.encrypt_secret(value)) == value


def test_missing_key_raises_rather_than_falling_back(with_key):
    """Never silently store a secret in plaintext."""
    with_key("")
    with pytest.raises(crypto.MissingSecretKeyError, match="DATATALK_SECRET_KEY"):
        crypto.encrypt_secret("hunter2")


def test_malformed_key_raises_with_a_hint(with_key):
    with_key("not-a-valid-fernet-key")
    with pytest.raises(crypto.MissingSecretKeyError, match="valid Fernet key"):
        crypto.encrypt_secret("hunter2")


def test_rotation_decrypts_old_tokens(with_key):
    """The rotation story: prepend a new key, keep the old one to decrypt."""
    with_key(KEY_A)
    old_token = crypto.encrypt_secret("legacy-password")

    with_key(KEY_B, KEY_A)  # B now encrypts; A still decrypts
    assert crypto.decrypt_secret(old_token) == "legacy-password"

    new_token = crypto.encrypt_secret("new-password")
    with_key(KEY_B)  # drop A once everything is re-encrypted
    assert crypto.decrypt_secret(new_token) == "new-password"


def test_unknown_key_raises_actionable_error(with_key):
    with_key(KEY_A)
    token = crypto.encrypt_secret("hunter2")

    with_key(KEY_B)  # A is gone entirely
    with pytest.raises(crypto.SecretDecryptionError, match="re-enter"):
        crypto.decrypt_secret(token)


def test_encrypted_column_type_never_writes_plaintext(with_key):
    """The TypeDecorator is what makes encryption unforgettable."""
    from datatalk.db.types import EncryptedStr

    with_key(KEY_A)
    col = EncryptedStr()

    stored = col.process_bind_param("s3cret", None)
    assert b"s3cret" not in stored
    assert col.process_result_value(stored, None) == "s3cret"

    assert col.process_bind_param(None, None) is None
    assert col.process_result_value(None, None) is None
