"""Explicit, cache-bound discovery links; no LOCG provider, fuzzy match or writes to files."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import or_, select

from pullbox.core.metadata_identity import (
    ExactIdentityEvidence,
    ExternalIdentityRef,
    IdentityEvidenceKind,
    IdentityNamespace,
    MetadataEntityKind,
)
from pullbox.core.metadata_identity_events import (
    IdentityEventActor,
    IdentityEventEvidence,
    IdentityEventRequest,
    IdentityEvidenceLocator,
    IdentityEvidenceRecordKind,
)
from pullbox.core.metadata_identity_state import (
    IdentityVerificationAction,
    IdentityVerificationState,
)
from pullbox.models.metadata_identity import SeriesExternalIdentity
from pullbox.models.series import Series
from pullbox.models.whats_new import WhatsNewReleaseCache
from pullbox.schemas.whats_new import WhatsNewSeriesSelection
from pullbox.services.metadata_identity_review import (
    confirm_locg_series_selection,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession


class WhatsNewSelectionError(ValueError):
    """The saved release cannot authorize this selection."""


def positive_id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        return ExternalIdentityRef(
            IdentityNamespace.LOCG, MetadataEntityKind.SERIES, str(value)
        ).external_id
    except ValueError:
        return None


def release_series_id(release: dict[str, object]) -> str | None:
    nested = release.get("series")
    nested_id = positive_id(nested.get("locg_series_id")) if isinstance(nested, dict) else None
    direct_id = positive_id(release.get("locg_series_id"))
    if nested_id and direct_id and nested_id != direct_id:
        raise WhatsNewSelectionError("This release has conflicting series IDs. Recheck releases.")
    return nested_id or direct_id


@dataclass(frozen=True)
class ReleaseSelection:
    selection: WhatsNewSeriesSelection
    locg_series_id: str | None
    title: str
    publisher: str
    year: int | None


async def load_release_selection(
    session: AsyncSession, cache_id: int, release_id: int
) -> ReleaseSelection:
    row = await session.scalar(
        select(WhatsNewReleaseCache)
        .where(WhatsNewReleaseCache.id == cache_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if row is None:
        raise WhatsNewSelectionError("These releases are no longer cached. Reload What's New.")
    payload = row.payload
    groups = [payload.get("issues", [])]
    weeks = payload.get("weeks")
    if isinstance(weeks, list):
        groups.extend(week.get("issues", []) for week in weeks if isinstance(week, dict))
    matches = [
        release
        for group in groups
        if isinstance(group, list)
        for release in group
        if isinstance(release, dict)
        and positive_id(release.get("locg_issue_id")) == str(release_id)
    ]
    if not matches:
        raise WhatsNewSelectionError("This release changed or was removed. Reload What's New.")
    contexts = []
    for release in matches:
        series = release.get("series")
        publisher = release.get("publisher")
        if not isinstance(series, dict) or not isinstance(series.get("title"), str):
            raise WhatsNewSelectionError("This release has no series title. Recheck releases.")
        year = series.get("start_year")
        contexts.append(
            {
                "locg_series_id": release_series_id(release),
                "title": series["title"][:512],
                "publisher": str(publisher.get("name", ""))[:255]
                if isinstance(publisher, dict)
                else "",
                "year": year if type(year) is int and 1 <= year <= 9999 else None,
            }
        )
    if any(context != contexts[0] for context in contexts[1:]):
        raise WhatsNewSelectionError("This release has conflicting series evidence. Reload it.")
    context = contexts[0]
    fingerprint = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    return ReleaseSelection(
        WhatsNewSeriesSelection(cache_id=cache_id, release_id=release_id, fingerprint=fingerprint),
        context["locg_series_id"],
        context["title"],
        context["publisher"],
        context["year"],
    )


async def validate_release_selection(
    session: AsyncSession, selection: WhatsNewSeriesSelection
) -> ReleaseSelection:
    context = await load_release_selection(session, selection.cache_id, selection.release_id)
    if context.selection.fingerprint != selection.fingerprint:
        raise WhatsNewSelectionError("This release changed. Reload What's New before adding it.")
    return context


async def link_confirmed_release(
    session: AsyncSession,
    selection: WhatsNewSeriesSelection,
    series_id: int,
    actor_user_id: int,
) -> None:
    """Called inside the normal Add transaction, after the user's explicit choice."""
    context = await validate_release_selection(session, selection)
    if context.locg_series_id is None:
        return  # A sparse release may add a series, but cannot acquire an invented link.
    claims = (
        await session.scalars(
            select(SeriesExternalIdentity)
            .where(
                SeriesExternalIdentity.identity_namespace == IdentityNamespace.LOCG,
                or_(
                    SeriesExternalIdentity.series_id == series_id,
                    SeriesExternalIdentity.external_id == context.locg_series_id,
                ),
            )
            .with_for_update()
        )
    ).all()
    if claims:
        if len(claims) == 1 and (
            claims[0].series_id == series_id
            and claims[0].external_id == context.locg_series_id
            and claims[0].verification_state is IdentityVerificationState.VERIFIED
        ):
            return
        raise WhatsNewSelectionError(
            "This release already has a different or disputed series link."
        )
    identity = ExternalIdentityRef(
        IdentityNamespace.LOCG, MetadataEntityKind.SERIES, context.locg_series_id
    )
    await confirm_locg_series_selection(
        session,
        IdentityEventRequest(
            uuid4(),
            series_id,
            IdentityVerificationAction.CONFIRM,
            IdentityEventEvidence(
                ExactIdentityEvidence(identity, IdentityEvidenceKind.USER_SELECTION),
                context.selection.fingerprint,
                locator=IdentityEvidenceLocator(IdentityEvidenceRecordKind.SERIES, series_id),
            ),
            actor=IdentityEventActor.USER,
            actor_user_id=actor_user_id,
            review_revision=1,
        ),
    )


async def local_release_series(
    session: AsyncSession, identities: Iterable[str]
) -> dict[str, Series]:
    ids = set(identities)
    if not ids:
        return {}
    rows = await session.execute(
        select(SeriesExternalIdentity.external_id, Series)
        .join(Series, Series.id == SeriesExternalIdentity.series_id)
        .where(
            SeriesExternalIdentity.identity_namespace == IdentityNamespace.LOCG,
            SeriesExternalIdentity.verification_state == IdentityVerificationState.VERIFIED,
            SeriesExternalIdentity.external_id.in_(ids),
        )
    )
    return {external_id: series for external_id, series in rows}
