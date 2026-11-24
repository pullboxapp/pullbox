"""Explicit Watch/cancel/completion; no polling, fuzzy promotion or provider calls."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryRoot
from pullbox.models.metadata_identity import SeriesExternalIdentity
from pullbox.models.series import Series
from pullbox.models.series_interest import SeriesInterest, SeriesInterestState
from pullbox.services.library_root_management import available_add_series_roots
from pullbox.services.whats_new_actions import (
    ReleaseSelection,
    WhatsNewSelectionError,
    load_release_selection,
    validate_release_selection,
    watched_release_id,
)
from pullbox.services.whats_new_cache_service import WhatsNewCacheService

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.schemas.whats_new import WhatsNewWatchRequest

ACTIVE_STATES = (SeriesInterestState.WATCHING, SeriesInterestState.NEEDS_CONFIRMATION)


async def resolve_watched_release(session: AsyncSession, interest_id: int) -> ReleaseSelection:
    """Resolve one clicked Watch from the two live feeds, never historical title matches."""
    interest = await session.get(SeriesInterest, interest_id)
    if interest is None or interest.state not in ACTIVE_STATES:
        raise WhatsNewSelectionError("This Watch is no longer active. Reload the Pull List.")
    cache = WhatsNewCacheService()
    readers = (
        (cache.get_upcoming, cache.get_latest_current_week)
        if interest.state is SeriesInterestState.WATCHING
        else (cache.get_latest_current_week, cache.get_upcoming)
    )
    for read in readers:
        row = await read(session)
        if row is None or cache.is_stale(row) or row.fetched_at > datetime.now(UTC):
            continue
        release_id = watched_release_id(row.payload, interest.source_series_id)
        if release_id is None:
            continue
        context = await load_release_selection(session, row.id, release_id)
        if (
            context.locg_series_id != interest.source_series_id
            or cache.is_stale(row)
            or row.fetched_at > datetime.now(UTC)
        ):
            raise WhatsNewSelectionError("This release changed. Refresh What's New and try again.")
        return context
    raise WhatsNewSelectionError(
        "No fresh release matches this Watch. Refresh What's New, then try Find & Add again."
    )


def _check_repeat_destination(interest: SeriesInterest, root_id: int) -> SeriesInterest:
    if interest.target_library_root_id != root_id:
        raise WhatsNewSelectionError(
            "This series already watches a different destination. Cancel Watch before changing it."
        )
    return interest


async def unavailable_watch_destination(
    session: AsyncSession, interest: SeriesInterest
) -> dict[str, str | bool | int]:
    """Explain a failed destination check without choosing or changing a root."""
    root_id = interest.target_library_root_id
    root = await session.get(LibraryRoot, root_id) if root_id is not None else None
    if root is None:
        return {
            "removed": True,
            "message": (
                "The library saved for this Watch was removed. Close this window and choose "
                "Cancel Watch. Then use Find & Add in What's New to add to your current library."
            ),
        }
    if not root.enabled:
        reason = "is disabled. Enable it in Library roots"
    elif not root.allow_managed_writes:
        reason = (
            "does not allow managed writes. If this is the intended destination, "
            'turn on "Allow managed files" in Library roots'
        )
    else:
        reason = "is unavailable for writing. Check its folder mount and write permissions"
    return {
        "removed": False,
        "watch_id": interest.id,
        "library_root_id": root.id,
        "message": f'The library saved for this Watch, "{root.name}", {reason}, then recheck.',
    }


async def find_watch(session: AsyncSession, identity: str) -> SeriesInterest | None:
    rows = await session.scalars(
        select(SeriesInterest)
        .where(
            SeriesInterest.source_namespace == "locg",
            SeriesInterest.source_series_id == identity,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    return rows.one_or_none()


async def save_watch(
    session: AsyncSession, request: WhatsNewWatchRequest, actor: int
) -> SeriesInterest:
    context = await validate_release_selection(session, request.selection)
    if not context.locg_series_id or not context.store_date or context.store_date <= date.today():
        raise WhatsNewSelectionError(
            "Watch needs a future release with a stable series ID. Use Find & Add instead."
        )
    owner = await session.scalar(
        select(SeriesExternalIdentity).where(
            SeriesExternalIdentity.identity_namespace == IdentityNamespace.LOCG,
            SeriesExternalIdentity.external_id == context.locg_series_id,
        )
    )
    if owner:
        raise WhatsNewSelectionError(
            "This series already has a local or disputed link. Reload its release state."
        )
    roots = await available_add_series_roots(session, require_default=False)
    default_id = roots[0]["id"] if roots and roots[0]["is_default_managed_destination"] else None
    root_id = request.library_root_id or default_id
    if not root_id or not any(root["id"] == root_id for root in roots):
        raise WhatsNewSelectionError(
            "Choose an available managed library default in Media Management, then retry Watch."
        )
    interest = await find_watch(session, context.locg_series_id)
    if interest is not None and interest.state in ACTIVE_STATES:
        return _check_repeat_destination(interest, root_id)
    if interest is None:
        try:
            async with session.begin_nested():
                interest = SeriesInterest(
                    source_namespace="locg",
                    source_series_id=context.locg_series_id,
                    title_snapshot=context.title,
                    publisher_snapshot=context.publisher,
                    target_library_root_id=root_id,
                    last_actor_user_id=actor,
                )
                session.add(interest)
                await session.flush()
        except IntegrityError:
            interest = await find_watch(session, context.locg_series_id)
            if interest is None:
                raise
            if interest.state in ACTIVE_STATES:
                return _check_repeat_destination(interest, root_id)
    interest.state = SeriesInterestState.WATCHING
    interest.resolved_series_id = None
    interest.target_library_root_id = root_id
    interest.last_actor_user_id = actor
    interest.title_snapshot = context.title
    interest.publisher_snapshot = context.publisher
    interest.year_snapshot = context.year
    interest.next_known_release_date = context.store_date
    await session.flush()
    return interest


async def cancel_watch(session: AsyncSession, interest_id: int, actor: int) -> SeriesInterest:
    interest = await session.scalar(
        select(SeriesInterest)
        .where(SeriesInterest.id == interest_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if interest is None:
        raise WhatsNewSelectionError("This Watch no longer exists. Reload the list.")
    if interest.state is SeriesInterestState.PROMOTED:
        raise WhatsNewSelectionError(
            "This Watch was added already. Manage monitoring on its series instead."
        )
    interest.state = SeriesInterestState.CANCELLED
    interest.last_actor_user_id = actor
    await session.flush()
    return interest


async def active_watches(
    session: AsyncSession, identities: Iterable[str] | None = None
) -> list[SeriesInterest]:
    query = select(SeriesInterest).where(SeriesInterest.state.in_(ACTIVE_STATES))
    if identities is not None:
        query = query.where(SeriesInterest.source_series_id.in_(set(identities)))
    return list(
        (
            await session.scalars(query.order_by(SeriesInterest.title_snapshot, SeriesInterest.id))
        ).all()
    )


async def request_watch_confirmation(session: AsyncSession, identities: Iterable[str]) -> None:
    """Commit fresh availability with the cache, without reviving cancelled intent."""
    ids = set(identities)
    if not ids:
        return
    await session.execute(
        update(SeriesInterest)
        .where(
            SeriesInterest.source_namespace == "locg",
            SeriesInterest.source_series_id.in_(ids),
            SeriesInterest.state == SeriesInterestState.WATCHING,
        )
        .values(state=SeriesInterestState.NEEDS_CONFIRMATION)
    )


async def reconcile_tracked_watches(
    session: AsyncSession, evidence: dict[str, dict[IdentityNamespace, str]]
) -> None:
    """Settle existing tracked owners only. No provider, monitoring or issue mutations."""
    if not evidence:
        return
    link = SeriesExternalIdentity
    owners = (
        select(SeriesInterest.id, SeriesInterest.source_series_id, Series.id, LibraryRoot.id)
        .select_from(SeriesInterest)
        .join(link, link.external_id == SeriesInterest.source_series_id)
        .join(Series, Series.id == link.series_id)
        .join(LibraryRoot, LibraryRoot.id == SeriesInterest.target_library_root_id)
        .where(
            SeriesInterest.state.in_(ACTIVE_STATES),
            SeriesInterest.source_namespace == "locg",
            link.identity_namespace == IdentityNamespace.LOCG,
            link.verification_state == IdentityVerificationState.VERIFIED,
            Series.monitored.is_(True),
            Series.preferred_library_root_id == LibraryRoot.id,
            LibraryRoot.enabled.is_(True),
            LibraryRoot.allow_managed_writes.is_(True),
        )
    )
    matches = [r for r in await session.execute(owners) if r[1] in evidence]
    if not matches:
        return
    paths = {
        r["id"]: r["path"] for r in await available_add_series_roots(session, require_default=False)
    }
    for watch_id, identity, series_id, root_id in matches:
        if root_id not in paths:
            continue
        # Recheck the entire binding at the write, including cancellation and root changes.
        eligible = owners.where(
            SeriesInterest.id == watch_id,
            SeriesInterest.source_series_id == identity,
            Series.id == series_id,
            LibraryRoot.id == root_id,
            LibraryRoot.path == paths[root_id],
        )
        for namespace, value in evidence[identity].items():
            eligible = eligible.where(
                select(link.id)
                .where(
                    link.series_id == series_id,
                    link.identity_namespace == namespace,
                    link.external_id == value,
                    link.verification_state == IdentityVerificationState.VERIFIED,
                )
                .correlate(None)
                .exists()
            )
            if namespace == IdentityNamespace.COMICVINE:
                eligible = eligible.where(
                    Series.comicvine_id.is_(None) | (Series.comicvine_id == int(value))
                )
        await session.execute(
            update(SeriesInterest)
            .where(
                SeriesInterest.id == watch_id,
                SeriesInterest.state.in_(ACTIVE_STATES),
                SeriesInterest.target_library_root_id == root_id,
                eligible.correlate(None).exists(),
            )
            .values(state=SeriesInterestState.PROMOTED, resolved_series_id=series_id)
            .execution_options(synchronize_session=False)
        )


async def promote_confirmed_watch(
    session: AsyncSession, identity: str, series_id: int, actor: int
) -> None:
    interest = await find_watch(session, identity)
    if interest is None or interest.state not in ACTIVE_STATES:
        return
    roots = await available_add_series_roots(session, require_default=False)
    series = await session.get(Series, series_id)
    if (
        series is None
        or interest.target_library_root_id is None
        or not any(root["id"] == interest.target_library_root_id for root in roots)
        or series.preferred_library_root_id != interest.target_library_root_id
    ):
        raise WhatsNewSelectionError(
            "The watched destination changed or is unavailable. "
            "Cancel Watch or restore its library root before adding."
        )
    # This is inside Add's transaction; its post-commit event sees monitored/WANTED state.
    series.monitored = True
    issues = (
        await session.scalars(
            select(Issue).where(
                Issue.series_id == series_id,
                Issue.status == IssueStatus.SKIPPED,
                Issue.manual_skip.is_(False),
            )
        )
    ).all()
    for issue in issues:
        issue.status = IssueStatus.WANTED
    interest.state = SeriesInterestState.PROMOTED
    interest.resolved_series_id = series_id
    interest.last_actor_user_id = actor
    await session.flush()


def watch_response(interest: SeriesInterest) -> dict[str, object]:
    return {
        "id": interest.id,
        "state": interest.state.value,
        "locg_series_id": interest.source_series_id,
        "title": interest.title_snapshot,
        "library_root_id": interest.target_library_root_id,
        "resolved_series_id": interest.resolved_series_id,
    }
