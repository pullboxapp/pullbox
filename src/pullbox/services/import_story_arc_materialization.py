"""Materialize confirmed story-arc staging rows into the logical domain."""

from __future__ import annotations

import os
import stat
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Literal

from sqlalchemy import or_, select, tuple_
from sqlalchemy.orm import selectinload

from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.core.story_arc_naming import (
    validate_story_arc_file_template,
    validate_story_arc_folder_template,
)
from pullbox.models.import_job import ImportedFileStatus, ImportJob, ImportSourceType
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryRoot
from pullbox.models.story_arc import (
    ImportedStoryArcStatus,
    IssueStoryArc,
    StoryArc,
    StoryArcExternalIdentity,
    StoryArcLifecycle,
    StoryArcPlacement,
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
    StoryArcPlacementState,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.services.story_arc_service import StoryArcService, StoryArcServiceError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

    from pullbox.services.import_job_execution_types import RecordActionFunc


CancellationCheck = Callable[[], Awaitable[None]]
CountField = Literal[
    "arcs_created",
    "arcs_merged",
    "arcs_reused",
    "arcs_failed",
    "external_identities_created",
    "external_identities_reused",
    "memberships_created",
    "memberships_reused",
    "resolved_entries",
    "unresolved_entries",
    "entries_skipped",
]

_POLICY_FLAGS = ("monitored", "search_missing", "include_upcoming", "sync_enabled")
_CONFIRMED_POLICY_KEYS = frozenset(
    {
        "schema_version",
        "source",
        "activation",
        "monitored",
        "search_missing",
        "include_upcoming",
        "sync_enabled",
        "placement_policy",
    }
)
_PLACEMENT_POLICY_KEYS = frozenset(
    {
        "schema_version",
        "mode",
        "target_library_root_id",
        "destination_root",
        "folder_template",
        "file_template",
        "symlink_style",
        "synchronize",
    }
)
_PLACEMENT_POLICY_MODES = frozenset({"logical", "reference_only", "copy", "hardlink", "symlink"})
_SAFE_IMPORTED_FILE_STATES = frozenset(
    {ImportedFileStatus.IMPORTED, ImportedFileStatus.ALREADY_OWNED}
)
_UNRESOLVED_STATES = frozenset(
    {
        StoryArcResolutionState.PENDING,
        StoryArcResolutionState.MISSING,
        StoryArcResolutionState.AMBIGUOUS,
        StoryArcResolutionState.CONFLICT,
        StoryArcResolutionState.SKIPPED,
    }
)


@dataclass(frozen=True, slots=True)
class StoryArcMaterializationWarning:
    """Sanitized warning tied to durable import staging identity."""

    code: str
    imported_story_arc_id: int
    imported_story_arc_entry_id: int | None = None


@dataclass(frozen=True, slots=True)
class StoryArcMaterializationResult:
    """Durable logical-materialization counters for progress and diagnostics."""

    arcs_examined: int = 0
    arcs_created: int = 0
    arcs_merged: int = 0
    arcs_reused: int = 0
    arcs_failed: int = 0
    external_identities_created: int = 0
    external_identities_reused: int = 0
    memberships_created: int = 0
    memberships_reused: int = 0
    resolved_entries: int = 0
    unresolved_entries: int = 0
    entries_skipped: int = 0
    warnings: tuple[StoryArcMaterializationWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class _ValidatedPolicy:
    activated: bool
    snapshot: dict[str, object]
    monitored: bool = False
    search_missing: bool = False
    include_upcoming: bool = False
    sync_enabled: bool = False
    target_library_root_id: int | None = None
    warning_code: str | None = None


@dataclass(slots=True)
class _MutableCounts:
    arcs_examined: int = 0
    arcs_created: int = 0
    arcs_merged: int = 0
    arcs_reused: int = 0
    arcs_failed: int = 0
    external_identities_created: int = 0
    external_identities_reused: int = 0
    memberships_created: int = 0
    memberships_reused: int = 0
    resolved_entries: int = 0
    unresolved_entries: int = 0
    entries_skipped: int = 0

    def freeze(
        self,
        warnings: Sequence[StoryArcMaterializationWarning],
    ) -> StoryArcMaterializationResult:
        return StoryArcMaterializationResult(
            arcs_examined=self.arcs_examined,
            arcs_created=self.arcs_created,
            arcs_merged=self.arcs_merged,
            arcs_reused=self.arcs_reused,
            arcs_failed=self.arcs_failed,
            external_identities_created=self.external_identities_created,
            external_identities_reused=self.external_identities_reused,
            memberships_created=self.memberships_created,
            memberships_reused=self.memberships_reused,
            resolved_entries=self.resolved_entries,
            unresolved_entries=self.unresolved_entries,
            entries_skipped=self.entries_skipped,
            warnings=tuple(warnings),
        )


@dataclass(slots=True)
class _MaterializationState:
    arcs_by_id: dict[int, StoryArc]
    job_arc_ids_by_import_identity: dict[
        tuple[StoryArcSourceKind, str],
        int | None,
    ]
    identities_by_key: dict[tuple[str, str, str], int]
    loaded_identity_keys: set[tuple[str, str, str]]


@dataclass(slots=True)
class _MaterializationBatch:
    state: _MaterializationState
    library_root_ids: set[int]
    identity_evidence: dict[
        int,
        tuple[list[tuple[str, str, str]], str | None],
    ]


@dataclass(slots=True)
class _ArcMaterializationContext:
    staged_arc: ImportedStoryArc
    arc: StoryArc
    counts: _MutableCounts


@dataclass(slots=True)
class _EntryPageLookups:
    issues_by_id: dict[int, Issue]
    memberships_by_id: dict[int, IssueStoryArc]
    memberships_by_arc_issue: dict[tuple[int, int], IssueStoryArc]
    memberships_by_arc_source: dict[
        tuple[int, StoryArcSourceKind, str],
        IssueStoryArc,
    ]
    reference_candidates_by_entry_id: dict[int, _ReferenceCandidateResolution]
    placements_by_path: dict[str, StoryArcPlacement]
    placements_by_membership: dict[int, list[StoryArcPlacement]]


@dataclass(frozen=True, slots=True)
class _ReferencePathCandidate:
    path: Path
    trusted_root: Path


@dataclass(frozen=True, slots=True)
class _ReferenceCandidateResolution:
    candidate: _ReferencePathCandidate | None = None
    warning_code: str | None = None


@dataclass(frozen=True, slots=True)
class _ReferencePathInspection:
    fingerprint: dict[str, object] | None = None
    warning_code: str | None = None


@dataclass(slots=True)
class _ReferencePageCheckpoint:
    cancellation_check: CancellationCheck | None
    checked: bool = False

    async def ensure_checked(self) -> None:
        """Check cancellation once before a bounded page touches reference evidence."""
        if self.checked:
            return
        await _checkpoint(self.cancellation_check)
        self.checked = True


async def materialize_confirmed_story_arcs(
    session: AsyncSession,
    *,
    import_job_id: int,
    batch_size: int = 100,
    entry_checkpoint_size: int = 250,
    cancellation_check: CancellationCheck | None = None,
    record_action: RecordActionFunc | None = None,
) -> StoryArcMaterializationResult:
    """Create or explicitly merge confirmed logical story arcs for one job.

    This Step 4 service consumes database staging and canonical issue rows. It
    never invokes a provider, opens archive content, mutates a source artifact,
    or commits. Confirmed existing Mylar/folder artifacts may be attached as
    referenced placements after metadata-only, no-follow root validation. The
    caller retains one rollback boundary across canonical file processing and
    logical story-arc registration.
    """
    if isinstance(batch_size, bool) or batch_size <= 0:
        raise ValueError("Story-arc materialization batch size must be positive")
    if isinstance(entry_checkpoint_size, bool) or entry_checkpoint_size <= 0:
        raise ValueError("Story-arc entry checkpoint size must be positive")

    counts = _MutableCounts()
    warnings: list[StoryArcMaterializationWarning] = []
    job = await session.get(ImportJob, import_job_id)
    if job is None:
        raise ValueError(f"Import job {import_job_id} was not found")
    await _checkpoint(cancellation_check)
    state = _MaterializationState(
        arcs_by_id={},
        job_arc_ids_by_import_identity={},
        identities_by_key={},
        loaded_identity_keys=set(),
    )
    await _load_job_arc_recovery_index(
        session,
        import_job_id=import_job_id,
        batch_size=batch_size,
        state=state,
        cancellation_check=cancellation_check,
    )
    last_id = 0

    while True:
        staged_arcs = list(
            (
                await session.scalars(
                    select(ImportedStoryArc)
                    .where(
                        ImportedStoryArc.import_job_id == import_job_id,
                        ImportedStoryArc.status == ImportedStoryArcStatus.CONFIRMED,
                        ImportedStoryArc.selected_for_import.is_(True),
                        ImportedStoryArc.id > last_id,
                    )
                    .order_by(ImportedStoryArc.id)
                    .limit(batch_size)
                )
            ).all()
        )
        if not staged_arcs:
            break

        await _checkpoint(cancellation_check)
        batch_warning_start = len(warnings)
        identity_evidence = await _load_batch_external_identity_evidence(
            session,
            staged_arcs=staged_arcs,
            entry_page_size=entry_checkpoint_size,
            cancellation_check=cancellation_check,
        )
        batch = await _prepare_materialization_batch(
            session,
            import_job_id=import_job_id,
            staged_arcs=staged_arcs,
            state=state,
            identity_evidence=identity_evidence,
        )
        contexts: dict[int, _ArcMaterializationContext] = {}
        for staged_arc in staged_arcs:
            counts.arcs_examined += 1
            context = await _materialize_one_arc(
                session,
                import_job_id=import_job_id,
                job=job,
                staged_arc=staged_arc,
                counts=counts,
                warnings=warnings,
                record_action=record_action,
                batch=batch,
            )
            if context is not None:
                contexts[int(staged_arc.id)] = context

        await _materialize_entry_pages(
            session,
            import_job_id=import_job_id,
            job=job,
            contexts=contexts,
            counts=counts,
            warnings=warnings,
            record_action=record_action,
            entry_page_size=entry_checkpoint_size,
            cancellation_check=cancellation_check,
        )
        batch_warning_codes = _warning_codes_by_arc(warnings[batch_warning_start:])
        for context in contexts.values():
            context.staged_arc.status = ImportedStoryArcStatus.IMPORTED
            _persist_arc_materialization_diagnostics(
                context.staged_arc,
                story_arc_id=int(context.arc.id),
                status="imported",
                counts=_count_snapshot(context.counts),
                warning_codes=batch_warning_codes.get(int(context.staged_arc.id), []),
            )
        await session.flush()
        last_id = int(staged_arcs[-1].id)

    return counts.freeze(warnings)


async def _load_job_arc_recovery_index(
    session: AsyncSession,
    *,
    import_job_id: int,
    batch_size: int,
    state: _MaterializationState,
    cancellation_check: CancellationCheck | None,
) -> None:
    """Build an O(1) restart index from bounded scalar-only pages."""
    last_id = 0
    while True:
        rows = (
            await session.execute(
                select(
                    StoryArc.id,
                    StoryArc.source_kind,
                    StoryArc.source_import_job_id,
                    StoryArc.diagnostics,
                )
                .where(
                    StoryArc.source_import_job_id == import_job_id,
                    StoryArc.id > last_id,
                )
                .order_by(StoryArc.id)
                .limit(batch_size)
            )
        ).all()
        if not rows:
            return
        for arc_id, source_kind, source_import_job_id, diagnostics in rows:
            _index_job_arc_values(
                state,
                arc_id=int(arc_id),
                source_kind=source_kind,
                source_import_job_id=int(source_import_job_id),
                diagnostics=diagnostics,
            )
        last_id = int(rows[-1].id)
        await _checkpoint(cancellation_check)


async def _load_batch_external_identity_evidence(
    session: AsyncSession,
    *,
    staged_arcs: Sequence[ImportedStoryArc],
    entry_page_size: int,
    cancellation_check: CancellationCheck | None,
) -> dict[int, tuple[list[tuple[str, str, str]], str | None]]:
    """Scan provider-neutral entry evidence without materializing entry ORM rows."""
    staged_arc_ids = [int(staged_arc.id) for staged_arc in staged_arcs]
    comicvine_ids: dict[int, set[str]] = {arc_id: set() for arc_id in staged_arc_ids}
    row_count = 0
    result = await session.stream(
        select(
            ImportedStoryArcEntry.imported_story_arc_id,
            ImportedStoryArcEntry.evidence,
        )
        .where(
            ImportedStoryArcEntry.imported_story_arc_id.in_(staged_arc_ids),
            ImportedStoryArcEntry.selected_for_import.is_(True),
        )
        .order_by(ImportedStoryArcEntry.id)
        .execution_options(yield_per=entry_page_size)
    )
    try:
        async for imported_story_arc_id, evidence in result:
            values = comicvine_ids[int(imported_story_arc_id)]
            cv_arc_id = _mapping(evidence).get("cv_arc_id")
            if cv_arc_id is not None and str(cv_arc_id).strip() and len(values) < 2:
                values.add(str(cv_arc_id))
            row_count += 1
            if row_count % entry_page_size == 0:
                await _checkpoint(cancellation_check)
    finally:
        await result.close()

    loaded: dict[int, tuple[list[tuple[str, str, str]], str | None]] = {}
    for staged_arc in staged_arcs:
        identities: list[tuple[str, str, str]] = []
        if staged_arc.source_arc_id:
            identities.append((staged_arc.source_kind.value, "story_arc", staged_arc.source_arc_id))
        values = comicvine_ids[int(staged_arc.id)]
        warning_code = None
        if len(values) == 1:
            identities.append(("comicvine", "story_arc", next(iter(values))))
        elif len(values) > 1:
            warning_code = "conflicting_external_identity_evidence"
        loaded[int(staged_arc.id)] = (identities, warning_code)
    return loaded


async def _prepare_materialization_batch(
    session: AsyncSession,
    *,
    import_job_id: int,
    staged_arcs: Sequence[ImportedStoryArc],
    state: _MaterializationState,
    identity_evidence: dict[
        int,
        tuple[list[tuple[str, str, str]], str | None],
    ],
) -> _MaterializationBatch:
    """Prefetch all canonical lookups needed by one bounded staging page."""
    state.arcs_by_id.clear()
    identity_keys: set[tuple[str, str, str]] = set()
    target_arc_ids: set[int] = set()
    library_root_ids: set[int] = set()

    for staged_arc in staged_arcs:
        identities, _warning = identity_evidence[int(staged_arc.id)]
        identity_keys.update(identities)
        if staged_arc.materialized_story_arc_id is not None:
            target_arc_ids.add(int(staged_arc.materialized_story_arc_id))
        if staged_arc.proposed_story_arc_id is not None:
            target_arc_ids.add(int(staged_arc.proposed_story_arc_id))
        raw_policy = staged_arc.proposed_policy_snapshot
        if isinstance(raw_policy, dict):
            placement_policy = raw_policy.get("placement_policy")
            target_root_id = (
                placement_policy.get("target_library_root_id")
                if isinstance(placement_policy, dict)
                else None
            )
            if (
                isinstance(target_root_id, int)
                and not isinstance(target_root_id, bool)
                and target_root_id > 0
            ):
                library_root_ids.add(target_root_id)

    missing_identity_keys = identity_keys - state.loaded_identity_keys
    if missing_identity_keys:
        loaded_identities = list(
            (
                await session.scalars(
                    select(StoryArcExternalIdentity).where(
                        tuple_(
                            StoryArcExternalIdentity.source,
                            StoryArcExternalIdentity.namespace,
                            StoryArcExternalIdentity.external_id,
                        ).in_(missing_identity_keys)
                    )
                )
            ).all()
        )
        for loaded_identity in loaded_identities:
            state.identities_by_key[_identity_key(loaded_identity)] = int(
                loaded_identity.story_arc_id
            )
        state.loaded_identity_keys.update(missing_identity_keys)
    for key in identity_keys:
        identity_arc_id = state.identities_by_key.get(key)
        if identity_arc_id is not None:
            target_arc_ids.add(identity_arc_id)

    for staged_arc in staged_arcs:
        recovered_arc_id = state.job_arc_ids_by_import_identity.get(
            (staged_arc.source_kind, staged_arc.source_key)
        )
        if recovered_arc_id is not None:
            target_arc_ids.add(recovered_arc_id)

    missing_arc_ids = target_arc_ids - state.arcs_by_id.keys()
    if missing_arc_ids:
        arcs = list(
            (await session.scalars(select(StoryArc).where(StoryArc.id.in_(missing_arc_ids)))).all()
        )
        for arc in arcs:
            state.arcs_by_id[int(arc.id)] = arc
            if arc.source_import_job_id == import_job_id:
                _index_job_arc(state, arc)

    existing_root_ids = (
        {
            int(root_id)
            for root_id in (
                await session.scalars(
                    select(LibraryRoot.id).where(
                        LibraryRoot.id.in_(library_root_ids),
                        LibraryRoot.enabled.is_(True),
                    )
                )
            ).all()
        }
        if library_root_ids
        else set()
    )
    return _MaterializationBatch(
        state=state,
        library_root_ids=existing_root_ids,
        identity_evidence=identity_evidence,
    )


async def _materialize_one_arc(
    session: AsyncSession,
    *,
    import_job_id: int,
    job: ImportJob,
    staged_arc: ImportedStoryArc,
    counts: _MutableCounts,
    warnings: list[StoryArcMaterializationWarning],
    record_action: RecordActionFunc | None,
    batch: _MaterializationBatch,
) -> _ArcMaterializationContext | None:
    arc_warning_start = len(warnings)
    arc_counts = _MutableCounts()
    policy = _validate_policy(staged_arc, library_root_ids=batch.library_root_ids)
    if policy.warning_code is not None:
        _warn(warnings, policy.warning_code, staged_arc)

    identities, identity_warning = batch.identity_evidence[int(staged_arc.id)]
    if identity_warning is not None:
        _warn(warnings, identity_warning, staged_arc)
        _increment_counts(counts, arc_counts, "arcs_failed")
        staged_arc.status = ImportedStoryArcStatus.FAILED
        _persist_arc_materialization_diagnostics(
            staged_arc,
            story_arc_id=None,
            status="failed",
            counts=_count_snapshot(arc_counts),
            warning_codes=[warning.code for warning in warnings[arc_warning_start:]],
        )
        return None

    arc, outcome, failure_code = await _resolve_or_create_arc(
        session,
        import_job_id=import_job_id,
        staged_arc=staged_arc,
        policy=policy,
        identities=identities,
        batch=batch,
    )
    if arc is None:
        _increment_counts(counts, arc_counts, "arcs_failed")
        staged_arc.status = ImportedStoryArcStatus.FAILED
        _warn(warnings, failure_code or "canonical_story_arc_unavailable", staged_arc)
        _persist_arc_materialization_diagnostics(
            staged_arc,
            story_arc_id=None,
            status="failed",
            counts=_count_snapshot(arc_counts),
            warning_codes=[warning.code for warning in warnings[arc_warning_start:]],
        )
        return None

    if outcome == "created":
        _increment_counts(counts, arc_counts, "arcs_created")
    elif outcome == "merged":
        _increment_counts(counts, arc_counts, "arcs_merged")
    else:
        _increment_counts(counts, arc_counts, "arcs_reused")

    policy_before = _story_arc_policy_state(arc)
    if policy.activated:
        policy_changed = _apply_policy(
            arc,
            policy,
            increment_revision=outcome != "created",
        )
    else:
        policy_changed = False
    staged_arc.materialized_story_arc_id = arc.id

    if outcome == "created" and record_action is not None:
        await record_action(
            session,
            job,
            phase="story_arcs",
            action_type="story_arc_created",
            payload={
                "story_arc_id": int(arc.id),
                "imported_story_arc_id": int(staged_arc.id),
                "expected_after": _story_arc_created_state(arc),
            },
        )
    elif policy_changed and record_action is not None:
        await session.flush()
        await record_action(
            session,
            job,
            phase="story_arcs",
            action_type="story_arc_policy_updated",
            payload={
                "story_arc_id": int(arc.id),
                "imported_story_arc_id": int(staged_arc.id),
                "restore_before": policy_before,
                "expected_after": _story_arc_policy_state(arc),
            },
        )

    for identity in identities:
        identity_outcome, materialized_identity = await _materialize_external_identity(
            session,
            arc=arc,
            staged_arc=staged_arc,
            identity=identity,
            state=batch.state,
        )
        if identity_outcome == "created":
            _increment_counts(counts, arc_counts, "external_identities_created")
            if record_action is not None and materialized_identity is not None:
                await record_action(
                    session,
                    job,
                    phase="story_arcs",
                    action_type="story_arc_external_identity_created",
                    payload={
                        "external_identity_id": int(materialized_identity.id),
                        "story_arc_id": int(arc.id),
                        "imported_story_arc_id": int(staged_arc.id),
                        "expected_after": _external_identity_state(materialized_identity),
                    },
                )
        elif identity_outcome == "reused":
            _increment_counts(counts, arc_counts, "external_identities_reused")
        else:
            _warn(warnings, "external_identity_conflict", staged_arc)

    return _ArcMaterializationContext(
        staged_arc=staged_arc,
        arc=arc,
        counts=arc_counts,
    )


async def _resolve_or_create_arc(
    session: AsyncSession,
    *,
    import_job_id: int,
    staged_arc: ImportedStoryArc,
    policy: _ValidatedPolicy,
    identities: Sequence[tuple[str, str, str]],
    batch: _MaterializationBatch,
) -> tuple[StoryArc | None, str | None, str | None]:
    if staged_arc.materialized_story_arc_id is not None:
        materialized = batch.state.arcs_by_id.get(int(staged_arc.materialized_story_arc_id))
        if materialized is not None:
            return materialized, "reused", None

    if staged_arc.proposed_story_arc_id is not None:
        proposed = batch.state.arcs_by_id.get(int(staged_arc.proposed_story_arc_id))
        if proposed is None:
            return None, None, "explicit_merge_target_missing"
        if proposed.lifecycle == StoryArcLifecycle.ARCHIVED:
            return None, None, "explicit_merge_target_archived"
        if _identity_targets_another_arc(batch.state, identities, int(proposed.id)):
            return None, None, "external_identity_requires_explicit_merge_review"
        return proposed, "merged", None

    recovered = _recover_arc_created_by_this_job(
        batch.state,
        import_job_id=import_job_id,
        staged_arc=staged_arc,
        identities=identities,
    )
    if recovered is not None:
        return recovered, "reused", None
    if _any_external_identity_exists(batch.state, identities):
        return None, None, "external_identity_requires_explicit_merge_review"
    if staged_arc.name is None or not staged_arc.name.strip():
        return None, None, "canonical_story_arc_name_missing"

    service = StoryArcService()
    try:
        arc = await service.create(
            session,
            name=staged_arc.name,
            description=staged_arc.description,
            monitored=policy.monitored if policy.activated else False,
            search_missing=policy.search_missing if policy.activated else False,
            include_upcoming=policy.include_upcoming if policy.activated else False,
            sync_enabled=policy.sync_enabled if policy.activated else False,
            source_kind=staged_arc.source_kind,
        )
    except (StoryArcServiceError, ValueError):
        return None, None, "canonical_story_arc_validation_failed"
    arc.source_import_job_id = import_job_id
    arc.diagnostics = {
        "schema_version": 1,
        "import_identity": {
            "import_job_id": import_job_id,
            "source_key": staged_arc.source_key,
        },
    }
    if policy.activated:
        _apply_policy(arc, policy, increment_revision=False)
    await session.flush()
    batch.state.arcs_by_id[int(arc.id)] = arc
    _index_job_arc(batch.state, arc)
    return arc, "created", None


def _recover_arc_created_by_this_job(
    state: _MaterializationState,
    *,
    import_job_id: int,
    staged_arc: ImportedStoryArc,
    identities: Sequence[tuple[str, str, str]],
) -> StoryArc | None:
    identity_arc_ids = _external_identity_arc_ids(state, identities)
    if len(identity_arc_ids) == 1:
        arc = state.arcs_by_id.get(next(iter(identity_arc_ids)))
        if arc is not None and _arc_matches_import_identity(arc, import_job_id, staged_arc):
            return arc

    recovered_arc_id = state.job_arc_ids_by_import_identity.get(
        (staged_arc.source_kind, staged_arc.source_key)
    )
    return state.arcs_by_id.get(recovered_arc_id) if recovered_arc_id is not None else None


def _arc_matches_import_identity(
    arc: StoryArc,
    import_job_id: int,
    staged_arc: ImportedStoryArc,
) -> bool:
    identity = _mapping(_mapping(arc.diagnostics).get("import_identity"))
    return (
        arc.source_import_job_id == import_job_id
        and identity.get("import_job_id") == import_job_id
        and identity.get("source_key") == staged_arc.source_key
    )


def _index_job_arc(state: _MaterializationState, arc: StoryArc) -> None:
    _index_job_arc_values(
        state,
        arc_id=int(arc.id),
        source_kind=arc.source_kind,
        source_import_job_id=arc.source_import_job_id,
        diagnostics=arc.diagnostics,
    )


def _index_job_arc_values(
    state: _MaterializationState,
    *,
    arc_id: int,
    source_kind: StoryArcSourceKind,
    source_import_job_id: int | None,
    diagnostics: object,
) -> None:
    identity = _mapping(_mapping(diagnostics).get("import_identity"))
    source_key = identity.get("source_key")
    if (
        not isinstance(source_key, str)
        or not source_key
        or identity.get("import_job_id") != source_import_job_id
    ):
        return
    key = (source_kind, source_key)
    if key not in state.job_arc_ids_by_import_identity:
        state.job_arc_ids_by_import_identity[key] = arc_id
        return
    existing_arc_id = state.job_arc_ids_by_import_identity[key]
    if existing_arc_id is None or existing_arc_id != arc_id:
        state.job_arc_ids_by_import_identity[key] = None


async def _materialize_entry_pages(
    session: AsyncSession,
    *,
    import_job_id: int,
    job: ImportJob,
    contexts: Mapping[int, _ArcMaterializationContext],
    counts: _MutableCounts,
    warnings: list[StoryArcMaterializationWarning],
    record_action: RecordActionFunc | None,
    entry_page_size: int,
    cancellation_check: CancellationCheck | None,
) -> None:
    if not contexts:
        return
    staged_arc_ids = list(contexts)
    last_entry_id = 0
    while True:
        await _checkpoint(cancellation_check)
        selected_entries = list(
            (
                await session.scalars(
                    select(ImportedStoryArcEntry)
                    .where(
                        ImportedStoryArcEntry.imported_story_arc_id.in_(staged_arc_ids),
                        ImportedStoryArcEntry.selected_for_import.is_(True),
                        ImportedStoryArcEntry.id > last_entry_id,
                    )
                    .options(selectinload(ImportedStoryArcEntry.import_file))
                    .order_by(ImportedStoryArcEntry.id)
                    .limit(entry_page_size)
                )
            ).all()
        )
        if not selected_entries:
            return
        lookups = await _prepare_entry_page_lookups(
            session,
            import_job_id=import_job_id,
            job=job,
            contexts=contexts,
            entries=selected_entries,
        )
        reference_checkpoint = _ReferencePageCheckpoint(cancellation_check)
        for entry in selected_entries:
            context = contexts[int(entry.imported_story_arc_id)]
            await _materialize_entry(
                session,
                import_job_id=import_job_id,
                job=job,
                context=context,
                entry=entry,
                counts=counts,
                warnings=warnings,
                record_action=record_action,
                lookups=lookups,
                reference_checkpoint=reference_checkpoint,
            )
        await session.flush()
        last_entry_id = int(selected_entries[-1].id)


async def _prepare_entry_page_lookups(
    session: AsyncSession,
    *,
    import_job_id: int,
    job: ImportJob,
    contexts: Mapping[int, _ArcMaterializationContext],
    entries: Sequence[ImportedStoryArcEntry],
) -> _EntryPageLookups:
    issue_ids = _candidate_issue_ids(entries, import_job_id=import_job_id)
    issues = (
        {
            int(issue.id): issue
            for issue in (await session.scalars(select(Issue).where(Issue.id.in_(issue_ids)))).all()
        }
        if issue_ids
        else {}
    )
    pointer_ids = {
        int(entry.materialized_membership_id)
        for entry in entries
        if entry.materialized_membership_id is not None
    }
    arc_issue_pairs: set[tuple[int, int]] = set()
    arc_source_pairs: set[tuple[int, StoryArcSourceKind, str]] = set()
    for entry in entries:
        arc_id = int(contexts[int(entry.imported_story_arc_id)].arc.id)
        for issue_id in _candidate_issue_ids((entry,), import_job_id=import_job_id):
            arc_issue_pairs.add((arc_id, issue_id))
        if entry.source_entry_id is not None:
            arc_source_pairs.add((arc_id, entry.source_kind, entry.source_entry_id))

    filters: list[ColumnElement[bool]] = []
    if pointer_ids:
        filters.append(IssueStoryArc.id.in_(pointer_ids))
    if arc_issue_pairs:
        filters.append(
            tuple_(IssueStoryArc.story_arc_id, IssueStoryArc.issue_id).in_(arc_issue_pairs)
        )
    if arc_source_pairs:
        filters.append(
            tuple_(
                IssueStoryArc.story_arc_id,
                IssueStoryArc.source_kind,
                IssueStoryArc.source_entry_id,
            ).in_(arc_source_pairs)
        )
    memberships = (
        list(
            (
                await session.scalars(
                    select(IssueStoryArc).where(or_(*filters)).order_by(IssueStoryArc.id)
                )
            ).all()
        )
        if filters
        else []
    )
    reference_candidates = {
        int(entry.id): _resolve_reference_candidate(job, entry)
        for entry in entries
        if entry.source_location is not None
    }
    candidate_paths = {
        str(resolution.candidate.path)
        for resolution in reference_candidates.values()
        if resolution.candidate is not None
    }
    membership_ids = {int(membership.id) for membership in memberships}
    placement_filters: list[ColumnElement[bool]] = []
    if candidate_paths:
        placement_filters.append(StoryArcPlacement.placement_path.in_(candidate_paths))
    if membership_ids:
        placement_filters.append(StoryArcPlacement.issue_story_arc_id.in_(membership_ids))
    placements = (
        list(
            (
                await session.scalars(
                    select(StoryArcPlacement)
                    .where(or_(*placement_filters))
                    .order_by(StoryArcPlacement.id)
                )
            ).all()
        )
        if placement_filters
        else []
    )
    placements_by_membership: dict[int, list[StoryArcPlacement]] = {}
    for placement in placements:
        placements_by_membership.setdefault(int(placement.issue_story_arc_id), []).append(placement)
    return _EntryPageLookups(
        issues_by_id=issues,
        memberships_by_id={int(membership.id): membership for membership in memberships},
        memberships_by_arc_issue={
            (int(membership.story_arc_id), int(membership.issue_id)): membership
            for membership in memberships
            if membership.issue_id is not None
        },
        memberships_by_arc_source={
            (
                int(membership.story_arc_id),
                membership.source_kind,
                membership.source_entry_id,
            ): membership
            for membership in memberships
            if membership.source_entry_id is not None
        },
        reference_candidates_by_entry_id=reference_candidates,
        placements_by_path={placement.placement_path: placement for placement in placements},
        placements_by_membership=placements_by_membership,
    )


async def _materialize_entry(
    session: AsyncSession,
    *,
    import_job_id: int,
    job: ImportJob,
    context: _ArcMaterializationContext,
    entry: ImportedStoryArcEntry,
    counts: _MutableCounts,
    warnings: list[StoryArcMaterializationWarning],
    record_action: RecordActionFunc | None,
    lookups: _EntryPageLookups,
    reference_checkpoint: _ReferencePageCheckpoint,
) -> None:
    staged_arc = context.staged_arc
    arc = context.arc
    entry_warning_start = len(warnings)
    issue_id, issue_warning = _resolved_issue_id(
        entry,
        issues=lookups.issues_by_id,
        import_job_id=import_job_id,
    )
    if issue_warning is not None:
        _warn(warnings, issue_warning, staged_arc, entry)
    if entry.resolution_state in {
        StoryArcResolutionState.AMBIGUOUS,
        StoryArcResolutionState.CONFLICT,
        StoryArcResolutionState.SKIPPED,
    }:
        issue_id = None

    arc_id = int(arc.id)
    membership = _membership_from_pointer(lookups.memberships_by_id, entry, arc_id)
    if membership is None and issue_id is not None:
        membership = lookups.memberships_by_arc_issue.get((arc_id, issue_id))
    source_identity_conflict = False
    if membership is None and entry.source_entry_id is not None:
        membership = lookups.memberships_by_arc_source.get(
            (arc_id, entry.source_kind, entry.source_entry_id)
        )
        if (
            membership is not None
            and issue_id is not None
            and membership.issue_id is not None
            and membership.issue_id != issue_id
        ):
            _warn(warnings, "source_entry_identity_conflict", staged_arc, entry)
            membership = None
            issue_id = None
            source_identity_conflict = True

    if membership is not None:
        membership_before = _membership_state(membership)
        arc_revision_before = int(arc.revision)
        same_source_entry = (
            membership.source_kind == entry.source_kind
            and membership.source_entry_id == entry.source_entry_id
        )
        if issue_id is not None and membership.issue_id is None:
            duplicate = lookups.memberships_by_arc_issue.get((arc_id, issue_id))
            if duplicate is None or duplicate.id == membership.id:
                membership.issue_id = issue_id
                membership.resolution_state = StoryArcResolutionState.RESOLVED
                membership.sync_eligible = bool(arc.sync_enabled)
                lookups.memberships_by_arc_issue[(arc_id, issue_id)] = membership
                arc.revision += 1
        membership_changed = membership_before != _membership_state(membership)
        if membership_changed and record_action is not None:
            await session.flush()
            await record_action(
                session,
                job,
                phase="story_arcs",
                action_type="story_arc_membership_updated",
                payload={
                    "membership_id": int(membership.id),
                    "story_arc_id": arc_id,
                    "imported_story_arc_entry_id": int(entry.id),
                    "restore_before": membership_before,
                    "expected_after": _membership_state(membership),
                    "arc_revision_before": arc_revision_before,
                    "arc_revision_after": int(arc.revision),
                },
            )
        if not same_source_entry and issue_id is not None:
            _warn(warnings, "duplicate_issue_membership_reused", staged_arc, entry)
        entry.materialized_membership_id = membership.id
        _increment_counts(counts, context.counts, "memberships_reused")
    else:
        arc_revision_before = int(arc.revision)
        issue = lookups.issues_by_id.get(issue_id) if issue_id is not None else None
        exact_number, exact_warning = _exact_issue_number(entry, issue)
        if exact_warning is not None:
            _warn(warnings, exact_warning, staged_arc, entry)
        resolution_state = (
            StoryArcResolutionState.CONFLICT
            if source_identity_conflict
            else _materialized_resolution_state(entry, issue_id)
        )
        sequence_number = (
            int(entry.reading_order)
            if entry.reading_order is not None
            else int(entry.source_ordinal)
        )
        if entry.reading_order is None:
            _warn(warnings, "reading_order_defaulted_to_source_ordinal", staged_arc, entry)
        membership = IssueStoryArc(
            story_arc_id=arc.id,
            issue_id=issue_id,
            sequence_number=sequence_number,
            source_ordinal=int(entry.source_ordinal),
            legacy_sequence_was_null=entry.reading_order is None,
            resolution_state=resolution_state,
            source_kind=entry.source_kind,
            source_entry_id=entry.source_entry_id,
            source_arc_id=entry.source_arc_id,
            source_issue_id=entry.source_issue_id,
            source_series_id=entry.source_series_id,
            source_issue_number_text=exact_number,
            source_series_name=entry.source_series_name,
            source_issue_title=entry.source_issue_title,
            source_publisher=entry.source_publisher,
            source_release_date_text=entry.source_release_date_text,
            source_issue_date_text=entry.source_issue_date_text,
            resolution_confidence=entry.resolution_confidence,
            resolution_method=entry.resolution_method,
            evidence=dict(entry.evidence or {}),
            sync_eligible=(
                issue_id is not None
                and resolution_state == StoryArcResolutionState.RESOLVED
                and bool(arc.sync_enabled)
            ),
        )
        session.add(membership)
        await session.flush()
        entry.materialized_membership_id = membership.id
        lookups.memberships_by_id[int(membership.id)] = membership
        if membership.issue_id is not None:
            lookups.memberships_by_arc_issue[(arc_id, int(membership.issue_id))] = membership
        if membership.source_entry_id is not None:
            lookups.memberships_by_arc_source[
                (arc_id, membership.source_kind, membership.source_entry_id)
            ] = membership
        _increment_counts(counts, context.counts, "memberships_created")
        arc.revision += 1
        if record_action is not None:
            await session.flush()
            await record_action(
                session,
                job,
                phase="story_arcs",
                action_type="story_arc_membership_created",
                payload={
                    "membership_id": int(membership.id),
                    "story_arc_id": arc_id,
                    "imported_story_arc_entry_id": int(entry.id),
                    "expected_after": _membership_state(membership),
                    "arc_revision_before": arc_revision_before,
                    "arc_revision_after": int(arc.revision),
                },
            )

    if membership.resolution_state == StoryArcResolutionState.RESOLVED:
        _increment_counts(counts, context.counts, "resolved_entries")
    else:
        _increment_counts(counts, context.counts, "unresolved_entries")
    if membership.resolution_state == StoryArcResolutionState.SKIPPED:
        _increment_counts(counts, context.counts, "entries_skipped")
    await _materialize_referenced_placement(
        session,
        job=job,
        staged_arc=staged_arc,
        entry=entry,
        membership=membership,
        warnings=warnings,
        record_action=record_action,
        lookups=lookups,
        reference_checkpoint=reference_checkpoint,
    )
    _persist_entry_materialization_diagnostics(
        entry,
        membership_id=int(membership.id),
        warning_codes=[warning.code for warning in warnings[entry_warning_start:]],
    )


async def _materialize_referenced_placement(
    session: AsyncSession,
    *,
    job: ImportJob,
    staged_arc: ImportedStoryArc,
    entry: ImportedStoryArcEntry,
    membership: IssueStoryArc,
    warnings: list[StoryArcMaterializationWarning],
    record_action: RecordActionFunc | None,
    lookups: _EntryPageLookups,
    reference_checkpoint: _ReferencePageCheckpoint,
) -> None:
    """Attach one confirmed pre-existing artifact without opening or mutating it."""
    resolution = lookups.reference_candidates_by_entry_id.get(int(entry.id))
    if resolution is None:
        return
    await reference_checkpoint.ensure_checked()
    if resolution.warning_code is not None or resolution.candidate is None:
        _warn(
            warnings,
            resolution.warning_code or "story_arc_reference_path_invalid",
            staged_arc,
            entry,
        )
        return

    candidate = resolution.candidate
    placement_path = str(candidate.path)
    existing = lookups.placements_by_path.get(placement_path)
    if existing is not None and not _is_same_import_reference(
        existing,
        job=job,
        entry=entry,
        membership=membership,
    ):
        _warn(warnings, "story_arc_reference_path_collision", staged_arc, entry)
        return
    if existing is None:
        prior_for_membership = [
            placement
            for placement in lookups.placements_by_membership.get(int(membership.id), ())
            if _is_same_import_reference(
                placement,
                job=job,
                entry=entry,
                membership=membership,
            )
        ]
        if prior_for_membership:
            _warn(warnings, "story_arc_reference_location_changed", staged_arc, entry)
            return

    inspection = _inspect_reference_path(candidate)
    if existing is not None:
        if existing.creating_action_id is None:
            _warn(warnings, "story_arc_reference_provenance_incomplete", staged_arc, entry)
            return
        _refresh_referenced_placement(
            existing,
            inspection=inspection,
            staged_arc=staged_arc,
            entry=entry,
            warnings=warnings,
        )
        return
    if inspection.warning_code is not None or inspection.fingerprint is None:
        _warn(
            warnings,
            inspection.warning_code or "story_arc_reference_path_invalid",
            staged_arc,
            entry,
        )
        return
    if record_action is None:
        _warn(warnings, "story_arc_reference_journal_unavailable", staged_arc, entry)
        return

    prepared_payload: dict[str, object] = {
        "schema_version": 1,
        "journal_state": "prepared",
        "placement_id": None,
        "issue_story_arc_id": int(membership.id),
        "imported_story_arc_entry_id": int(entry.id),
        "placement_path": placement_path,
        "source_kind": entry.source_kind.value,
        "source_import_job_id": int(job.id),
        "expected_after": None,
    }
    action = await record_action(
        session,
        job,
        phase="story_arcs",
        action_type="story_arc_referenced_placement_attached",
        payload=prepared_payload,
    )
    now = datetime.now(UTC)
    last_result = _reference_last_result(
        code="reference_current",
        baseline=inspection.fingerprint,
        observed=inspection.fingerprint,
    )
    imported_file = entry.import_file
    library_file_id = (
        int(imported_file.library_file_id)
        if imported_file is not None
        and imported_file.import_job_id == job.id
        and imported_file.library_file_id is not None
        else None
    )
    placement = StoryArcPlacement(
        issue_story_arc_id=int(membership.id),
        library_file_id=library_file_id,
        placement_path=placement_path,
        mode=StoryArcPlacementMode.REFERENCE_ONLY,
        ownership=StoryArcPlacementOwnership.REFERENCED,
        symlink_style=None,
        source_kind=entry.source_kind,
        source_import_job_id=int(job.id),
        creating_action_id=int(action.id),
        rendered_reading_order=int(membership.sequence_number),
        source_fingerprint={},
        state=StoryArcPlacementState.CURRENT,
        last_result=last_result,
        last_checked_at=now,
    )
    session.add(placement)
    await session.flush()
    action.payload = {
        **prepared_payload,
        "journal_state": "completed",
        "placement_id": int(placement.id),
        "expected_after": _referenced_placement_state(placement),
    }
    await session.flush()
    lookups.placements_by_path[placement_path] = placement
    lookups.placements_by_membership.setdefault(int(membership.id), []).append(placement)


def _refresh_referenced_placement(
    placement: StoryArcPlacement,
    *,
    inspection: _ReferencePathInspection,
    staged_arc: ImportedStoryArc,
    entry: ImportedStoryArcEntry,
    warnings: list[StoryArcMaterializationWarning],
) -> None:
    baseline = _reference_baseline_fingerprint(placement)
    now = datetime.now(UTC)
    placement.last_checked_at = now
    if inspection.warning_code is not None or inspection.fingerprint is None:
        missing = inspection.warning_code == "story_arc_reference_missing"
        placement.state = (
            StoryArcPlacementState.MISSING if missing else StoryArcPlacementState.DRIFTED
        )
        placement.last_result = _reference_last_result(
            code="reference_missing" if missing else "reference_unsafe",
            baseline=baseline,
            observed=None,
            warning_code=inspection.warning_code,
        )
        _warn(
            warnings,
            inspection.warning_code or "story_arc_reference_path_invalid",
            staged_arc,
            entry,
        )
        return
    if baseline is None:
        placement.state = StoryArcPlacementState.DRIFTED
        placement.last_result = _reference_last_result(
            code="reference_provenance_incomplete",
            baseline=None,
            observed=inspection.fingerprint,
        )
        _warn(warnings, "story_arc_reference_provenance_incomplete", staged_arc, entry)
        return
    if baseline != inspection.fingerprint:
        placement.state = StoryArcPlacementState.DRIFTED
        placement.last_result = _reference_last_result(
            code="reference_drifted",
            baseline=baseline,
            observed=inspection.fingerprint,
        )
        _warn(warnings, "story_arc_reference_drifted", staged_arc, entry)
        return
    placement.state = StoryArcPlacementState.CURRENT
    placement.last_result = _reference_last_result(
        code="reference_current",
        baseline=baseline,
        observed=inspection.fingerprint,
    )


def _is_same_import_reference(
    placement: StoryArcPlacement,
    *,
    job: ImportJob,
    entry: ImportedStoryArcEntry,
    membership: IssueStoryArc,
) -> bool:
    return (
        placement.issue_story_arc_id == membership.id
        and placement.mode is StoryArcPlacementMode.REFERENCE_ONLY
        and placement.ownership is StoryArcPlacementOwnership.REFERENCED
        and placement.source_kind is entry.source_kind
        and placement.source_import_job_id == job.id
    )


def _reference_baseline_fingerprint(
    placement: StoryArcPlacement,
) -> dict[str, object] | None:
    baseline = _mapping(placement.last_result).get("baseline_fingerprint")
    return dict(baseline) if isinstance(baseline, dict) else None


def _reference_last_result(
    *,
    code: str,
    baseline: Mapping[str, object] | None,
    observed: Mapping[str, object] | None,
    warning_code: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "code": code,
        "baseline_fingerprint": dict(baseline) if baseline is not None else None,
        "observed_fingerprint": dict(observed) if observed is not None else None,
        "warning_code": warning_code,
    }


def _referenced_placement_state(placement: StoryArcPlacement) -> dict[str, object]:
    return {
        "issue_story_arc_id": int(placement.issue_story_arc_id),
        "library_file_id": placement.library_file_id,
        "placement_path": placement.placement_path,
        "mode": placement.mode.value,
        "ownership": placement.ownership.value,
        "symlink_style": None,
        "source_kind": placement.source_kind.value,
        "source_import_job_id": placement.source_import_job_id,
        "creating_action_id": placement.creating_action_id,
        "rendered_reading_order": placement.rendered_reading_order,
        "source_fingerprint": dict(placement.source_fingerprint or {}),
        "state": placement.state.value,
        "last_result": dict(placement.last_result or {}),
    }


def _resolve_reference_candidate(
    job: ImportJob,
    entry: ImportedStoryArcEntry,
) -> _ReferenceCandidateResolution:
    raw_location = entry.source_location
    if raw_location is None:
        return _ReferenceCandidateResolution()
    if _unsafe_path_text(raw_location):
        return _ReferenceCandidateResolution(warning_code="story_arc_reference_path_invalid")
    if (
        job.source_type is ImportSourceType.FILESYSTEM
        and entry.source_kind is StoryArcSourceKind.FOLDER
    ):
        return _candidate_under_trusted_root(
            raw_location=raw_location,
            trusted_root=job.source_path,
        )
    if (
        job.source_type is ImportSourceType.MYLAR3
        and entry.source_kind is StoryArcSourceKind.MYLAR3
    ):
        return _mapped_mylar_reference_candidate(
            raw_location=raw_location,
            path_map=job.mylar3_path_map,
        )
    return _ReferenceCandidateResolution(warning_code="story_arc_reference_source_mismatch")


def _candidate_under_trusted_root(
    *,
    raw_location: str,
    trusted_root: str,
) -> _ReferenceCandidateResolution:
    if _unsafe_path_text(trusted_root):
        return _ReferenceCandidateResolution(warning_code="story_arc_reference_root_untrusted")
    root = Path(trusted_root)
    candidate = Path(raw_location)
    if (
        not root.is_absolute()
        or not candidate.is_absolute()
        or root == Path(root.anchor)
        or ".." in root.parts
        or ".." in candidate.parts
    ):
        return _ReferenceCandidateResolution(warning_code="story_arc_reference_path_invalid")
    normalized_root = Path(os.path.abspath(root))
    normalized_candidate = Path(os.path.abspath(candidate))
    try:
        normalized_candidate.relative_to(normalized_root)
    except ValueError:
        return _ReferenceCandidateResolution(
            warning_code="story_arc_reference_outside_trusted_root"
        )
    if normalized_candidate == normalized_root:
        return _ReferenceCandidateResolution(warning_code="story_arc_reference_not_regular_file")
    if len(str(normalized_candidate)) > 1000:
        return _ReferenceCandidateResolution(warning_code="story_arc_reference_path_invalid")
    return _ReferenceCandidateResolution(
        candidate=_ReferencePathCandidate(
            path=normalized_candidate,
            trusted_root=normalized_root,
        )
    )


def _mapped_mylar_reference_candidate(
    *,
    raw_location: str,
    path_map: object,
) -> _ReferenceCandidateResolution:
    if not isinstance(path_map, dict) or not path_map:
        return _ReferenceCandidateResolution(warning_code="story_arc_reference_root_untrusted")
    mapping_items: list[tuple[PurePath, str, Path]] = []
    for raw_remote_root, raw_host_root in path_map.items():
        if not isinstance(raw_remote_root, str) or not isinstance(raw_host_root, str):
            continue
        if _unsafe_path_text(raw_remote_root) or _unsafe_path_text(raw_host_root):
            continue
        remote_root = _pure_absolute_path(raw_remote_root)
        host_root = Path(raw_host_root)
        if (
            remote_root is None
            or not host_root.is_absolute()
            or host_root == Path(host_root.anchor)
            or ".." in host_root.parts
        ):
            continue
        mapping_items.append((remote_root, raw_host_root, Path(os.path.abspath(host_root))))
    if not mapping_items:
        return _ReferenceCandidateResolution(warning_code="story_arc_reference_root_untrusted")

    direct_candidates: list[_ReferenceCandidateResolution] = []
    for _remote_root, raw_host_root, _normalized_host_root in mapping_items:
        direct = _candidate_under_trusted_root(
            raw_location=raw_location,
            trusted_root=raw_host_root,
        )
        if direct.candidate is not None:
            direct_candidates.append(direct)
    if direct_candidates:
        return max(
            direct_candidates,
            key=lambda item: (
                len(item.candidate.trusted_root.parts) if item.candidate is not None else 0
            ),
        )

    remote_location = _pure_absolute_path(raw_location)
    if remote_location is None or ".." in remote_location.parts:
        return _ReferenceCandidateResolution(warning_code="story_arc_reference_path_invalid")
    for remote_root, _raw_host_root, host_root in sorted(
        mapping_items,
        key=lambda item: len(item[0].parts),
        reverse=True,
    ):
        if type(remote_location) is not type(remote_root):
            continue
        try:
            relative = remote_location.relative_to(remote_root)
        except ValueError:
            continue
        if not relative.parts or ".." in relative.parts:
            return _ReferenceCandidateResolution(
                warning_code="story_arc_reference_not_regular_file"
            )
        candidate = host_root.joinpath(*relative.parts)
        if len(str(candidate)) > 1000:
            return _ReferenceCandidateResolution(warning_code="story_arc_reference_path_invalid")
        return _ReferenceCandidateResolution(
            candidate=_ReferencePathCandidate(
                path=candidate,
                trusted_root=host_root,
            )
        )
    return _ReferenceCandidateResolution(warning_code="story_arc_reference_outside_trusted_root")


def _pure_absolute_path(value: str) -> PurePath | None:
    windows_path = PureWindowsPath(value)
    if windows_path.is_absolute():
        return windows_path
    posix_path = PurePosixPath(value.replace("\\", "/"))
    return posix_path if posix_path.is_absolute() else None


def _unsafe_path_text(value: str) -> bool:
    return not value or any(ord(character) < 32 for character in value)


def _inspect_reference_path(
    candidate: _ReferencePathCandidate,
) -> _ReferencePathInspection:
    if not _secure_reference_inspection_supported():
        return _ReferencePathInspection(
            warning_code="story_arc_reference_secure_inspection_unavailable"
        )
    try:
        before = _secure_reference_stat(candidate)
        after = _secure_reference_stat(candidate)
    except _ReferencePathValidationError as exc:
        return _ReferencePathInspection(warning_code=exc.code)
    if _reference_stat_identity(before) != _reference_stat_identity(after):
        return _ReferencePathInspection(
            warning_code="story_arc_reference_changed_during_inspection"
        )
    return _ReferencePathInspection(fingerprint=_reference_metadata_fingerprint(after))


class _ReferencePathValidationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _secure_reference_inspection_supported() -> bool:
    return bool(
        os.name == "posix"
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
    )


def _secure_reference_stat(candidate: _ReferencePathCandidate) -> os.stat_result:
    root = candidate.trusted_root
    try:
        relative = candidate.path.relative_to(root)
    except ValueError as exc:  # pragma: no cover - candidate constructor invariant
        raise _ReferencePathValidationError("story_arc_reference_outside_trusted_root") from exc
    if not relative.parts:
        raise _ReferencePathValidationError("story_arc_reference_not_regular_file")
    try:
        root_stat = root.lstat()
    except FileNotFoundError as exc:
        raise _ReferencePathValidationError("story_arc_reference_missing") from exc
    except OSError as exc:
        raise _ReferencePathValidationError("story_arc_reference_unavailable") from exc
    if stat.S_ISLNK(root_stat.st_mode):
        raise _ReferencePathValidationError("story_arc_reference_symlink")
    if not stat.S_ISDIR(root_stat.st_mode):
        raise _ReferencePathValidationError("story_arc_reference_root_untrusted")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptors: list[int] = []
    try:
        root_fd = os.open(root, flags)
        descriptors.append(root_fd)
        opened_root = os.fstat(root_fd)
        if _reference_stat_node(root_stat) != _reference_stat_node(opened_root):
            raise _ReferencePathValidationError("story_arc_reference_changed_during_inspection")
        parent_fd = root_fd
        for part in relative.parts[:-1]:
            child_stat = _stat_child_nofollow(parent_fd, part)
            if stat.S_ISLNK(child_stat.st_mode):
                raise _ReferencePathValidationError("story_arc_reference_symlink")
            if not stat.S_ISDIR(child_stat.st_mode):
                raise _ReferencePathValidationError("story_arc_reference_not_regular_file")
            try:
                child_fd = os.open(part, flags, dir_fd=parent_fd)
            except FileNotFoundError as exc:
                raise _ReferencePathValidationError("story_arc_reference_missing") from exc
            except OSError as exc:
                raise _ReferencePathValidationError(
                    "story_arc_reference_changed_during_inspection"
                ) from exc
            descriptors.append(child_fd)
            opened_child = os.fstat(child_fd)
            if _reference_stat_node(child_stat) != _reference_stat_node(opened_child):
                raise _ReferencePathValidationError("story_arc_reference_changed_during_inspection")
            parent_fd = child_fd
        target_stat = _stat_child_nofollow(parent_fd, relative.parts[-1])
        if stat.S_ISLNK(target_stat.st_mode):
            raise _ReferencePathValidationError("story_arc_reference_symlink")
        if not stat.S_ISREG(target_stat.st_mode):
            raise _ReferencePathValidationError("story_arc_reference_not_regular_file")
        return target_stat
    except _ReferencePathValidationError:
        raise
    except FileNotFoundError as exc:
        raise _ReferencePathValidationError("story_arc_reference_missing") from exc
    except OSError as exc:
        raise _ReferencePathValidationError("story_arc_reference_unavailable") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _stat_child_nofollow(parent_fd: int, name: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise _ReferencePathValidationError("story_arc_reference_missing") from exc
    except OSError as exc:
        raise _ReferencePathValidationError("story_arc_reference_unavailable") from exc


def _reference_stat_node(value: os.stat_result) -> tuple[int, int, int]:
    return value.st_dev, value.st_ino, value.st_mode


def _reference_stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _reference_metadata_fingerprint(value: os.stat_result) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "regular_metadata",
        "size": int(value.st_size),
        "mtime_ns": int(value.st_mtime_ns),
        "ctime_ns": int(value.st_ctime_ns),
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
    }


def _candidate_issue_ids(
    entries: Sequence[ImportedStoryArcEntry],
    *,
    import_job_id: int,
) -> set[int]:
    result: set[int] = set()
    for entry in entries:
        if entry.matched_issue_id is not None:
            result.add(int(entry.matched_issue_id))
        imported_file = entry.import_file
        if (
            imported_file is not None
            and imported_file.import_job_id == import_job_id
            and imported_file.status in _SAFE_IMPORTED_FILE_STATES
            and imported_file.matched_issue_id is not None
        ):
            result.add(int(imported_file.matched_issue_id))
    return result


def _resolved_issue_id(
    entry: ImportedStoryArcEntry,
    *,
    issues: Mapping[int, Issue],
    import_job_id: int,
) -> tuple[int | None, str | None]:
    direct_id = int(entry.matched_issue_id) if entry.matched_issue_id is not None else None
    imported_file = entry.import_file
    file_id: int | None = None
    if imported_file is not None:
        if imported_file.import_job_id != import_job_id:
            return direct_id if direct_id in issues else None, "import_file_job_mismatch"
        if (
            imported_file.status in _SAFE_IMPORTED_FILE_STATES
            and imported_file.matched_issue_id is not None
        ):
            file_id = int(imported_file.matched_issue_id)
        elif direct_id is None and imported_file.matched_issue_id is not None:
            return None, "import_file_match_not_materialized"

    if direct_id is not None and file_id is not None and direct_id != file_id:
        return None, "matched_issue_identity_conflict"
    candidate = direct_id if direct_id is not None else file_id
    if candidate is None:
        return None, None
    if candidate not in issues:
        return None, "matched_issue_missing"
    return candidate, None


def _membership_from_pointer(
    memberships_by_id: Mapping[int, IssueStoryArc],
    entry: ImportedStoryArcEntry,
    story_arc_id: int,
) -> IssueStoryArc | None:
    if entry.materialized_membership_id is None:
        return None
    membership = memberships_by_id.get(int(entry.materialized_membership_id))
    if membership is None or membership.story_arc_id != story_arc_id:
        return None
    return membership


def _exact_issue_number(
    entry: ImportedStoryArcEntry,
    issue: Issue | None,
) -> tuple[str | None, str | None]:
    if entry.source_issue_number_text is not None:
        try:
            return normalize_issue_number_text(entry.source_issue_number_text), None
        except ValueError:
            return (
                issue.effective_issue_number_text if issue is not None else None,
                "source_issue_number_invalid",
            )
    if issue is not None:
        return issue.effective_issue_number_text, None
    return None, "source_issue_number_missing"


def _materialized_resolution_state(
    entry: ImportedStoryArcEntry,
    issue_id: int | None,
) -> StoryArcResolutionState:
    if issue_id is not None:
        return StoryArcResolutionState.RESOLVED
    if entry.resolution_state == StoryArcResolutionState.RESOLVED:
        return StoryArcResolutionState.MISSING
    if entry.resolution_state in _UNRESOLVED_STATES:
        return entry.resolution_state
    return StoryArcResolutionState.PENDING


def _validate_policy(
    staged_arc: ImportedStoryArc,
    *,
    library_root_ids: set[int],
) -> _ValidatedPolicy:
    raw = staged_arc.proposed_policy_snapshot
    if not isinstance(raw, dict) or not raw:
        return _ValidatedPolicy(activated=False, snapshot={})
    if raw.get("activation") != "confirmed":
        return _ValidatedPolicy(
            activated=False,
            snapshot={},
            warning_code="policy_not_activated",
        )
    if (
        set(raw) != _CONFIRMED_POLICY_KEYS
        or isinstance(raw.get("schema_version"), bool)
        or raw.get("schema_version") != 1
        or raw.get("source") != staged_arc.source_kind.value
        or any(not isinstance(raw.get(key), bool) for key in _POLICY_FLAGS)
        or (
            raw.get("monitored") is False
            and (raw.get("search_missing") is True or raw.get("include_upcoming") is True)
        )
    ):
        return _ValidatedPolicy(
            activated=False,
            snapshot={},
            warning_code="policy_validation_failed",
        )
    placement_snapshot, target_root_id, placement_warning = _validate_placement_policy_snapshot(
        raw.get("placement_policy"),
        library_root_ids=library_root_ids,
    )
    if placement_warning is not None or placement_snapshot is None:
        return _ValidatedPolicy(
            activated=False,
            snapshot={},
            warning_code=placement_warning or "policy_validation_failed",
        )
    if raw["sync_enabled"] != placement_snapshot["synchronize"]:
        return _ValidatedPolicy(
            activated=False,
            snapshot={},
            warning_code="policy_validation_failed",
        )
    return _ValidatedPolicy(
        activated=True,
        snapshot=placement_snapshot,
        monitored=bool(raw["monitored"]),
        search_missing=bool(raw["search_missing"]),
        include_upcoming=bool(raw["include_upcoming"]),
        sync_enabled=bool(raw["sync_enabled"]),
        target_library_root_id=target_root_id,
    )


def _validate_placement_policy_snapshot(
    value: object,
    *,
    library_root_ids: set[int],
) -> tuple[dict[str, object] | None, int | None, str | None]:
    if not isinstance(value, dict) or set(value) != _PLACEMENT_POLICY_KEYS:
        return None, None, "policy_validation_failed"
    if isinstance(value.get("schema_version"), bool) or value.get("schema_version") != 1:
        return None, None, "policy_validation_failed"
    mode = value.get("mode")
    if not isinstance(mode, str) or mode not in _PLACEMENT_POLICY_MODES:
        return None, None, "policy_validation_failed"
    synchronize = value.get("synchronize")
    if not isinstance(synchronize, bool):
        return None, None, "policy_validation_failed"
    folder_template = value.get("folder_template")
    file_template = value.get("file_template")
    if (
        not isinstance(folder_template, str)
        or not isinstance(file_template, str)
        or len(folder_template.encode("utf-8")) > 1024
        or len(file_template.encode("utf-8")) > 1024
    ):
        return None, None, "policy_validation_failed"
    try:
        validate_story_arc_folder_template(folder_template)
        validate_story_arc_file_template(file_template)
    except ValueError:
        return None, None, "policy_validation_failed"

    symlink_style = value.get("symlink_style")
    if mode == "symlink":
        if symlink_style not in {"absolute", "relative"}:
            return None, None, "policy_validation_failed"
    elif symlink_style is not None:
        return None, None, "policy_validation_failed"

    target_root_id_raw = value.get("target_library_root_id")
    destination_root = value.get("destination_root")
    if mode == "logical":
        if target_root_id_raw is not None or destination_root is not None or synchronize:
            return None, None, "policy_validation_failed"
        target_root_id = None
    else:
        if (
            isinstance(target_root_id_raw, bool)
            or not isinstance(target_root_id_raw, int)
            or target_root_id_raw <= 0
            or not isinstance(destination_root, str)
            or not destination_root.strip()
            or len(destination_root) > 1000
            or _unsafe_path_text(destination_root)
            or not Path(destination_root).is_absolute()
        ):
            return None, None, "policy_validation_failed"
        if target_root_id_raw not in library_root_ids:
            return None, None, "policy_target_root_missing"
        target_root_id = target_root_id_raw
    return dict(value), target_root_id, None


def _apply_policy(
    arc: StoryArc,
    policy: _ValidatedPolicy,
    *,
    increment_revision: bool,
) -> bool:
    changed = any(
        (
            arc.monitored != policy.monitored,
            arc.search_missing != policy.search_missing,
            arc.include_upcoming != policy.include_upcoming,
            arc.sync_enabled != policy.sync_enabled,
            arc.target_library_root_id != policy.target_library_root_id,
            arc.policy_schema_version != 1,
            arc.policy_snapshot != policy.snapshot,
        )
    )
    arc.monitored = policy.monitored
    arc.search_missing = policy.search_missing
    arc.include_upcoming = policy.include_upcoming
    arc.sync_enabled = policy.sync_enabled
    arc.target_library_root_id = policy.target_library_root_id
    arc.policy_schema_version = 1
    arc.policy_snapshot = dict(policy.snapshot)
    if changed and increment_revision:
        arc.revision += 1
    return changed


async def _materialize_external_identity(
    session: AsyncSession,
    *,
    arc: StoryArc,
    staged_arc: ImportedStoryArc,
    identity: tuple[str, str, str],
    state: _MaterializationState,
) -> tuple[str, StoryArcExternalIdentity | None]:
    source, namespace, external_id = identity
    existing_arc_id = state.identities_by_key.get(identity)
    if existing_arc_id is not None:
        return ("reused", None) if existing_arc_id == arc.id else ("conflict", None)
    created = StoryArcExternalIdentity(
        story_arc_id=arc.id,
        source=source,
        namespace=namespace,
        external_id=external_id,
        evidence={
            "schema_version": 1,
            "import_job_id": staged_arc.import_job_id,
            "imported_story_arc_id": staged_arc.id,
        },
    )
    session.add(created)
    await session.flush()
    state.identities_by_key[identity] = int(arc.id)
    state.loaded_identity_keys.add(identity)
    return "created", created


def _identity_targets_another_arc(
    state: _MaterializationState,
    identities: Sequence[tuple[str, str, str]],
    expected_story_arc_id: int,
) -> bool:
    return any(
        arc_id != expected_story_arc_id for arc_id in _external_identity_arc_ids(state, identities)
    )


def _any_external_identity_exists(
    state: _MaterializationState,
    identities: Sequence[tuple[str, str, str]],
) -> bool:
    return bool(_external_identity_arc_ids(state, identities))


def _external_identity_arc_ids(
    state: _MaterializationState,
    identities: Sequence[tuple[str, str, str]],
) -> set[int]:
    return {
        identity_arc_id
        for key in identities
        if (identity_arc_id := state.identities_by_key.get(key)) is not None
    }


def _identity_key(identity: StoryArcExternalIdentity) -> tuple[str, str, str]:
    return (identity.source, identity.namespace, identity.external_id)


def _warn(
    warnings: list[StoryArcMaterializationWarning],
    code: str,
    staged_arc: ImportedStoryArc,
    entry: ImportedStoryArcEntry | None = None,
) -> None:
    warnings.append(
        StoryArcMaterializationWarning(
            code=code,
            imported_story_arc_id=int(staged_arc.id),
            imported_story_arc_entry_id=int(entry.id) if entry is not None else None,
        )
    )


def _story_arc_policy_state(arc: StoryArc) -> dict[str, object]:
    return {
        "monitored": bool(arc.monitored),
        "search_missing": bool(arc.search_missing),
        "include_upcoming": bool(arc.include_upcoming),
        "sync_enabled": bool(arc.sync_enabled),
        "target_library_root_id": arc.target_library_root_id,
        "policy_schema_version": arc.policy_schema_version,
        "policy_snapshot": dict(arc.policy_snapshot or {}),
        "revision": int(arc.revision),
    }


def _story_arc_created_state(arc: StoryArc) -> dict[str, object]:
    return {
        "name": arc.name,
        "normalized_name": arc.normalized_name,
        "description": arc.description,
        "comicvine_id": arc.comicvine_id,
        "publisher_id": arc.publisher_id,
        "comicvine_url": arc.comicvine_url,
        "source_kind": arc.source_kind.value,
        "lifecycle": arc.lifecycle.value,
        "source_import_job_id": arc.source_import_job_id,
        "diagnostics": dict(arc.diagnostics or {}),
        **_story_arc_policy_state(arc),
    }


def _membership_state(membership: IssueStoryArc) -> dict[str, object]:
    return {
        "story_arc_id": int(membership.story_arc_id),
        "issue_id": membership.issue_id,
        "sequence_number": int(membership.sequence_number),
        "source_ordinal": int(membership.source_ordinal),
        "legacy_sequence_was_null": bool(membership.legacy_sequence_was_null),
        "resolution_state": membership.resolution_state.value,
        "source_kind": membership.source_kind.value,
        "source_entry_id": membership.source_entry_id,
        "source_arc_id": membership.source_arc_id,
        "source_issue_id": membership.source_issue_id,
        "source_series_id": membership.source_series_id,
        "source_issue_number_text": membership.source_issue_number_text,
        "source_series_name": membership.source_series_name,
        "source_issue_title": membership.source_issue_title,
        "source_publisher": membership.source_publisher,
        "source_release_date_text": membership.source_release_date_text,
        "source_issue_date_text": membership.source_issue_date_text,
        "resolution_confidence": membership.resolution_confidence,
        "resolution_method": membership.resolution_method,
        "evidence": dict(membership.evidence or {}),
        "sync_eligible": bool(membership.sync_eligible),
        "last_materialization_result": dict(membership.last_materialization_result or {}),
    }


def _external_identity_state(identity: StoryArcExternalIdentity) -> dict[str, object]:
    return {
        "story_arc_id": int(identity.story_arc_id),
        "source": identity.source,
        "namespace": identity.namespace,
        "external_id": identity.external_id,
        "source_url": identity.source_url,
        "evidence": dict(identity.evidence or {}),
    }


def _persist_arc_materialization_diagnostics(
    staged_arc: ImportedStoryArc,
    *,
    story_arc_id: int | None,
    status: str,
    counts: Mapping[str, int],
    warning_codes: Sequence[str],
) -> None:
    diagnostics = dict(staged_arc.diagnostics or {})
    diagnostics["materialization"] = {
        "schema_version": 1,
        "status": status,
        "story_arc_id": story_arc_id,
        "counts": dict(counts),
        "warning_codes": list(dict.fromkeys(warning_codes)),
    }
    staged_arc.diagnostics = diagnostics


def _persist_entry_materialization_diagnostics(
    entry: ImportedStoryArcEntry,
    *,
    membership_id: int,
    warning_codes: Sequence[str],
) -> None:
    diagnostics = dict(entry.diagnostics or {})
    diagnostics["materialization"] = {
        "schema_version": 1,
        "status": "imported",
        "membership_id": membership_id,
        "warning_codes": list(dict.fromkeys(warning_codes)),
    }
    entry.diagnostics = diagnostics


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _count_snapshot(counts: _MutableCounts) -> dict[str, int]:
    return {
        "arcs_created": counts.arcs_created,
        "arcs_merged": counts.arcs_merged,
        "arcs_reused": counts.arcs_reused,
        "arcs_failed": counts.arcs_failed,
        "external_identities_created": counts.external_identities_created,
        "external_identities_reused": counts.external_identities_reused,
        "memberships_created": counts.memberships_created,
        "memberships_reused": counts.memberships_reused,
        "resolved_entries": counts.resolved_entries,
        "unresolved_entries": counts.unresolved_entries,
        "entries_skipped": counts.entries_skipped,
    }


def _increment_counts(
    total: _MutableCounts,
    arc: _MutableCounts,
    field: CountField,
) -> None:
    setattr(total, field, int(getattr(total, field)) + 1)
    setattr(arc, field, int(getattr(arc, field)) + 1)


def _warning_codes_by_arc(
    warnings: Sequence[StoryArcMaterializationWarning],
) -> dict[int, list[str]]:
    result: dict[int, list[str]] = {}
    for warning in warnings:
        result.setdefault(warning.imported_story_arc_id, []).append(warning.code)
    return result


async def _checkpoint(callback: CancellationCheck | None) -> None:
    if callback is not None:
        await callback()
