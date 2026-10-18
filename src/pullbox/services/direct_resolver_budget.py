"""Keep provider resolver fallbacks within one bounded operation deadline."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from pullbox.providers.direct.client import DirectProviderClientError
from pullbox.providers.direct.contract import DirectDeadlineRequest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = structlog.get_logger(__name__)


class ProviderResolverBudget:
    def __init__(self, deadline: datetime, *, now: Callable[[], datetime]) -> None:
        self.deadline = deadline
        self.now = now
        self.expires_at = time.monotonic() + max(0.0, (deadline - now()).total_seconds())

    async def run[RequestT: DirectDeadlineRequest, ResponseT](
        self,
        request: RequestT,
        invoke: Callable[[RequestT], Awaitable[ResponseT]],
        *,
        remaining_resolvers: int,
    ) -> ResponseT:
        remaining = min(
            (self.deadline - self.now()).total_seconds(),
            self.expires_at - time.monotonic(),
        )
        if remaining <= 0:
            raise DirectProviderClientError(
                "provider_timed_out", "The provider operation deadline expired.", retryable=True
            )
        profile = request.resolver_profile
        seconds = remaining
        if profile is not None:
            # Reserve an equal share for each untried resolver; unused time carries forward.
            seconds = min(profile.timeout_seconds, remaining / max(1, remaining_resolvers))
            profile = profile.model_copy(update={"timeout_seconds": seconds})
            logger.info(
                "direct_provider_resolver_budget",
                request_id=str(request.request_id),
                timeout_seconds=round(seconds, 3),
                remaining_resolvers=remaining_resolvers,
            )
        bounded = request.model_copy(
            update={
                "deadline": self.deadline - timedelta(seconds=remaining - seconds),
                "resolver_profile": profile,
            }
        )
        try:
            async with asyncio.timeout(seconds):
                return await invoke(bounded)
        except TimeoutError as exc:
            logger.warning(
                "direct_provider_attempt_timed_out",
                request_id=str(request.request_id),
                using_resolver=profile is not None,
                timeout_seconds=round(seconds, 3),
            )
            raise DirectProviderClientError(
                "provider_timed_out", "The provider attempt timed out.", retryable=True
            ) from exc


def resolver_retry_allowed(error: DirectProviderClientError, *, using_resolver: bool) -> bool:
    return (
        error.code == "browser_challenge_required"
        or error.code.startswith("resolver_")
        or (using_resolver and error.code == "provider_timed_out")
    )
