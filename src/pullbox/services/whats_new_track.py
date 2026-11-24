"""Enable monitoring from a release without adding a series or attaching identities."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select, update

from pullbox.models import Series
from pullbox.models.whats_new import WhatsNewReleaseCache
from pullbox.services.series_service import want_auto_skipped_issues
from pullbox.services.whats_new_actions import (
    WhatsNewSelectionError,
    load_release_selection,
    local_release_series,
)
from pullbox.services.whats_new_cache_service import WhatsNewCacheService
from pullbox.services.whats_new_exact_add import (
    current_release_evidence,
    lock_current_release_feeds,
)
from pullbox.services.whats_new_grab import cache_fingerprint
from pullbox.services.whats_new_library_matches import native_library_matches

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.schemas.whats_new import WhatsNewTrackSelection


async def track_release_series(
    session: AsyncSession, selection: WhatsNewTrackSelection
) -> list[tuple[int, int]]:
    """Caller owns the transaction and publishes wanted events only after commit."""
    cache = WhatsNewCacheService()
    current = await cache.get_latest_current_week(session)
    upcoming = await cache.get_upcoming(session)
    await lock_current_release_feeds(session, current, upcoming)
    expected = selection.expected_series_id
    # Claim the paused-to-tracked transition once. All later failures roll it back.
    changed = await session.scalar(
        update(Series)
        .where(Series.id == expected, Series.monitored.is_(False))
        .values(monitored=True)
        .returning(Series.id)
    )
    if changed is None and not await session.scalar(
        select(Series.monitored).where(Series.id == expected).with_for_update()
    ):
        raise WhatsNewSelectionError("This series is no longer in your library. Reload What's New.")
    selected = await load_release_selection(session, selection.cache_id, selection.release_id)
    row = await session.get(WhatsNewReleaseCache, selection.cache_id)
    locg = selected.locg_series_id
    if not locg or not row or cache_fingerprint(row.payload) != selection.fingerprint:
        raise WhatsNewSelectionError("These releases changed. Reload What's New before tracking.")
    owners = await local_release_series(session, [locg])
    owner = owners.get(locg)
    owner_id = owner.id if owner is not None else None
    if owner_id is None and current and upcoming and row.id in (current.id, upcoming.id):
        evidence = await current_release_evidence([current, upcoming])
        match = (await native_library_matches(session, evidence, {locg})).get(locg)
        owner_id = match.id if match else None
    if owner_id != expected:
        raise WhatsNewSelectionError("The library series match changed. Reload before tracking.")
    return await want_auto_skipped_issues(session, [expected]) if changed is not None else []
