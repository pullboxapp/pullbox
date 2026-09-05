"""Root removal is configuration-only and must never orphan library data."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.exceptions import ValidationError
from pullbox.models.config import SystemConfig
from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot
from pullbox.models.series import Series
from pullbox.models.story_arc import StoryArc
from pullbox.services import library_root_management as roots
from pullbox.services.import_retry_helpers import build_retry_import_request
from pullbox.utilities.models import JobState, UtilityJob


async def disabled_root(session: AsyncSession, path: Path) -> LibraryRoot:
    root = LibraryRoot(name="Old library", path=str(path), enabled=False)
    session.add(root)
    await session.flush()
    return root


@pytest.mark.asyncio
async def test_removal_preserves_files_and_cancelled_import_history(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root = await disabled_root(db_session, tmp_path)
    untouched = tmp_path / "untracked.cbz"
    untouched.write_bytes(b"user-owned file")
    job = ImportJob(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.CANCELLED,
        target_library_root_id=root.id,
    )
    db_session.add(job)
    await db_session.flush()
    preview = await roots.preview_library_root_removal(db_session, root.id, actor_id=7)
    assert preview["can_remove"] is True
    assert preview["history_count"] == 1
    assert await db_session.get(LibraryRoot, root.id) is root
    await roots.remove_library_root(
        db_session, root.id, actor_id=7, preview_token=preview["preview_token"]
    )
    assert await db_session.scalar(select(LibraryRoot.id).where(LibraryRoot.id == root.id)) is None
    await db_session.refresh(job)
    assert job.target_library_root_id is None
    assert job.removed_library_root_snapshot == {
        "id": root.id,
        "name": root.name,
        "path": str(tmp_path),
    }
    assert untouched.read_bytes() == b"user-owned file"
    with pytest.raises(ValidationError, match=r"removed.*new import"):
        build_retry_import_request(job)


@pytest.mark.asyncio
async def test_offline_unused_root_is_removable(db_session: AsyncSession, tmp_path: Path) -> None:
    root = await disabled_root(db_session, tmp_path / "offline")
    preview = await roots.preview_library_root_removal(db_session, root.id, actor_id=7)
    assert preview["can_remove"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dependency",
    ["enabled", "default", "series", "preferred", "active", "config", "arc", "utility", "followup"],
)
async def test_removal_explains_dependencies(
    db_session: AsyncSession,
    tmp_path: Path,
    dependency: str,
) -> None:
    root = await disabled_root(db_session, tmp_path)
    if dependency == "enabled":
        root.enabled = True
    elif dependency == "default":
        root.is_default_managed_destination = True
    elif dependency in {"series", "preferred"}:
        db_session.add(
            Series(
                title="Owned",
                sort_title="owned",
                **{
                    "library_root_id"
                    if dependency == "series"
                    else "preferred_library_root_id": root.id,
                },
            )
        )
    elif dependency == "active":
        db_session.add(
            ImportJob(
                source_path=str(tmp_path),
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.PAUSED,
                target_library_root_id=root.id,
            )
        )
    elif dependency == "arc":
        db_session.add(StoryArc(name="Arc", target_library_root_id=root.id))
    elif dependency == "utility":
        db_session.add(
            UtilityJob(
                id="paused", job_type="mass_rename", display_name="Rename", state=JobState.PAUSED
            )
        )
    elif dependency == "followup":
        db_session.add(
            ImportJob(
                source_path=str(tmp_path),
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.COMPLETED,
                target_library_root_id=root.id,
                story_arc_placement_followup_pending=True,
            )
        )
    else:
        db_session.add(
            SystemConfig(
                key="story_arc_files_library_root_id", value=str(root.id), value_type="string"
            )
        )
    await db_session.flush()
    preview = await roots.preview_library_root_removal(db_session, root.id, actor_id=7)
    assert preview["can_remove"] is False
    assert preview["blocking_reasons"]
    assert preview["preview_token"] is None


@pytest.mark.asyncio
async def test_confirmation_rechecks_dependencies(db_session: AsyncSession, tmp_path: Path) -> None:
    root = await disabled_root(db_session, tmp_path)
    preview = await roots.preview_library_root_removal(db_session, root.id, actor_id=7)
    db_session.add(Series(title="New arrival", sort_title="new arrival", library_root_id=root.id))
    await db_session.flush()
    with pytest.raises(ValidationError, match="series"):
        await roots.remove_library_root(
            db_session, root.id, actor_id=7, preview_token=preview["preview_token"]
        )
    assert await db_session.get(LibraryRoot, root.id) is not None


@pytest.mark.asyncio
async def test_preview_cannot_be_reused_by_another_operator(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    root = await disabled_root(db_session, tmp_path)
    preview = await roots.preview_library_root_removal(db_session, root.id, actor_id=7)
    with pytest.raises(ValidationError, match="preview"):
        await roots.remove_library_root(
            db_session, root.id, actor_id=8, preview_token=preview["preview_token"]
        )


@pytest.mark.asyncio
async def test_database_and_orm_reject_root_deletion_with_files(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    await db_session.execute(text("PRAGMA foreign_keys=ON"))
    root = await disabled_root(db_session, tmp_path)
    file = LibraryFile(
        file_path=str(tmp_path / "issue.cbz"),
        file_name="issue.cbz",
        file_size=1,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime.now(UTC),
        library_root_id=root.id,
    )
    db_session.add(file)
    await db_session.commit()
    await db_session.refresh(root, ["files"])
    with pytest.raises(IntegrityError):
        await db_session.delete(root)
        await db_session.flush()
    await db_session.rollback()
    assert await db_session.scalar(select(LibraryFile.id)) is not None


@pytest.mark.asyncio
async def test_removal_rejects_changed_or_invalid_preview(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    root = await disabled_root(db_session, tmp_path)
    preview = await roots.preview_library_root_removal(db_session, root.id, actor_id=7)
    root.name = "Changed since preview"
    await db_session.flush()
    for token in (preview["preview_token"], "invalid"):
        with pytest.raises(ValidationError, match="preview"):
            await roots.remove_library_root(db_session, root.id, actor_id=7, preview_token=token)


@pytest.mark.asyncio
async def test_removed_root_blocks_failed_retry_before_any_reconciliation(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    from unittest.mock import AsyncMock

    from pullbox.services.import_orphans import retry_failed_series

    job = ImportJob(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.COMPLETED,
        removed_library_root_snapshot={"id": 1, "path": "/removed", "name": "Old"},
    )
    db_session.add(job)
    await db_session.flush()
    logger = AsyncMock()
    with pytest.raises(ValidationError, match="library root was removed"):
        await retry_failed_series(db_session, job.id, log_event=logger)
    assert job.status == ImportJobStatus.COMPLETED
    logger.assert_not_awaited()
