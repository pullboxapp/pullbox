"""Finish published Watches through normal Add, outside the feed-cache write transaction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import structlog
from sqlalchemy import select, tuple_, update

from pullbox.config import get_settings
from pullbox.core.events import get_event_bus
from pullbox.core.exceptions import ValidationError
from pullbox.core.metadata_identity import (
    ExactIdentityEvidence,
    ExternalIdentityRef,
    IdentityEvidenceKind,
    IdentityNamespace,
    MetadataEntityKind,
)
from pullbox.core.metadata_identity_events import (
    IdentityEventEvidence,
    IdentityEventRequest,
    IdentityEvidenceLocator,
    IdentityEvidenceRecordKind,
)
from pullbox.core.metadata_identity_state import (
    IdentityReviewRequiredError,
    IdentityVerificationAction,
    IdentityVerificationState,
)
from pullbox.models import LibraryRoot, SeriesExternalIdentity, SeriesInterest, User
from pullbox.models.series_interest import SeriesInterestState as State
from pullbox.models.whats_new import WhatsNewReleaseCache
from pullbox.schemas.metadata_sources import SourceCapability, SourceStatus
from pullbox.services.library_root_management import validate_managed_library_root
from pullbox.services.metadata_catalog_review import approve_catalog_review
from pullbox.services.metadata_discovery import MetadataSourceRegistry
from pullbox.services.metadata_identity_attachment import (
    IdentityAttachmentConflictError,
    attach_verified_identities,
)
from pullbox.services.metadata_series_add import source_series_add_transaction
from pullbox.services.metadata_series_adoption import (
    SeriesAdoptionError,
    SourceSeriesBundle,
    fetch_source_series_bundle,
)
from pullbox.services.metadata_sources import load_source_runtime
from pullbox.services.series_interest import ACTIVE_STATES
from pullbox.services.whats_new_cache_service import upcoming_cache_key

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from sqlalchemy.sql.elements import ColumnElement

logger = structlog.get_logger(__name__)
_RETRY = {SourceStatus.TIMEOUT, SourceStatus.UNAVAILABLE, SourceStatus.RATE_LIMITED}


@dataclass(frozen=True)
class WatchAdd:
    watch_id: int
    locg: str
    root_id: int
    root_path: str
    actor: int
    claims: dict[IdentityNamespace, str]
    observed: date
    refresh_time: datetime

    def current(self) -> tuple[ColumnElement[bool], ...]:
        return (
            SeriesInterest.id == self.watch_id,
            SeriesInterest.source_series_id == self.locg,
            SeriesInterest.state.in_(ACTIVE_STATES),
            SeriesInterest.target_library_root_id == self.root_id,
            SeriesInterest.last_actor_user_id == self.actor,
            select(WhatsNewReleaseCache.id)
            .where(
                WhatsNewReleaseCache.cache_key == upcoming_cache_key(),
                WhatsNewReleaseCache.fetched_at == self.refresh_time,
            )
            .exists(),
        )


async def pending_adds(
    session: AsyncSession,
    evidence: dict[str, dict[IdentityNamespace, str]],
    observed: date,
    refresh_time: datetime,
    *,
    interrupted_only: bool = False,
) -> tuple[WatchAdd, ...]:
    if not any(evidence.values()):
        return ()
    owners = select(SeriesExternalIdentity.id).where(
        SeriesExternalIdentity.identity_namespace == IdentityNamespace.LOCG,
        SeriesExternalIdentity.external_id == SeriesInterest.source_series_id,
    )
    watches = await session.execute(
        select(SeriesInterest, LibraryRoot.path)
        .join(LibraryRoot, LibraryRoot.id == SeriesInterest.target_library_root_id)
        .join(User, User.id == SeriesInterest.last_actor_user_id)
        .where(
            SeriesInterest.state == State.WATCHING
            if interrupted_only
            else SeriesInterest.state.in_(ACTIVE_STATES),
            User.is_active.is_(True),
            LibraryRoot.enabled.is_(True),
            LibraryRoot.allow_managed_writes.is_(True),
            ~owners.exists(),
        )
    )
    jobs = tuple(
        WatchAdd(
            w.id,
            w.source_series_id,
            w.target_library_root_id,
            path,
            w.last_actor_user_id,
            claims,
            observed,
            refresh_time,
        )
        for w, path in watches
        if (claims := evidence.get(w.source_series_id))
        and w.last_actor_user_id is not None
        and w.target_library_root_id is not None
    )
    if jobs and not interrupted_only:
        # The cache commit is durable intent; startup can recover before the first Add.
        await session.execute(
            update(SeriesInterest)
            .where(
                SeriesInterest.id.in_([job.watch_id for job in jobs]),
                SeriesInterest.state == State.NEEDS_CONFIRMATION,
            )
            .values(state=State.WATCHING)
        )
    return jobs


async def fetch_published_catalog(
    session: AsyncSession, locg: str, claims: dict[IdentityNamespace, str]
) -> SourceSeriesBundle:
    """Require one complete configured catalog to corroborate every published claim."""
    gcd = get_settings().metadata_gcd_api_v2_enabled
    registry = MetadataSourceRegistry(
        await load_source_runtime(session, gcd_api_enabled=gcd),
        gcd_api_enabled=gcd,
        revalidate_reads=True,
    )
    await session.rollback()
    retry = False
    for source in registry.ordered_sources():
        identity = claims.get(source.identity_namespace)
        if not identity or registry.source_availability(source, SourceCapability.ISSUE_LIST):
            continue
        try:
            bundle = await fetch_source_series_bundle(
                registry, source, identity, source_revision=registry.runtime[source].policy.revision
            )
        except SeriesAdoptionError as exc:
            if exc.status in _RETRY:
                retry = True
                continue
            if exc.status in {
                SourceStatus.NOT_FOUND,
                SourceStatus.UNCONFIGURED,
                SourceStatus.EMPTY,
            }:
                continue
            raise
        native = bundle.series
        verified = {ref.namespace: ref.external_id for ref in native.cross_identities}
        verified[native.identity_namespace] = native.external_id
        if (
            not bundle.issues
            or any(ns in verified and verified[ns] != value for ns, value in claims.items())
            or verified.get(IdentityNamespace.LOCG, locg) != locg
        ):
            raise SeriesAdoptionError(
                "The published series and provider catalog need confirmation."
            )
        if not claims.keys() <= verified.keys():
            continue  # A lower-priority source may corroborate every supplied ID.
        return approve_catalog_review(bundle, None)
    raise SeriesAdoptionError(
        "No available exact catalog.", status=SourceStatus.UNAVAILABLE if retry else None
    )


async def _add(session: AsyncSession, job: WatchAdd) -> None:
    if (
        not get_settings().metadata_whats_new_actions_enabled
        or datetime.now(UTC).date() != job.observed
        or not await session.scalar(select(SeriesInterest.id).where(*job.current()))
    ):
        return
    bundle = await fetch_published_catalog(session, job.locg, job.claims)
    await session.rollback()

    async def guard() -> None:
        if (
            not get_settings().metadata_whats_new_actions_enabled
            or datetime.now(UTC).date() != job.observed
        ):
            raise SeriesAdoptionError("The published Watch changed.")
        current = await session.scalar(select(SeriesInterest.id).where(*job.current()))
        identities = {
            (bundle.series.identity_namespace, bundle.series.external_id),
            (IdentityNamespace.LOCG, job.locg),
            *((ref.namespace, ref.external_id) for ref in bundle.series.cross_identities),
        }
        stale = await session.scalar(
            select(SeriesExternalIdentity.id)
            .where(
                tuple_(
                    SeriesExternalIdentity.identity_namespace, SeriesExternalIdentity.external_id
                ).in_(identities),
                SeriesExternalIdentity.verification_state != IdentityVerificationState.VERIFIED,
            )
            .limit(1)
        )
        root = await session.scalar(
            select(LibraryRoot)
            .where(
                LibraryRoot.id == job.root_id,
                LibraryRoot.path == job.root_path,
                LibraryRoot.enabled.is_(True),
                LibraryRoot.allow_managed_writes.is_(True),
            )
            .with_for_update(read=True)
        )
        if (
            not current
            or stale
            or not root
            or not await session.scalar(
                select(User.id).where(User.id == job.actor, User.is_active.is_(True))
            )
        ):
            raise SeriesAdoptionError("The Watch or its destination changed.")

    async with source_series_add_transaction(
        session,
        bundle,
        library_root_id=job.root_id,
        search_on_add=True,
        event_bus=get_event_bus(),
        before_adopt=guard,
    ) as result:
        if not result.series.monitored or result.series.preferred_library_root_id != job.root_id:
            raise SeriesAdoptionError("The existing series needs confirmation.")
        if not result.created:
            root = await session.get(LibraryRoot, job.root_id)
            if root is None:
                raise SeriesAdoptionError("The watched destination no longer exists.")
            await validate_managed_library_root(root)
        native = bundle.series
        kind = MetadataEntityKind.SERIES
        revision = hashlib.sha256(
            json.dumps(
                [job.locg, job.claims, job.observed.isoformat(), bundle.source_revision],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        await attach_verified_identities(
            session,
            [
                IdentityEventRequest(
                    uuid4(),
                    result.series.id,
                    IdentityVerificationAction.VERIFY,
                    IdentityEventEvidence(
                        ExactIdentityEvidence(
                            ExternalIdentityRef(IdentityNamespace.LOCG, kind, job.locg),
                            IdentityEvidenceKind.PROVIDER_CROSSWALK,
                        ),
                        revision,
                        locator=IdentityEvidenceLocator(
                            IdentityEvidenceRecordKind.RELEASE_WATCH, job.watch_id
                        ),
                        source_identity=ExternalIdentityRef(
                            native.identity_namespace, kind, native.external_id
                        ),
                    ),
                )
            ],
            require_current_ownership=True,
        )
        # Lock order matches interactive Add: adopt/link the series, then claim the Watch.
        # Cancellation, newer feeds and concurrent completions roll this entire Add back.
        claimed = await session.scalar(
            update(SeriesInterest)
            .where(*job.current())
            .values(state=State.PROMOTED, resolved_series_id=result.series.id)
            .returning(SeriesInterest.id)
        )
        if claimed is None:
            raise SeriesAdoptionError("The Watch changed before the series was added.")


async def finish_watch_adds(
    factory: async_sessionmaker[AsyncSession], jobs: tuple[WatchAdd, ...]
) -> None:
    for job in jobs:
        async with factory() as session:
            try:
                await _add(session, job)
            except (
                SeriesAdoptionError,
                IdentityAttachmentConflictError,
                IdentityReviewRequiredError,
                ValidationError,
            ) as exc:
                await session.rollback()
                if not get_settings().metadata_whats_new_actions_enabled:
                    continue
                retry = isinstance(exc, SeriesAdoptionError) and exc.status in _RETRY
                await session.execute(
                    update(SeriesInterest)
                    .where(*job.current())
                    .values(state=State.WATCHING if retry else State.NEEDS_CONFIRMATION)
                )
                await session.commit()
                logger.info("whats_new_watch_add_deferred", watch_id=job.watch_id, retryable=retry)
