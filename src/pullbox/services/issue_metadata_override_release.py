"""Release selected issue overrides without publishing an identical XML pair."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from pullbox.core.exceptions import JobCancelledError
from pullbox.services.archive_metadata_binding import (
    ArchiveMetadataBindingError,
    lock_archive_metadata_binding,
    require_unowned_metadata_file,
    revalidate_archive_metadata_target,
)
from pullbox.services.import_file_edit_history import lock_import_file_history
from pullbox.services.library_mutation_coordination import (
    lock_file_mutation_admission,
    require_no_archive_publication,
)
from pullbox.services.metadata_baselines import MetadataBaselineWrite, save_metadata_baselines
from pullbox.utilities.import_guards import ensure_no_active_import_file_mutation
from pullbox.utilities.models import JobState, UtilityJob, UtilityJobItem

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.schemas.issue_metadata_editor import IssueMetadataEditorReview
    from pullbox.schemas.metadata_snapshot import FieldOrigin
    from pullbox.services.issue_file_metadata import PreparedFileMetadata

RELEASE_OUTCOME = "override_released"


def released_override(item: UtilityJobItem | None) -> bool:
    if item is None or item.operation != "file_metadata" or not item.after_state:
        return False
    try:
        outcome: object = json.loads(item.after_state)
        return outcome == {"outcome": RELEASE_OUTCOME}
    except ValueError:
        return False


async def release_issue_overrides(
    session: AsyncSession,
    plan: PreparedFileMetadata,
    editor: IssueMetadataEditorReview,
    operation: UUID,
    job_id: str | None,
) -> str:
    target = plan.target
    state = target.binding.metadata.issues[0]
    baseline = state.baseline
    if not baseline or not editor.edits or any(e.source != "reset" for e in editor.edits.values()):
        raise ArchiveMetadataBindingError("approval_changed")
    replacements: dict[str, FieldOrigin] = {
        o.field: o for o in plan.issue.origins if o.field in editor.edits
    }
    overridden = {o.field for o in baseline.origins if o.user_override}
    if not editor.edits.keys() <= overridden or replacements.keys() != editor.edits.keys():
        raise ArchiveMetadataBindingError("approval_changed")
    await lock_file_mutation_admission(session)
    if job_id is not None:
        job = await session.get(UtilityJob, job_id, with_for_update=True)
        item = await session.get(UtilityJobItem, operation.hex, with_for_update=True)
        if not job or job.state != JobState.RUNNING or not item or item.job_id != job_id:
            raise JobCancelledError("Metadata edit stopped before saving")
        # The worker can restart between this commit and its normal completion accounting.
        item.after_state = json.dumps({"outcome": RELEASE_OUTCOME})
    await ensure_no_active_import_file_mutation(session)
    await require_no_archive_publication(session, target.path, include_descendants=False)
    await lock_import_file_history(session, target.binding.library_file_id)
    await lock_archive_metadata_binding(session, target.binding)
    await require_unowned_metadata_file(session, target.binding.library_file_id)
    await revalidate_archive_metadata_target(session, target)
    origins = tuple(replacements.get(o.field, o) for o in baseline.origins)
    await save_metadata_baselines(
        session,
        [
            MetadataBaselineWrite(
                state.local_id,
                baseline.model_copy(update={"origins": origins}),
                state.baseline_revision,
            )
        ],
    )
    await asyncio.to_thread(target.check_unchanged)
    return RELEASE_OUTCOME
