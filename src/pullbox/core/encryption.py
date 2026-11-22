"""Symmetric encryption for secrets stored in the database.

Uses Fernet (AES-128-CBC + HMAC-SHA256) from the ``cryptography`` library.
The encryption key is deterministically derived from the shared application
secret via PBKDF2-HMAC-SHA256 so that a given secret always produces the same
Fernet key.

Encrypted values are stored with an ``enc:`` prefix so that code can
distinguish encrypted data from legacy plaintext and handle migration
gracefully.

Usage::

    from pullbox.core.encryption import encrypt_secret, decrypt_secret

    ciphertext = encrypt_secret("my-api-key")   # "enc:gAAAAA..."
    plaintext  = decrypt_secret(ciphertext)       # "my-api-key"

    # Plaintext pass-through (pre-migration data):
    decrypt_secret("old-plaintext-key")           # "old-plaintext-key"
"""

import base64
from functools import lru_cache

import structlog
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = structlog.get_logger(__name__)

# Prefix that marks a value as Fernet-encrypted.
_ENC_PREFIX = "enc:"

# Fixed salt — changing this would invalidate all encrypted data.
# Not a security weakness: the secret key provides the entropy.
_SALT = b"pullbox-secret-encryption-salt-v1"


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    """Derive a Fernet instance from the application secret key.

    Cached so derivation only happens once per process.
    """
    from pullbox.core.config_resolver import get_application_secret

    return derive_secret_cipher(get_application_secret())


def derive_secret_cipher(secret: str) -> Fernet:
    """Derive the storage cipher without loading or creating application configuration."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT,
        iterations=480_000,
    )
    derived = kdf.derive(secret.encode())
    fernet_key = base64.urlsafe_b64encode(derived)
    return Fernet(fernet_key)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret string for database storage.

    Returns a string prefixed with ``enc:`` containing the Fernet token.
    Returns an empty string unchanged.
    """
    if not plaintext:
        return plaintext
    # Already encrypted — don't double-encrypt
    if plaintext.startswith(_ENC_PREFIX):
        return plaintext

    token = _get_fernet().encrypt(plaintext.encode())
    return f"{_ENC_PREFIX}{token.decode()}"


def decrypt_secret(stored: str) -> str:
    """Decrypt a secret retrieved from the database.

    If the value has the ``enc:`` prefix it is decrypted.  Otherwise it
    is returned as-is (legacy plaintext).  Returns an empty string unchanged.
    """
    if not stored:
        return stored
    if not stored.startswith(_ENC_PREFIX):
        # Legacy plaintext — return as-is
        return stored

    token = stored[len(_ENC_PREFIX) :].encode()
    try:
        return _get_fernet().decrypt(token).decode()
    except InvalidToken:
        logger.error("decrypt_secret_failed", hint="secret key may have changed")
        raise ValueError(
            "Failed to decrypt secret — has PULLBOX_SECRET_KEY changed? "
            "Encrypted data cannot be recovered with a different key."
        ) from None


def is_encrypted(value: str | None) -> bool:
    """Check whether a stored value is already encrypted."""
    return bool(value and value.startswith(_ENC_PREFIX))
