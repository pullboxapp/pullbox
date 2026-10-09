"""Fill proven metadata gaps from fresh, confirmed public release-cache context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import or_, select

from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.core.metadata_identity import (
    ExternalIdentityRef,
    IdentityNamespace,
    MetadataEntityKind,
)
from pullbox.models import Series
from pullbox.models.whats_new import WhatsNewCacheKind, WhatsNewReleaseCache
from pullbox.schemas.metadata_snapshot import (
    FieldOrigin,
    MetadataSnapshot,
    MetadataValues,
    PassiveReleaseOrigin,
    field_domain,
)
from pullbox.services.metadata_assembly import missing_metadata_value
from pullbox.services.whats_new_actions import positive_id
from pullbox.services.whats_new_cache_service import DEFAULT_STALE_AFTER
from pullbox.services.whats_new_issue_state import local_release_issues

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.services.metadata_series_refresh_state import SeriesRefreshState

FIELDS = frozenset({"publisher", "year_start", "volume"})
CROSS_IDS = {
    "comicvine_series_id": IdentityNamespace.COMICVINE,
    "metron_series_id": IdentityNamespace.METRON,
    "gcd_series_id": IdentityNamespace.GCD,
}
ISSUE_CROSS_IDS = {
    "comicvine_issue_id": IdentityNamespace.COMICVINE,
    "metron_issue_id": IdentityNamespace.METRON,
    "gcd_issue_id": IdentityNamespace.GCD,
}


class LocgEnrichmentError(ValueError):
    """Cached discovery evidence cannot safely enrich the confirmed series."""


@dataclass(frozen=True)
class CachedIssueRelease:
    release_id: str
    number: str | None
    store_date: date | None
    cross_ids: tuple[tuple[str, int], ...]
    fetched_at: datetime

    def payload(self, series_id: str) -> dict[str, object]:
        return {
            "locg_issue_id": int(self.release_id),
            "locg_series_id": int(series_id),
            "series": {"locg_series_id": int(series_id)},
            "issue_number": self.number,
            "store_date": self.store_date,
            **dict(self.cross_ids),
        }


@dataclass(frozen=True)
class SeriesReleaseFacts:
    values: MetadataValues
    origin: PassiveReleaseOrigin
    cross_identities: tuple[ExternalIdentityRef, ...]
    cache_versions: tuple[tuple[int, datetime, datetime], ...]
    issues: tuple[CachedIssueRelease, ...] = ()


@dataclass(frozen=True)
class IssueReleaseFacts:
    local_id: int
    origin: PassiveReleaseOrigin


def _captured_issue(
    item: dict[str, object], release_id: str, fetched_at: datetime
) -> CachedIssueRelease:
    number = item.get("issue_number")
    try:
        number = normalize_issue_number_text(number) if isinstance(number, str) else None
    except ValueError:
        number = None
    day = item.get("store_date")
    if isinstance(day, str):
        try:
            parsed = date.fromisoformat(day)
            day = parsed if parsed.isoformat() == day else None
        except ValueError:
            day = None
    return CachedIssueRelease(
        release_id,
        number,
        day if type(day) is date else None,
        tuple(
            (field, value if type(value) is int and 0 < value < 2**63 else 0)
            for field in ISSUE_CROSS_IDS
            if (value := item.get(field)) is not None
        ),
        fetched_at,
    )


async def read_series_release_facts(
    session: AsyncSession,
    identities: Sequence[ExternalIdentityRef],
    *,
    now: datetime,
    lock: bool = False,
) -> SeriesReleaseFacts | None:
    """At most two cache rows, no historical scan, provider call or per-issue query.

    Repeating this read under the caller's write locks revalidates the exact facts
    and freshness after provider I/O. The caller still owns the atomic commit.
    """
    identity = next((item for item in identities if item.namespace is IdentityNamespace.LOCG), None)
    if identity is None:
        return None
    latest = (
        select(WhatsNewReleaseCache.id)
        .where(WhatsNewReleaseCache.cache_kind == WhatsNewCacheKind.CURRENT_WEEK)
        .order_by(WhatsNewReleaseCache.store_date.desc(), WhatsNewReleaseCache.fetched_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    query = (
        select(WhatsNewReleaseCache)
        .where(
            or_(WhatsNewReleaseCache.id == latest, WhatsNewReleaseCache.cache_key == "upcoming:all")
        )
        .order_by(WhatsNewReleaseCache.id)
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update(read=True)
    rows = list(await session.scalars(query))
    values: dict[str, set[str | int]] = {field: set() for field in FIELDS}
    releases: set[str] = set()
    crosswalk: set[ExternalIdentityRef] = set()
    versions: list[tuple[int, datetime, datetime]] = []
    issue_releases: list[CachedIssueRelease] = []
    for row in rows:
        age = now - row.fetched_at
        if (
            age.total_seconds() < 0
            or age > DEFAULT_STALE_AFTER
            or not isinstance(row.payload, dict)
        ):
            continue
        items = row.payload.get("issues", [])
        groups = row.payload.get("weeks", [])
        if not isinstance(items, list) or not isinstance(groups, list):
            continue
        items = list(items)
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("issues"), list):
                if len(items) + len(group["issues"]) > 10000:
                    items = []
                    break
                items.extend(group["issues"])
        if len(items) > 10000:
            continue
        matched = False
        for item in items:
            if not isinstance(item, dict):
                continue
            series = item.get("series")
            if not isinstance(series, dict):
                continue
            ids = (item.get("locg_series_id"), series.get("locg_series_id"))
            if identity.external_id not in {positive_id(value) for value in ids}:
                continue
            if any(
                value is not None and positive_id(value) != identity.external_id for value in ids
            ):
                raise LocgEnrichmentError(
                    "Cached release series identities disagree. Review the confirmed LOCG link "
                    "and recheck What's New releases before retrying."
                )
            release_id = positive_id(item.get("locg_issue_id"))
            if release_id is None:
                continue
            matched = True
            releases.add(release_id)
            issue_releases.append(_captured_issue(item, release_id, row.fetched_at))
            for field, namespace in CROSS_IDS.items():
                raw = series.get(field)
                if raw is None:
                    continue
                identifier = positive_id(raw)
                if identifier is None:
                    raise LocgEnrichmentError(
                        "Cached release identities are invalid. Recheck What's New releases "
                        "before retrying."
                    )
                crosswalk.add(ExternalIdentityRef(namespace, MetadataEntityKind.SERIES, identifier))
            publisher = item.get("publisher")
            name = publisher.get("name") if isinstance(publisher, dict) else None
            if isinstance(name, str) and name.strip() and len(name.strip()) <= 255:
                values["publisher"].add(name.strip())
            year = series.get("start_year")
            if isinstance(year, int) and not isinstance(year, bool) and 1 <= year <= 9999:
                values["year_start"].add(year)
            volume = series.get("volume")
            if isinstance(volume, str) and volume.strip() and len(volume.strip()) <= 100:
                values["volume"].add(volume.strip())
        if matched:
            versions.append((row.id, row.fetched_at, row.last_successful_refresh_at))
    if not releases or len(releases) > 10000:
        return None
    facts = SeriesReleaseFacts(
        MetadataValues.model_validate(
            {field: next(iter(options)) for field, options in values.items() if len(options) == 1}
        ),
        PassiveReleaseOrigin(
            locg_series_id=identity.external_id,
            release_ids=tuple(sorted(releases)),
            fetched_at=min(version[1] for version in versions),
        ),
        tuple(sorted(crosswalk, key=lambda item: (item.namespace, item.external_id))),
        tuple(versions),
        tuple(issue_releases),
    )
    _check_crosswalk(identities, facts)
    return facts


async def read_issue_release_facts(
    session: AsyncSession, state: SeriesRefreshState, facts: SeriesReleaseFacts | None
) -> tuple[IssueReleaseFacts, ...]:
    """Reuse the release resolver in bulk; no issue creation or new identity claims."""
    if facts is None or not state.issues or not facts.issues:
        return ()
    parent = await session.get(Series, state.series.local_id)
    assert parent is not None
    known = {item.local_id: item for item in state.issues}
    by_number: dict[str, set[int]] = {}
    by_identity: dict[tuple[IdentityNamespace, int], int] = {
        (ref.namespace, int(ref.external_id)): entity.local_id
        for entity in state.issues
        for ref in entity.identities
        if ref.namespace is not IdentityNamespace.LOCG
    }
    for entity in state.issues:
        by_number.setdefault(entity.values.issue_number_text or "", set()).add(entity.local_id)
    seen: dict[str, CachedIssueRelease] = {}
    for release in facts.issues:
        previous = seen.get(release.release_id)
        if previous and previous.payload(facts.origin.locg_series_id) != release.payload(
            facts.origin.locg_series_id
        ):
            return ()
        if previous is None or release.fetched_at < previous.fetched_at:
            seen[release.release_id] = release
    releases = list(seen.values())
    candidates: dict[int, list[CachedIssueRelease]] = {}
    blocked: set[int] = set()
    # Bound SQL parameters without losing disagreements across variant batches.
    for offset in range(0, len(releases), 200):
        batch = releases[offset : offset + 200]
        resolved = await local_release_issues(
            session,
            [release.payload(facts.origin.locg_series_id) for release in batch],
            {facts.origin.locg_series_id: parent},
        )
        for release, result in zip(batch, resolved, strict=True):
            if result is not None and result.issue_id in known:
                candidates.setdefault(result.issue_id, []).append(release)
            else:
                blocked.update(by_number.get(release.number or "", ()))
                for field, value in release.cross_ids:
                    target = by_identity.get((ISSUE_CROSS_IDS[field], value))
                    if target is not None:
                        blocked.add(target)
    accepted = []
    for local_id, members in sorted(candidates.items()):
        issue = known[local_id]
        days = {item.store_date for item in members}
        anchors = tuple(
            ref for ref in issue.identities if ref.namespace is not IdentityNamespace.LOCG
        )
        if local_id in blocked or len(days) != 1 or None in days or not anchors:
            continue
        accepted.append(
            IssueReleaseFacts(
                local_id,
                PassiveReleaseOrigin(
                    locg_series_id=facts.origin.locg_series_id,
                    release_ids=tuple(sorted({item.release_id for item in members})),
                    fetched_at=min(item.fetched_at for item in members),
                    issue_identity=anchors[0],
                    issue_number_text=issue.values.issue_number_text,
                    matched_store_date=next(iter(days)),
                    match_kind="exact_issue"
                    if all(item.cross_ids for item in members)
                    else "number_date",
                ),
            )
        )
    return tuple(accepted)


def enrich_issue_snapshot(
    snapshot: MetadataSnapshot, facts: IssueReleaseFacts | None, *, now: datetime
) -> MetadataSnapshot:
    if facts is None:
        return snapshot
    origins = {item.field: item for item in snapshot.origins}
    origin = origins.get("store_date")
    proof = facts.origin
    if (
        snapshot.entity_kind is not MetadataEntityKind.ISSUE
        or proof.issue_identity not in snapshot.identities
        or proof.issue_number_text != snapshot.values.issue_number_text
        or not missing_metadata_value(snapshot.values.store_date)
        or (origin and (origin.user_override or origin.embedded_documents))
        or any(item.startswith("archive:issue:store_date:") for item in snapshot.diagnostics)
        or (
            proof.match_kind == "number_date"
            and snapshot.values.cover_date != proof.matched_store_date
        )
    ):
        return snapshot
    origins["store_date"] = FieldOrigin(
        field="store_date",
        domain=field_domain(snapshot.entity_kind, "store_date"),
        observed_at=now,
        passive_release=proof,
    )
    return MetadataSnapshot.model_validate(
        {
            **snapshot.model_dump(),
            "values": {**snapshot.values.model_dump(), "store_date": proof.matched_store_date},
            "origins": tuple(origins.values()),
        }
    )


def _check_crosswalk(identities: Sequence[ExternalIdentityRef], facts: SeriesReleaseFacts) -> None:
    known = {item.namespace: item.external_id for item in identities}
    for identity in facts.cross_identities:
        if identity.namespace in known and known[identity.namespace] != identity.external_id:
            raise LocgEnrichmentError(
                "Cached release identities disagree with this series. Review the confirmed "
                "LOCG link and recheck What's New releases before retrying."
            )
        known[identity.namespace] = identity.external_id


def enrich_series_snapshot(
    snapshot: MetadataSnapshot, facts: SeriesReleaseFacts | None, *, now: datetime
) -> MetadataSnapshot:
    if facts is None:
        return snapshot
    _check_crosswalk((*snapshot.identities, *snapshot.observed_identities), facts)
    values = snapshot.values.model_dump()
    origins = {item.field: item for item in snapshot.origins}
    for field in FIELDS:
        origin = origins.get(field)
        incoming = getattr(facts.values, field)
        if (
            missing_metadata_value(values[field])
            and not missing_metadata_value(incoming)
            and not (origin and (origin.user_override or origin.embedded_documents))
            and not any(
                item.startswith(f"archive:series:{field}:") for item in snapshot.diagnostics
            )
        ):
            values[field] = incoming
            origins[field] = FieldOrigin(
                field=field,
                domain=field_domain(snapshot.entity_kind, field),
                observed_at=now,
                passive_release=facts.origin,
            )
    return MetadataSnapshot.model_validate(
        {
            **snapshot.model_dump(),
            "values": values,
            "origins": tuple(origins.values()),
        }
    )
