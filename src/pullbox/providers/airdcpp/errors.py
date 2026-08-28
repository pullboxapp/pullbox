"""Secret-safe normalized AirDC++ provider errors."""

from __future__ import annotations


class AirDcppError(Exception):
    """Base class for safe AirDC++ errors suitable for user-facing mapping."""

    code = "airdcpp_error"


class AirDcppAuthenticationError(AirDcppError):
    code = "authentication"

    def __init__(self) -> None:
        super().__init__("AirDC++ authentication failed")


class AirDcppPermissionError(AirDcppError):
    code = "permission"

    def __init__(self, missing_permission: str | None = None) -> None:
        self.missing_permission = missing_permission
        message = "AirDC++ denied the required permission"
        if missing_permission:
            message = f"AirDC++ requires permission: {missing_permission}"
        super().__init__(message)


class AirDcppCompatibilityError(AirDcppError):
    code = "compatibility"


class AirDcppEntityNotFoundError(AirDcppError):
    code = "not_found"

    def __init__(self) -> None:
        super().__init__("The requested AirDC++ entity was not found")


class AirDcppConflictError(AirDcppError):
    code = "conflict"

    def __init__(self) -> None:
        super().__init__("AirDC++ rejected the request state")


class AirDcppRateLimitError(AirDcppError):
    code = "rate_limited"

    def __init__(self, retry_after_seconds: int | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("AirDC++ rate limited the request")


class AirDcppUnavailableError(AirDcppError):
    code = "unavailable"

    def __init__(self) -> None:
        super().__init__("AirDC++ is unavailable")


class AirDcppResponseError(AirDcppError):
    code = "invalid_response"
