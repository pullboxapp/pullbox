"""Bounded in-process opaque grants for transient Direct Connect routes."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from pullbox.services.airdcpp_search_types import DcValidatedCandidate


@dataclass(frozen=True, slots=True)
class AirDcppRouteGrant:
    candidate: DcValidatedCandidate
    issue_id: int
    user_id: int
    search_log_id: int | None
    request_key: str
    expires_at: datetime


class AirDcppRouteTokenStore:
    """Hold route details server-side so browser tokens reveal no route data."""

    def __init__(
        self,
        *,
        max_entries: int = 1000,
        ttl_seconds: int = 600,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not 1 <= max_entries <= 10_000 or not 1 <= ttl_seconds <= 3600:
            raise ValueError("Invalid Direct Connect route-token bounds")
        self._max_entries = max_entries
        self._ttl = timedelta(seconds=ttl_seconds)
        self._now = now or (lambda: datetime.now(UTC))
        self._entries: dict[str, AirDcppRouteGrant] = {}

    @property
    def entry_count(self) -> int:
        self._prune()
        return len(self._entries)

    def issue(
        self,
        candidate: DcValidatedCandidate,
        *,
        issue_id: int,
        user_id: int,
        search_log_id: int | None,
    ) -> str:
        if issue_id <= 0 or user_id <= 0:
            raise ValueError("Invalid Direct Connect route ownership")
        self._prune()
        while len(self._entries) >= self._max_entries:
            oldest = next(iter(self._entries))
            self._entries.pop(oldest, None)
        token = secrets.token_urlsafe(32)
        request_key = f"dc-{secrets.token_urlsafe(24)}"
        self._entries[token] = AirDcppRouteGrant(
            candidate=candidate,
            issue_id=issue_id,
            user_id=user_id,
            search_log_id=search_log_id,
            request_key=request_key,
            expires_at=self._now() + self._ttl,
        )
        return token

    def resolve(self, token: str, *, issue_id: int, user_id: int) -> AirDcppRouteGrant:
        self._prune()
        grant = self._entries.get(token)
        if grant is None or grant.issue_id != issue_id or grant.user_id != user_id:
            raise ValueError("The Direct Connect search route is unavailable; search again.")
        return grant

    def _prune(self) -> None:
        now = self._now()
        expired = [token for token, grant in self._entries.items() if grant.expires_at <= now]
        for token in expired:
            self._entries.pop(token, None)


_route_token_store = AirDcppRouteTokenStore()


def get_airdcpp_route_token_store() -> AirDcppRouteTokenStore:
    return _route_token_store
