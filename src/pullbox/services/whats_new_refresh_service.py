"""Refresh What's New release cache from pullbox-data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Protocol

from pullbox.config import get_settings
from pullbox.services.series_interest import request_watch_confirmation
from pullbox.services.whats_new_actions import available_release_series_ids
from pullbox.services.whats_new_cache_service import WhatsNewCacheService
from pullbox.services.whats_new_data_client import PullboxDataClientError, WhatsNewDataClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class WhatsNewReleaseClient(Protocol):
    """Subset of pullbox-data release client behavior needed for cache refresh."""

    async def get_current_week(self) -> dict[str, object]: ...

    async def get_upcoming(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class WhatsNewRefreshResult:
    """Summary of a successful What's New cache refresh."""

    current_week_store_date: date
    current_week_count: int
    upcoming_week_count: int
    upcoming_release_count: int


class WhatsNewRefreshService:
    """Fetch upstream release payloads and update the local cache."""

    def __init__(
        self,
        *,
        client: WhatsNewReleaseClient | None = None,
        cache: WhatsNewCacheService | None = None,
    ) -> None:
        self.client = client or WhatsNewDataClient()
        self.cache = cache or WhatsNewCacheService()

    async def refresh(self, session: AsyncSession) -> WhatsNewRefreshResult:
        """Refresh current-week and upcoming cache rows."""

        current_payload = await self.client.get_current_week()
        upcoming_payload = await self.client.get_upcoming()
        store_date = _payload_store_date(current_payload)

        await self.cache.upsert_current_week(
            session,
            store_date=store_date,
            payload=current_payload,
        )
        await self.cache.upsert_upcoming(session, payload=upcoming_payload)

        if get_settings().metadata_whats_new_actions_enabled:
            as_of = date.today()
            available = available_release_series_ids(current_payload, as_of=as_of)
            available.update(available_release_series_ids(upcoming_payload, as_of=as_of))
            await request_watch_confirmation(session, available)

        return WhatsNewRefreshResult(
            current_week_store_date=store_date,
            current_week_count=_count(current_payload.get("count")),
            upcoming_week_count=len(_weeks(upcoming_payload)),
            upcoming_release_count=sum(
                _count(week.get("count")) for week in _weeks(upcoming_payload)
            ),
        )


def _payload_store_date(payload: dict[str, object]) -> date:
    value = payload.get("store_date")
    if not isinstance(value, str):
        msg = "pullbox-data current-week payload did not include a store_date."
        raise PullboxDataClientError(msg)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        msg = "pullbox-data current-week payload included an invalid store_date."
        raise PullboxDataClientError(msg, cause=exc) from exc


def _weeks(payload: dict[str, object]) -> list[dict[str, object]]:
    weeks = payload.get("weeks")
    if not isinstance(weeks, list):
        return []
    return [week for week in weeks if isinstance(week, dict)]


def _count(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return 0
