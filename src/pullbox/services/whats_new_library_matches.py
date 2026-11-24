"""Read-only series badges from corroborated native IDs, never evidence for an issue."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import or_, select

from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models import Series, SeriesExternalIdentity

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.services.whats_new_actions import ReleaseSeriesEvidence


@dataclass(frozen=True)
class LibrarySeriesBadge:
    id: int
    monitored: bool


async def native_library_matches(
    session: AsyncSession, evidence: ReleaseSeriesEvidence, visible_ids: set[str]
) -> dict[str, LibrarySeriesBadge | None]:
    """An absent key has no known owner; None means the local evidence disagrees."""
    wanted = {
        locg: {**native, IdentityNamespace.LOCG: locg}
        for locg in visible_ids
        if (native := evidence.claims.get(locg))
    }
    if not wanted:
        return {}
    grouped: dict[IdentityNamespace, set[str]] = defaultdict(set)
    for refs in wanted.values():
        for ns, external in refs.items():
            grouped[ns].add(external)
    link = SeriesExternalIdentity
    # Read sibling links too: a matching Metron ID cannot overrule a different GCD or LOCG ID.
    candidates = select(link.series_id).where(
        or_(
            *(
                (link.identity_namespace == ns) & link.external_id.in_(values)
                for ns, values in grouped.items()
            )
        )
    )
    cv = IdentityNamespace.COMICVINE
    eligible = [Series.id.in_(candidates)]
    if cv in grouped:
        eligible.append(Series.comicvine_id.in_([int(value) for value in grouped[cv]]))
    rows = await session.execute(
        select(
            Series.id,
            Series.monitored,
            Series.comicvine_id,
            link.identity_namespace,
            link.external_id,
            link.verification_state,
        )
        .select_from(Series)
        .outerjoin(link, Series.id == link.series_id)
        .where(or_(*eligible))
    )
    by_ref: dict[tuple[IdentityNamespace, str], int] = {}
    by_owner: dict[int, dict[IdentityNamespace, tuple[str, IdentityVerificationState]]] = (
        defaultdict(dict)
    )
    badges: dict[int, LibrarySeriesBadge] = {}
    legacy_by_owner: dict[int, str] = {}
    legacy_by_id: dict[str, int] = {}
    for owner, monitored, legacy, ns, external, state in rows:
        if ns is not None:
            by_ref[ns, external] = owner
            by_owner[owner][ns] = (external, state)
        if legacy is not None:
            legacy_by_owner[owner] = str(legacy)
            legacy_by_id[str(legacy)] = owner
        badges[owner] = LibrarySeriesBadge(owner, monitored)
    result: dict[str, LibrarySeriesBadge | None] = {}
    for locg, refs in wanted.items():
        owners = {by_ref[key] for key in refs.items() if key in by_ref}
        cv_id = refs.get(cv)
        legacy_owner = legacy_by_id.get(cv_id) if cv_id else None
        if not owners and legacy_owner is None:
            continue
        result[locg] = None
        if len(owners) == 1:
            owner = next(iter(owners))
            local = by_owner[owner]
            if legacy_owner not in (None, owner) or (
                cv_id is not None and legacy_by_owner.get(owner, cv_id) != cv_id
            ):
                continue
            if any(
                evidence.series_by_native_id.get((ns, value), locg) != locg
                for ns, (value, _) in local.items()
            ):
                continue
            if all(
                ns not in local or local[ns] == (value, IdentityVerificationState.VERIFIED)
                for ns, value in refs.items()
            ):
                result[locg] = badges[owner]
    return result
