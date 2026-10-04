"""Fill compatible series gaps from fresh, confirmed public release-cache context."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.metadata_identity import (
    ExternalIdentityRef,
    IdentityNamespace,
    MetadataEntityKind,
)
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

FIELDS = frozenset({"publisher", "year_start", "volume"})
CROSS_IDS = {
    "comicvine_series_id": IdentityNamespace.COMICVINE,
    "metron_series_id": IdentityNamespace.METRON,
    "gcd_series_id": IdentityNamespace.GCD,
}


class LocgEnrichmentError(ValueError):
    """Cached discovery evidence cannot safely enrich the confirmed series."""


@dataclass(frozen=True)
class SeriesReleaseFacts:
    values: MetadataValues
    origin: PassiveReleaseOrigin
    cross_identities: tuple[ExternalIdentityRef, ...]
    cache_versions: tuple[tuple[int, datetime, datetime], ...]


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
    )
    _check_crosswalk(identities, facts)
    return facts


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
