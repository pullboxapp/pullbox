"""Read-only inventory for rehearsing the provider identity migration."""

from __future__ import annotations

import enum
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from pullbox.core.comicvine_arc_identity import normalize_comicvine_arc_id
from pullbox.core.metadata_identity import (
    ExactIdentityConflict,
    ExactIdentityEvidence,
    ExternalIdentityRef,
    IdentityEvidenceKind,
    IdentityNamespace,
    MetadataEntityKind,
    find_exact_identity_conflicts,
)
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.models.story_arc import StoryArc, StoryArcExternalIdentity

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

MAX_INVENTORY_PAGE_SIZE = 500
MAX_ARC_IDENTITIES_PER_PAGE = 2000


class LegacyIdentityStorage(enum.StrEnum):
    COMICVINE_COLUMN = "comicvine_column"
    STORY_ARC_RELATION = "story_arc_relation"


@dataclass(frozen=True)
class LegacyIdentityObservation:
    local_id: int
    identity: ExternalIdentityRef
    storage: LegacyIdentityStorage
    storage_row_id: int


@dataclass(frozen=True)
class LegacyIdentityProblem:
    local_id: int
    storage: LegacyIdentityStorage
    storage_row_id: int
    code: str


@dataclass(frozen=True)
class LegacyIdentityDisagreement:
    local_id: int
    conflict: ExactIdentityConflict


@dataclass(frozen=True)
class LegacyIdentityPage:
    entity_kind: MetadataEntityKind
    local_ids: tuple[int, ...]
    observations: tuple[LegacyIdentityObservation, ...]
    problems: tuple[LegacyIdentityProblem, ...]
    next_after_id: int | None
    has_more: bool
    disagreements: tuple[LegacyIdentityDisagreement, ...] = ()


class IdentityInventoryLimitError(ValueError):
    """The page cannot be reported completely within its identity-row budget."""


async def read_legacy_identity_page(
    session: AsyncSession,
    entity_kind: MetadataEntityKind,
    *,
    after_id: int = 0,
    limit: int = 100,
) -> LegacyIdentityPage:
    """Inventory persisted evidence, never grant trust or repair either store.

    This is a keyset page, not a transactionally frozen whole-library snapshot.
    Migration rehearsals must use a quiescent copy and check external ownership
    across all pages separately. Disagreements here are scoped to one target.
    """
    if not isinstance(entity_kind, MetadataEntityKind):
        raise ValueError("Unknown metadata entity kind")
    if (
        type(limit) is not int
        or not 1 <= limit <= MAX_INVENTORY_PAGE_SIZE
        or type(after_id) is not int
        or after_id < 0
    ):
        raise ValueError("Invalid identity inventory page bounds")
    models: dict[MetadataEntityKind, type[Series] | type[Issue] | type[StoryArc]] = {
        MetadataEntityKind.SERIES: Series,
        MetadataEntityKind.ISSUE: Issue,
        MetadataEntityKind.STORY_ARC: StoryArc,
    }
    model = models[entity_kind]
    observations: list[LegacyIdentityObservation] = []
    problems: list[LegacyIdentityProblem] = []
    # ORM SELECTs can autoflush unrelated caller mutations, even in a read helper.
    with session.no_autoflush:
        rows = (
            await session.execute(
                select(model.id, model.comicvine_id)
                .where(model.id > after_id)
                .order_by(model.id)
                .limit(limit + 1)
            )
        ).all()
        page_rows = rows[:limit]
        local_ids = tuple(row.id for row in page_rows)
        for local_id, value in page_rows:
            if value is None:
                continue
            storage = LegacyIdentityStorage.COMICVINE_COLUMN
            if type(value) is not int or value <= 0:
                problems.append(
                    LegacyIdentityProblem(local_id, storage, local_id, "invalid_external_id")
                )
                continue
            observations.append(
                LegacyIdentityObservation(
                    local_id,
                    ExternalIdentityRef(IdentityNamespace.COMICVINE, entity_kind, str(value)),
                    storage,
                    local_id,
                )
            )

        if entity_kind == MetadataEntityKind.STORY_ARC and local_ids:
            arc_rows = (
                await session.execute(
                    select(
                        StoryArcExternalIdentity.id,
                        StoryArcExternalIdentity.story_arc_id,
                        StoryArcExternalIdentity.source,
                        StoryArcExternalIdentity.external_id,
                    )
                    .where(
                        StoryArcExternalIdentity.story_arc_id.in_(local_ids),
                        StoryArcExternalIdentity.source.in_(tuple(IdentityNamespace)),
                        StoryArcExternalIdentity.namespace == "story_arc",
                    )
                    .order_by(StoryArcExternalIdentity.story_arc_id, StoryArcExternalIdentity.id)
                    .limit(MAX_ARC_IDENTITIES_PER_PAGE + 1)
                )
            ).all()
            if len(arc_rows) > MAX_ARC_IDENTITIES_PER_PAGE:
                raise IdentityInventoryLimitError(
                    "Story arc identity row budget exceeded. Smaller pages may help; "
                    "an arc that exceeds the budget alone needs a separate inspection."
                )
            for row_id, local_id, source, external_id in arc_rows:
                storage = LegacyIdentityStorage.STORY_ARC_RELATION
                try:
                    if source == IdentityNamespace.COMICVINE:
                        external_id = normalize_comicvine_arc_id(external_id)
                    identity = ExternalIdentityRef(
                        IdentityNamespace(source), entity_kind, external_id
                    )
                except ValueError:
                    problems.append(
                        LegacyIdentityProblem(local_id, storage, row_id, "invalid_external_id")
                    )
                else:
                    observations.append(
                        LegacyIdentityObservation(local_id, identity, storage, row_id)
                    )

    grouped: dict[int, list[ExactIdentityEvidence]] = defaultdict(list)
    for observation in observations:
        grouped[observation.local_id].append(
            ExactIdentityEvidence(observation.identity, IdentityEvidenceKind.LEGACY_BACKFILL)
        )
    disagreements = tuple(
        LegacyIdentityDisagreement(local_id, conflict)
        for local_id, evidence in sorted(grouped.items())
        for conflict in find_exact_identity_conflicts(evidence)
    )
    return LegacyIdentityPage(
        entity_kind,
        local_ids,
        tuple(observations),
        tuple(problems),
        local_ids[-1] if local_ids else None,
        len(rows) > limit,
        disagreements,
    )
