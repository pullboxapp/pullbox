"""Operator-initiated Add from published release evidence, without a provider search."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, tuple_

from pullbox.config import get_settings
from pullbox.core.events import get_event_bus
from pullbox.core.library_policy import load_search_on_add_default
from pullbox.core.metadata_identity import IdentityNamespace, MetadataSource
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models import LibraryRoot, Series, SeriesExternalIdentity
from pullbox.models.whats_new import WhatsNewReleaseCache
from pullbox.services.catalog import reader as catalog_reader
from pullbox.services.gcd_local_activation import active_snapshot
from pullbox.services.metadata_series_add import source_series_add_transaction
from pullbox.services.metadata_sources import read_source_policies
from pullbox.services.series_interest import ACTIVE_STATES, find_watch
from pullbox.services.whats_new_actions import (
    ReleaseSeriesEvidence,
    WhatsNewSelectionError,
    link_confirmed_release,
    load_release_selection,
    release_series_evidence,
)
from pullbox.services.whats_new_cache_service import WhatsNewCacheService
from pullbox.services.whats_new_grab import cache_fingerprint
from pullbox.services.whats_new_library_matches import native_library_matches
from pullbox.services.whats_new_watch_add import fetch_published_catalog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.schemas.whats_new import WhatsNewSeriesSelection
    from pullbox.services.whats_new_actions import ReleaseSelection


_CHANGED = "The release match or default library changed. Reload or use Find & Add."


@dataclass(frozen=True)
class ExactOffer:
    locg: str
    claims: dict[IdentityNamespace, str]
    root_id: int
    root_path: str

    def fingerprint(self, cache_hash: str, release_id: int) -> str:
        evidence = [cache_hash, release_id, self.locg, self.claims, self.root_id, self.root_path]
        return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()


async def current_release_evidence(rows: list[WhatsNewReleaseCache]) -> ReleaseSeriesEvidence:
    cache = WhatsNewCacheService()
    now = datetime.now(UTC)
    if len(rows) != 2 or any(row.fetched_at > now or cache.is_stale(row) for row in rows):
        return ReleaseSeriesEvidence({}, set(), {})
    return await asyncio.to_thread(
        release_series_evidence, *(row.payload for row in rows), as_of=now.date()
    )


async def lock_current_release_feeds(
    session: AsyncSession,
    current: WhatsNewReleaseCache | None,
    upcoming: WhatsNewReleaseCache | None,
) -> None:
    # Same order as the feed refresh, before locking a selected row or local series.
    for row in (current, upcoming):
        if (
            row is not None
            and await session.scalar(
                select(WhatsNewReleaseCache)
                .where(WhatsNewReleaseCache.id == row.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            is None
        ):
            raise WhatsNewSelectionError("These releases changed. Reload What's New.")
    if await WhatsNewCacheService().get_latest_current_week(session) is not current:
        raise WhatsNewSelectionError("These releases changed. Reload What's New.")


async def release_offers(
    session: AsyncSession, evidence: ReleaseSeriesEvidence, ids: set[str], *, lock: bool = False
) -> dict[str, ExactOffer]:
    matches = {
        key: evidence.claims[key] for key in ids & evidence.published if evidence.claims[key]
    }
    if not matches:
        return {}
    query = (
        select(LibraryRoot)
        .where(
            LibraryRoot.enabled.is_(True),
            LibraryRoot.allow_managed_writes.is_(True),
            LibraryRoot.is_default_managed_destination.is_(True),
        )
        .execution_options(populate_existing=True)
    )
    root = await session.scalar(query.with_for_update(read=True) if lock else query)
    if root is None:
        return {}
    needed = {ns for claims in matches.values() for ns in claims}
    usable = set()
    for policy in await read_source_policies(session):
        source = policy.source
        if (
            not policy.enabled
            or policy.configuration_status
            or source.identity_namespace not in needed
        ):
            continue
        if source is MetadataSource.COMICVINE_LOCAL:
            available = await asyncio.to_thread(
                lambda: catalog_reader.get_catalog_reader().available
            )
        elif source is MetadataSource.GCD_LOCAL:
            try:
                snapshot = await active_snapshot(session)
                available = snapshot is not None and snapshot.path == policy.settings.database_path
            except ValueError:
                available = False
        else:
            available = policy.credential_configured and (
                source is not MetadataSource.GCD_API_V2
                or get_settings().metadata_gcd_api_v2_enabled
            )
        if available:
            usable.add(source.identity_namespace)
    return {
        key: ExactOffer(key, claims, root.id, root.path)
        for key, claims in matches.items()
        if usable.intersection(claims)
    }


async def _validate(
    session: AsyncSession, submitted: WhatsNewSeriesSelection, *, lock: bool = False
) -> tuple[ReleaseSelection, ExactOffer]:
    if not get_settings().metadata_whats_new_actions_enabled:
        raise WhatsNewSelectionError("Release discovery actions are no longer enabled.")
    cache = WhatsNewCacheService()
    current = await cache.get_latest_current_week(session)
    upcoming = await cache.get_upcoming(session)
    if not current or not upcoming or submitted.cache_id not in {current.id, upcoming.id}:
        raise WhatsNewSelectionError(
            "These releases are no longer current. Reload or use Find & Add."
        )
    if lock:
        await lock_current_release_feeds(session, current, upcoming)
    selected = await load_release_selection(session, submitted.cache_id, submitted.release_id)
    locg = selected.locg_series_id
    if not locg or not selected.series_published:
        raise WhatsNewSelectionError("Publication is no longer confirmed. Use Find & Add.")
    evidence = await current_release_evidence([current, upcoming])
    local = await native_library_matches(session, evidence, {locg})
    if locg in local and local[locg] is None:
        raise WhatsNewSelectionError("Local provider links disagree. Use Find & Add.")
    options = await release_offers(session, evidence, {locg}, lock=lock)
    offer = options.get(locg)
    payload = current.payload if submitted.cache_id == current.id else upcoming.payload
    if not offer:
        raise WhatsNewSelectionError(
            "The exact source or default library is unavailable. Use Find & Add."
        )
    if offer.fingerprint(cache_fingerprint(payload), submitted.release_id) != submitted.fingerprint:
        raise WhatsNewSelectionError(_CHANGED)
    watch = await find_watch(session, locg)
    if watch and watch.state in ACTIVE_STATES:
        raise WhatsNewSelectionError("This Watch has its own destination. Use Find & Add.")
    return selected, offer


async def _owners(
    session: AsyncSession, claims: dict[IdentityNamespace, str]
) -> dict[IdentityNamespace, int]:
    links = await session.scalars(
        select(SeriesExternalIdentity).where(
            tuple_(
                SeriesExternalIdentity.identity_namespace, SeriesExternalIdentity.external_id
            ).in_(list(claims.items()))
        )
    )
    owners = {}
    for link in links:
        if link.verification_state is not IdentityVerificationState.VERIFIED:
            raise WhatsNewSelectionError("A local provider link needs review. Use Find & Add.")
        owners[link.identity_namespace] = link.series_id
    if len(set(owners.values())) > 1:
        raise WhatsNewSelectionError(
            "The provider links identify different series. Use Find & Add."
        )
    return owners


async def add_exact_release(
    session: AsyncSession, submitted: WhatsNewSeriesSelection, actor: int
) -> dict[str, object]:
    selected, offer = await _validate(session, submitted)
    expected = {**offer.claims, IdentityNamespace.LOCG: offer.locg}
    owners = await _owners(session, expected)
    if owners.keys() == expected.keys():
        series = await session.get(Series, owners[IdentityNamespace.LOCG])
        if series is not None:
            return {"id": series.id, "monitored": series.monitored, "locg_series_id": offer.locg}
    bundle = await fetch_published_catalog(session, offer.locg, offer.claims)
    await session.rollback()
    monitor = await load_search_on_add_default(session)
    await session.rollback()

    async def guard() -> None:
        await _validate(session, submitted, lock=True)
        await _owners(
            session,
            {
                **expected,
                **{ref.namespace: ref.external_id for ref in bundle.series.cross_identities},
            },
        )
        if monitor != await load_search_on_add_default(session):
            raise WhatsNewSelectionError("The monitoring policy changed. Reload or use Find & Add.")

    async with source_series_add_transaction(
        session,
        bundle,
        library_root_id=offer.root_id,
        search_on_add=monitor,
        event_bus=get_event_bus(),
        before_adopt=guard,
    ) as result:
        await link_confirmed_release(session, selected.selection, result.series.id, actor)
        return {
            "id": result.series.id,
            "monitored": result.series.monitored,
            "locg_series_id": offer.locg,
        }
