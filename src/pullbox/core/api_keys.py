"""API key helpers shared by auth schemas and services."""

from __future__ import annotations

import hashlib
import hmac

from pullbox.core.config_resolver import get_application_secret

API_KEY_HASH_PREFIX = "pb_kh2_"
API_KEY_PREFIX = "pb_k1_"
API_KEY_RANDOM_HEX_CHARS = 64
API_KEY_LENGTH = len(API_KEY_PREFIX) + API_KEY_RANDOM_HEX_CHARS
MAX_API_KEY_NAME_LENGTH = 100


def hash_api_key(raw_key: str) -> str:
    """Return the database hash for a raw API key."""
    digest = hmac.new(
        get_application_secret().encode("utf-8"),
        raw_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{API_KEY_HASH_PREFIX}{digest}"


def legacy_hash_api_key(raw_key: str) -> str:
    """Return the legacy unpeppered API-key hash for compatibility upgrades."""
    # Legacy rows from pre-public builds used a deterministic SHA-256 lookup hash.
    # Keep this only for one-time validation and upgrade to the HMAC form.
    # codeql[py/weak-sensitive-data-hashing]
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def api_key_hash_candidates(raw_key: str) -> tuple[str, ...]:
    """Return lookup hashes in preferred order for an API key."""
    current_hash = hash_api_key(raw_key)
    legacy_hash = legacy_hash_api_key(raw_key)
    return (current_hash, legacy_hash)


def is_legacy_api_key_hash(key_hash: str) -> bool:
    """Return whether a stored API-key hash uses the legacy format."""
    return not key_hash.startswith(API_KEY_HASH_PREFIX)


def is_well_formed_api_key(raw_key: str) -> bool:
    """Return True when a key has the expected Pullbox API-key envelope."""
    return raw_key.startswith(API_KEY_PREFIX) and len(raw_key) == API_KEY_LENGTH


def normalize_api_key_name(name: str) -> str:
    """Trim and collapse whitespace in user-facing API key names."""
    normalized = " ".join(name.split())
    if not normalized:
        msg = "API key name must not be blank."
        raise ValueError(msg)
    if len(normalized) > MAX_API_KEY_NAME_LENGTH:
        msg = f"API key name must be at most {MAX_API_KEY_NAME_LENGTH} characters."
        raise ValueError(msg)
    return normalized
