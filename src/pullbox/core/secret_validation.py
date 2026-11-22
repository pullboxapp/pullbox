"""Validation helpers for application signing/encryption secrets."""

from __future__ import annotations

import os

MIN_APPLICATION_SECRET_LENGTH = 32

_WEAK_APPLICATION_SECRETS = frozenset(
    {
        "change-me",
        "changeme",
        "default",
        "password",
        "pullbox",
        "secret",
        "test",
    }
)


class WeakApplicationSecretError(ValueError):
    """Raised when an application secret is not safe for runtime use."""


def _allows_weak_test_secret() -> bool:
    return os.environ.get("PULLBOX_ALLOW_WEAK_SECRET_FOR_TESTS", "").lower() in {
        "1",
        "true",
        "yes",
    }


def validate_application_secret(secret: str, *, allow_test_override: bool = True) -> None:
    """Raise when an application secret is empty, common, or too short."""
    normalized = secret.strip()
    if not normalized:
        raise WeakApplicationSecretError("Application secret key must not be empty.")
    if allow_test_override and _allows_weak_test_secret():
        return
    if len(normalized) < MIN_APPLICATION_SECRET_LENGTH:
        raise WeakApplicationSecretError(
            f"Application secret key must be at least {MIN_APPLICATION_SECRET_LENGTH} characters."
        )
    if normalized.lower() in _WEAK_APPLICATION_SECRETS:
        raise WeakApplicationSecretError("Application secret key uses a known weak value.")
    if len(set(normalized)) < 2:
        raise WeakApplicationSecretError("Application secret key must not repeat one character.")
