"""Preview and remove unused roots without touching physical library files."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from pullbox.core.config_resolver import get_application_secret
from pullbox.core.exceptions import ValidationError
from pullbox.models.config import SystemConfig
from pullbox.models.import_job import ImportJob
from pullbox.models.library import LibraryFile, LibraryRoot, LibraryRootPolicy
from pullbox.models.series import Series
from pullbox.models.story_arc import StoryArc, StoryArcPlacement
from pullbox.services.import_workflow_state import ACTIVE_IMPORT_JOB_STATUSES
from pullbox.services.library_root_policy_service import LibraryRootNotFoundError
from pullbox.utilities.models import JobState, UtilityJob

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_ACTIVE_UTILITIES = tuple(
    state
    for state in JobState
    if state
    not in {
        JobState.COMPLETED,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.ROLLED_BACK,
    }
)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_application_secret(), salt="library-root-remove-v1")


def _dependencies(root_id: int, path: str) -> list[tuple[str, Any, str]]:
    return [
        (
            "library_files",
            LibraryFile.library_root_id == root_id,
            "registered files still use this root. Relocate or remove those library entries "
            "first; disabling does not detach them.",
        ),
        (
            "series",
            Series.library_root_id == root_id,
            "series still use this root. Relocate or remove those series first.",
        ),
        (
            "preferred_series",
            Series.preferred_library_root_id == root_id,
            "series use this as their preferred destination. "
            "Choose another destination for those series first.",
        ),
        (
            "story_arcs",
            StoryArc.target_library_root_id == root_id,
            "Story Arcs target this root. Change their placement destinations first.",
        ),
        (
            "story_arc_placements",
            StoryArcPlacement.library_root_id == root_id,
            "Story Arc placements still use this root. Resolve those placements first.",
        ),
        (
            "destination_settings",
            (
                (
                    (SystemConfig.key == "story_arc_files_library_root_id")
                    & (SystemConfig.value == str(root_id))
                )
                | ((SystemConfig.key == "comics_directory") & (SystemConfig.value == path))
            ),
            "destination settings still use this root. "
            "Choose another default library or Story Arc destination first.",
        ),
        (
            "active_imports",
            ImportJob.status.in_(tuple(ACTIVE_IMPORT_JOB_STATUSES)),
            "imports are active or paused. Finish or cancel them before changing library roots.",
        ),
        (
            "active_utilities",
            UtilityJob.state.in_(_ACTIVE_UTILITIES),
            "file utilities are active or paused. Finish or cancel them before removing a root.",
        ),
        (
            "pending_import_followups",
            (ImportJob.target_library_root_id == root_id)
            & (
                ImportJob.story_arc_placement_followup_pending.is_(True)
                | ImportJob.story_arc_rollback_waiting_work_id.is_not(None)
            ),
            "import follow-ups still depend on this root. "
            "Complete their placement or rollback first.",
        ),
    ]


async def preview_library_root_removal(
    session: AsyncSession,
    library_root_id: int,
    *,
    actor_id: int,
) -> dict[str, Any]:
    """Return exact blockers without probing or changing the filesystem."""
    root = await session.get(LibraryRoot, library_root_id, populate_existing=True)
    if root is None:
        raise LibraryRootNotFoundError()
    blockers = []
    if root.enabled:
        blockers.append("Disable this library root before removing it.")
    if root.is_default_managed_destination:
        blockers.append("Select another default managed destination before removing this root.")
    counts = {}
    for key, condition, explanation in _dependencies(root.id, root.path):
        count = int(await session.scalar(select(func.count()).where(condition)) or 0)
        counts[key] = count
        if count:
            blockers.append(f"{count} {explanation}")
    history_count = int(
        await session.scalar(
            select(func.count(ImportJob.id)).where(
                ImportJob.target_library_root_id == root.id,
            )
        )
        or 0
    )
    policy = await session.scalar(
        select(LibraryRootPolicy).where(LibraryRootPolicy.library_root_id == root.id)
    )
    snapshot = {"id": root.id, "name": root.name, "path": root.path}
    signed_state = {
        "root": snapshot,
        "actor_id": actor_id,
        "updated_at": str(root.updated_at),
        "history_count": history_count,
        "policy_updated_at": str(policy.updated_at) if policy else None,
    }
    return {
        **snapshot,
        "can_remove": not blockers,
        "blocking_reasons": blockers,
        "dependencies": counts,
        "history_count": history_count,
        "has_naming_policy": policy is not None,
        "preview_token": _serializer().dumps(signed_state) if not blockers else None,
    }


async def remove_library_root(
    session: AsyncSession,
    library_root_id: int,
    *,
    actor_id: int,
    preview_token: str,
) -> None:
    """Recheck under a short write lock; retain detached historical identity."""
    try:
        confirmed = _serializer().loads(preview_token, max_age=900)
    except BadSignature as exc:
        raise ValidationError(
            "Removal preview expired or is invalid. Review the root again."
        ) from exc
    # SQLite needs a write reservation, not SELECT FOR UPDATE, before the recheck.
    await session.execute(
        update(LibraryRoot)
        .where(LibraryRoot.id == library_root_id)
        .values(
            updated_at=LibraryRoot.updated_at,
        )
    )
    preview = await preview_library_root_removal(session, library_root_id, actor_id=actor_id)
    if preview["blocking_reasons"]:
        raise ValidationError(" ".join(preview["blocking_reasons"]))
    current = _serializer().loads(preview["preview_token"], max_age=900)
    if confirmed != current:
        raise ValidationError("Removal preview changed. Review the root again before confirming.")
    snapshot = {key: preview[key] for key in ("id", "name", "path")}
    await session.execute(
        update(ImportJob)
        .where(
            ImportJob.target_library_root_id == library_root_id,
            ImportJob.status.not_in(tuple(ACTIVE_IMPORT_JOB_STATUSES)),
            ImportJob.story_arc_placement_followup_pending.is_(False),
            ImportJob.story_arc_rollback_waiting_work_id.is_(None),
        )
        .values(
            removed_library_root_snapshot=snapshot,
            target_library_root_id=None,
        )
    )
    guards = [
        ~select(1).where(condition).exists()
        for _, condition, _ in _dependencies(library_root_id, preview["path"])
    ]
    await session.execute(
        delete(LibraryRootPolicy).where(LibraryRootPolicy.library_root_id == library_root_id)
    )
    try:
        deleted = await session.scalar(
            delete(LibraryRoot)
            .where(
                LibraryRoot.id == library_root_id,
                LibraryRoot.enabled.is_(False),
                LibraryRoot.is_default_managed_destination.is_(False),
                *guards,
            )
            .returning(LibraryRoot.id)
        )
    except IntegrityError as exc:
        raise ValidationError(
            "This root gained a dependency. Review it again before removing it."
        ) from exc
    if deleted is None:
        raise ValidationError("This root gained a dependency. Review it again before removing it.")
