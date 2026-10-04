"""Durable canonical metadata baselines with caller-owned transactions."""

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.metadata_identity import (
    ExternalIdentityRef,
    IdentityNamespace,
    MetadataEntityKind,
)
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models import Base, Issue, Series, StoryArc, StoryArcExternalIdentity
from pullbox.models.metadata_baseline import (
    IssueMetadataBaseline,
    SeriesMetadataBaseline,
    StoryArcMetadataBaseline,
)
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.schemas.metadata_snapshot import MetadataSnapshot
from pullbox.services.metadata_writer_identity import metadata_write_scope

_MODELS: dict[
    MetadataEntityKind,
    tuple[
        type[Series] | type[Issue] | type[StoryArc],
        type[SeriesMetadataBaseline] | type[IssueMetadataBaseline] | type[StoryArcMetadataBaseline],
        type[SeriesExternalIdentity] | type[IssueExternalIdentity] | type[StoryArcExternalIdentity],
    ],
] = {
    MetadataEntityKind.SERIES: (Series, SeriesMetadataBaseline, SeriesExternalIdentity),
    MetadataEntityKind.ISSUE: (Issue, IssueMetadataBaseline, IssueExternalIdentity),
    MetadataEntityKind.STORY_ARC: (StoryArc, StoryArcMetadataBaseline, StoryArcExternalIdentity),
}


class MetadataBaselineConflictError(ValueError):
    """The baseline or its identity ownership changed before persistence."""


@dataclass(frozen=True)
class MetadataBaselineWrite:
    local_id: int
    snapshot: MetadataSnapshot
    expected_revision: int = 0


@dataclass(frozen=True)
class SavedMetadataBaseline:
    local_id: int
    revision: int
    snapshot: MetadataSnapshot


async def load_metadata_baseline(
    session: AsyncSession, kind: MetadataEntityKind, local_id: int
) -> SavedMetadataBaseline | None:
    _, model, _ = _MODELS[kind]
    table = Base.metadata.tables[model.__tablename__]
    row = (
        (await session.execute(select(table).where(table.c[f"{kind.value}_id"] == local_id)))
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    try:
        snapshot = MetadataSnapshot.model_validate_json(row.snapshot_json)
        if snapshot.entity_kind is not kind:
            raise ValueError("Wrong entity kind")
    except (ValueError, ValidationError) as exc:
        raise MetadataBaselineConflictError(
            "Stored metadata baseline is invalid. Review before refreshing."
        ) from exc
    return SavedMetadataBaseline(local_id, row.revision, snapshot)


async def save_metadata_baselines(
    session: AsyncSession, writes: Sequence[MetadataBaselineWrite]
) -> tuple[SavedMetadataBaseline, ...]:
    if not writes:
        return ()
    if len(writes) > 200:
        raise ValueError("Metadata baseline batches are limited to 200 entities")
    keys = set()
    payloads = []
    for item in writes:
        key = (item.snapshot.entity_kind, item.local_id)
        if (
            type(item.local_id) is not int
            or item.local_id <= 0
            or type(item.expected_revision) is not int
            or item.expected_revision < 0
            or key in keys
        ):
            raise ValueError("Invalid or repeated metadata baseline target")
        keys.add(key)
        payload = item.snapshot.model_dump_json()
        MetadataSnapshot.model_validate_json(payload)
        if len(payload.encode("utf-8")) > 1048576:
            raise ValueError("Metadata baseline exceeds its storage limit")
        payloads.append(payload)
    try:
        async with metadata_write_scope(session):
            await _lock_targets(session, writes)
            results = []
            for kind in (
                MetadataEntityKind.STORY_ARC,
                MetadataEntityKind.SERIES,
                MetadataEntityKind.ISSUE,
            ):
                group = [
                    (item, payload)
                    for item, payload in zip(writes, payloads, strict=True)
                    if item.snapshot.entity_kind is kind
                ]
                if group:
                    results.extend(await _save_group(session, kind, group))
            await session.flush()
    except IntegrityError as exc:
        raise MetadataBaselineConflictError(
            "Metadata baseline changed. Reload before retrying."
        ) from exc
    by_key = {(result.snapshot.entity_kind, result.local_id): result for result in results}
    return tuple(by_key[(item.snapshot.entity_kind, item.local_id)] for item in writes)


async def _lock_targets(session: AsyncSession, writes: Sequence[MetadataBaselineWrite]) -> None:
    ids = {
        kind: {item.local_id for item in writes if item.snapshot.entity_kind is kind}
        for kind in MetadataEntityKind
    }
    parents = dict(
        (
            await session.execute(
                select(Issue.id, Issue.series_id).where(Issue.id.in_(ids[MetadataEntityKind.ISSUE]))
            )
        )
        .tuples()
        .all()
    )
    series_ids = ids[MetadataEntityKind.SERIES] | set(parents.values())
    # Match identity writers: arcs, series parents, then issues, each in ID order.
    for kind, targets in (
        (MetadataEntityKind.STORY_ARC, ids[MetadataEntityKind.STORY_ARC]),
        (MetadataEntityKind.SERIES, series_ids),
        (MetadataEntityKind.ISSUE, ids[MetadataEntityKind.ISSUE]),
    ):
        model, _, _ = _MODELS[kind]
        found = set(
            await session.scalars(
                select(model.id).where(model.id.in_(targets)).order_by(model.id).with_for_update()
            )
        )
        if found != targets:
            raise MetadataBaselineConflictError("Metadata target no longer exists.")
    actual = dict(
        (await session.execute(select(Issue.id, Issue.series_id).where(Issue.id.in_(parents))))
        .tuples()
        .all()
    )
    if actual != parents:
        raise MetadataBaselineConflictError("Issue parent changed. Reload before retrying.")
    release_proofs = [
        (parents[item.local_id], origin.passive_release.locg_series_id)
        for item in writes
        if item.snapshot.entity_kind is MetadataEntityKind.ISSUE
        for origin in item.snapshot.origins
        if origin.passive_release is not None
    ]
    if release_proofs:
        verified_parents = set(
            (
                await session.execute(
                    select(SeriesExternalIdentity.series_id, SeriesExternalIdentity.external_id)
                    .where(
                        SeriesExternalIdentity.series_id.in_(
                            {parent for parent, _ in release_proofs}
                        ),
                        SeriesExternalIdentity.identity_namespace == IdentityNamespace.LOCG,
                        SeriesExternalIdentity.verification_state
                        == IdentityVerificationState.VERIFIED,
                    )
                    .with_for_update(read=True)
                )
            )
            .tuples()
            .all()
        )
        if any(proof not in verified_parents for proof in release_proofs):
            raise MetadataBaselineConflictError(
                "The series release link changed. Review the confirmed LOCG link before retrying."
            )


async def _save_group(
    session: AsyncSession, kind: MetadataEntityKind, group: list[tuple[MetadataBaselineWrite, str]]
) -> list[SavedMetadataBaseline]:
    _, model, claim_model = _MODELS[kind]
    table = Base.metadata.tables[model.__tablename__]
    claims = Base.metadata.tables[claim_model.__tablename__]
    key = f"{kind.value}_id"
    ids = [item.local_id for item, _ in group]
    saved = {
        row[key]: row
        for row in (
            await session.execute(select(table).where(table.c[key].in_(ids)).with_for_update())
        ).mappings()
    }
    verified: dict[int, set[ExternalIdentityRef]] = {local_id: set() for local_id in ids}
    for claim in (
        await session.execute(
            select(claims).where(claims.c[key].in_(ids)).with_for_update(read=True)
        )
    ).mappings():
        namespace = (
            claim.source if kind is MetadataEntityKind.STORY_ARC else claim.identity_namespace
        )
        if kind is MetadataEntityKind.STORY_ARC and (
            claim.namespace != "story_arc" or namespace not in IdentityNamespace
        ):
            continue
        if claim.verification_state == IdentityVerificationState.VERIFIED:
            verified[claim[key]].add(
                ExternalIdentityRef(IdentityNamespace(namespace), kind, claim.external_id)
            )
    additions = []
    results = []
    for item, payload in group:
        row = saved.get(item.local_id)
        if (row.revision if row else 0) != item.expected_revision or set(
            item.snapshot.identities
        ) != verified[item.local_id]:
            raise MetadataBaselineConflictError(
                "Metadata baseline or verified identities changed. Reload before retrying."
            )
        values = {
            key: item.local_id,
            "revision": item.expected_revision + 1,
            "snapshot_json": payload,
        }
        if row is None:
            additions.append(values)
        else:
            await session.execute(
                update(table)
                .where(table.c.id == row.id, table.c.revision == item.expected_revision)
                .values(**values)
            )
        results.append(
            SavedMetadataBaseline(item.local_id, item.expected_revision + 1, item.snapshot)
        )
    if additions:
        await session.execute(insert(table), additions)
    return results
