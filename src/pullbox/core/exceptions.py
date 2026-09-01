"""
Pullbox exception hierarchy — custom exceptions for all application errors.

All Pullbox exceptions inherit from PullboxError, which carries a
human-readable message, a machine-readable code, and an HTTP status code.
The global exception handler in app.py converts these to JSON or HTML
responses automatically.
"""

from typing import Any


class PullboxError(Exception):
    """Base exception for all Pullbox errors."""

    def __init__(self, message: str, code: str, status_code: int = 500) -> None:
        self.message = message
        self.code = code
        self.status_code = status_code
        super().__init__(message)


class NotFoundError(PullboxError):
    """Raised when a requested resource does not exist."""

    def __init__(self, resource: str, identifier: Any) -> None:
        super().__init__(
            message=f"{resource} with ID {identifier} was not found.",
            code=f"{resource.upper()}_NOT_FOUND",
            status_code=404,
        )


class ValidationError(PullboxError):
    """Raised when input data fails validation."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        self.details = details
        super().__init__(message=message, code="VALIDATION_ERROR", status_code=422)


class ProviderError(PullboxError):
    """Error from an external provider (ComicVine, download client, indexer)."""

    def __init__(self, provider: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.provider = provider
        self.details = details
        super().__init__(
            message=f"Provider '{provider}' error: {message}",
            code="PROVIDER_ERROR",
            status_code=502,
        )


class RateLimitError(ProviderError):
    """Raised when an external provider rate-limits a request."""

    def __init__(self, provider: str, retry_after_seconds: int | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            provider=provider,
            message=f"Rate limited. Retry after {retry_after_seconds}s.",
        )
        self.code = "RATE_LIMITED"
        self.status_code = 429


class LoginRateLimitError(PullboxError):
    """Raised when login attempts exceed the local abuse threshold."""

    def __init__(self, retry_after_seconds: int, message: str) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            message=message,
            code="LOGIN_RATE_LIMITED",
            status_code=429,
        )


class AuthenticationError(PullboxError):
    """Raised when authentication is required or credentials are invalid."""

    def __init__(self, message: str = "Authentication required.") -> None:
        super().__init__(message=message, code="AUTH_REQUIRED", status_code=401)


class ConfigurationError(PullboxError):
    """Raised when the application is misconfigured."""

    def __init__(self, message: str) -> None:
        super().__init__(message=message, code="CONFIG_ERROR", status_code=500)


class ImportDestinationValidationError(ConfigurationError):
    """Fail-closed managed-import destination requiring explicit review."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)
        self.code = "IMPORT_DESTINATION_REVIEW"


class BackupError(PullboxError):
    """Raised when a backup operation fails."""

    def __init__(self, message: str) -> None:
        super().__init__(message=message, code="BACKUP_ERROR", status_code=500)


class MylarReadError(PullboxError):
    """Raised when a Mylar3 database cannot be read."""

    def __init__(self, message: str) -> None:
        super().__init__(message=message, code="MYLAR_READ_ERROR", status_code=400)


class JobCancelledError(Exception):
    """Raised when a background job is cancelled cooperatively."""


class JobPausedError(Exception):
    """Raised when a background job pauses cooperatively at a safe boundary."""


class ImportProviderDegradedError(Exception):
    """Raised when import matching cannot trust an external metadata provider response."""

    def __init__(
        self,
        *,
        provider: str,
        query: str,
        year: int | None,
        attempts: int,
        last_error: str,
    ) -> None:
        self.provider = provider
        self.query = query
        self.year = year
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"{provider} degraded while matching '{query}'"
            + (f" ({year})" if year is not None else "")
        )
