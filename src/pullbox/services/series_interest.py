"""Explicit Watch/cancel/completion; no polling, fuzzy promotion or provider calls."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.metadata_identity import SeriesExternalIdentity
from pullbox.models.series import Series
from pullbox.models.series_interest import SeriesInterest, SeriesInterestState
from pullbox.services.library_root_management import available_add_series_roots
from pullbox.services.whats_new_actions import WhatsNewSelectionError, validate_release_selection

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.schemas.whats_new import WhatsNewWatchRequest

ACTIVE_STATES = (SeriesInterestState.WATCHING, SeriesInterestState.NEEDS_CONFIRMATION)


def _check_repeat_destination(interest: SeriesInterest, root_id: int) -> SeriesInterest:
    if interest.target_library_root_id != root_id:
        raise WhatsNewSelectionError(
            "This series already watches a different destination. Cancel Watch before changing it."
        )
    return interest


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
