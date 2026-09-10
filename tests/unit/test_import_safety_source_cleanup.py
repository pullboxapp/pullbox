"""Guardrails for explicitly removing one reviewed source artifact."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from itsdangerous import SignatureExpired
from sqlalchemy import select

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.models.config import SystemConfig
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.library import LibraryRoot
from pullbox.models.story_arc import (
    ImportedStoryArcStatus,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.models.user import User
from pullbox.services.import_safety_source_cleanup import (
    _load_token,
    _serializer,
    move_one_page_source_to_trash,
    preview_one_page_source_cleanup,
)

if TYPE_CHECKING:
    from pathlib import Path


async def _seed_cleanup_case(
    db_session,
    tmp_path: Path,
    *,
    allow_managed_writes: bool,
    source_type: ImportSourceType = ImportSourceType.MYLAR3,
    series_status: ImportSeriesStatus = ImportSeriesStatus.MATCHED,
    job_status: ImportJobStatus = ImportJobStatus.REVIEW,
) -> tuple[int, int, Path, Path]:
    source_root = tmp_path / "mylar"
    source_root.mkdir()
    source = source_root / "possible-cover.cbz"
    source.write_bytes(b"one-page source")
    trash = tmp_path / "trash"
    db_session.add_all(
        [
            User(id=42, username="operator", password_hash="unused"),
            LibraryRoot(
                name="Mylar source",
                path=str(source_root),
                enabled=True,
                allow_referenced_registrations=True,
                allow_managed_writes=allow_managed_writes,
                is_default_managed_destination=False,
            ),
            SystemConfig(key="utility_trash_folder", value=str(trash), value_type="string"),
        ]
    )
    job = ImportJob(
        source_path=str(
            source_root if source_type is ImportSourceType.FILESYSTEM else tmp_path / "mylar.db"
        ),
        source_type=source_type,
        status=job_status,
    )
    db_session.add(job)
    await db_session.flush()
    series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Possible cover",
        status=series_status,
        selected_for_import=True,
    )
    db_session.add(series)
    await db_session.flush()
    imported_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=series.id,
        file_path=str(source),
        file_name=source.name,
        file_size=source.stat().st_size,
        file_format="cbz",
        status=ImportedFileStatus.SAFETY_BLOCKED,
        include_in_import=True,
        source_signature=build_file_identity_signature(source),
        diagnostics={
            "safety_block": {
                "category": "single_page_comic",
                "code": "single_page_comic",
                "overrideable": True,
            }
        },
    )
    db_session.add(imported_file)
    await db_session.commit()
    return job.id, imported_file.id, source, trash


@pytest.mark.asyncio
async def test_completed_import_can_preview_source_cleanup_as_follow_up(
    db_session,
    tmp_path: Path,
) -> None:
    job_id, file_id, source, _trash = await _seed_cleanup_case(
        db_session,
        tmp_path,
        allow_managed_writes=True,
        source_type=ImportSourceType.FILESYSTEM,
        job_status=ImportJobStatus.COMPLETED,
    )

    preview = await preview_one_page_source_cleanup(db_session, job_id, file_id, actor_id=42)

    assert preview.can_move_to_trash is True
    assert preview.preview_token is not None
    assert source.exists()


@pytest.mark.asyncio
async def test_reference_only_mylar_root_cannot_remove_source(db_session, tmp_path: Path) -> None:
    job_id, file_id, source, _trash = await _seed_cleanup_case(
        db_session,
        tmp_path,
        allow_managed_writes=False,
    )

    preview = await preview_one_page_source_cleanup(db_session, job_id, file_id, actor_id=42)

    assert preview.can_move_to_trash is False
    assert preview.preview_token is None
    assert "reference-only" in preview.unavailable_reason
    assert source.exists()


@pytest.mark.asyncio
async def test_explicit_one_page_cleanup_moves_only_reviewed_source_to_trash(
    db_session,
    tmp_path: Path,
) -> None:
    job_id, file_id, source, trash = await _seed_cleanup_case(
        db_session,
        tmp_path,
        allow_managed_writes=True,
    )
    preview = await preview_one_page_source_cleanup(db_session, job_id, file_id, actor_id=42)
    assert preview.can_move_to_trash is True
    assert preview.preview_token is not None
    arc = ImportedStoryArc(
        import_job_id=job_id,
        source_kind=StoryArcSourceKind.FOLDER,
        source_key=f"folder:cleanup:{job_id}",
        source_ordinal=1,
        name="Cleanup arc",
        status=ImportedStoryArcStatus.NEEDS_REVIEW,
        diagnostics={"safety_incomplete": True},
    )
    db_session.add(arc)
    await db_session.flush()
    entry = ImportedStoryArcEntry(
        imported_story_arc_id=arc.id,
        import_file_id=file_id,
        source_ordinal=1,
        source_kind=StoryArcSourceKind.FOLDER,
        resolution_state=StoryArcResolutionState.AMBIGUOUS,
        diagnostics={
            "safety_code": "single_page_comic",
            "review_reason": "source_file_safety_blocked",
        },
    )
    db_session.add(entry)
    await db_session.commit()

    result = await move_one_page_source_to_trash(
        db_session,
        job_id,
        file_id,
        actor_id=42,
        preview_token=preview.preview_token,
    )

    assert source.exists() is False
    assert result.trash_path.is_file()
    assert result.trash_path.is_relative_to(trash)
    imported_file = await db_session.get(ImportedFile, file_id)
    assert imported_file is not None
    assert imported_file.status is ImportedFileStatus.SKIPPED
    assert imported_file.include_in_import is False
    assert entry.resolution_state is StoryArcResolutionState.SKIPPED
    assert "safety_code" not in entry.diagnostics
    assert arc.diagnostics["safety_incomplete"] is False


@pytest.mark.asyncio
async def test_folder_import_can_move_reviewed_one_page_source_to_trash(
    db_session,
    tmp_path: Path,
) -> None:
    job_id, file_id, source, trash = await _seed_cleanup_case(
        db_session,
        tmp_path,
        allow_managed_writes=False,
        source_type=ImportSourceType.FILESYSTEM,
    )
    preview = await preview_one_page_source_cleanup(db_session, job_id, file_id, actor_id=42)
    assert preview.can_move_to_trash is True
    assert preview.preview_token is not None

    result = await move_one_page_source_to_trash(
        db_session,
        job_id,
        file_id,
        actor_id=42,
        preview_token=preview.preview_token,
    )

    assert source.exists() is False
    assert result.trash_path.is_relative_to(trash)


@pytest.mark.asyncio
async def test_cleanup_closes_single_file_series_that_no_longer_needs_matching(
    db_session,
    tmp_path: Path,
) -> None:
    job_id, file_id, _source, _trash = await _seed_cleanup_case(
        db_session,
        tmp_path,
        allow_managed_writes=True,
        source_type=ImportSourceType.FILESYSTEM,
        series_status=ImportSeriesStatus.NO_MATCH,
    )
    preview = await preview_one_page_source_cleanup(db_session, job_id, file_id, actor_id=42)
    assert preview.preview_token is not None

    await move_one_page_source_to_trash(
        db_session,
        job_id,
        file_id,
        actor_id=42,
        preview_token=preview.preview_token,
    )

    imported_file = await db_session.get(ImportedFile, file_id)
    assert imported_file is not None
    imported_series = await db_session.get(ImportedSeries, imported_file.import_series_id)
    assert imported_series is not None
    assert imported_series.status is ImportSeriesStatus.SKIPPED


@pytest.mark.asyncio
async def test_cleanup_keeps_series_in_review_when_an_unresolved_file_remains(
    db_session,
    tmp_path: Path,
) -> None:
    job_id, file_id, source, _trash = await _seed_cleanup_case(
        db_session,
        tmp_path,
        allow_managed_writes=True,
        source_type=ImportSourceType.FILESYSTEM,
        series_status=ImportSeriesStatus.NO_MATCH,
    )
    imported_file = await db_session.get(ImportedFile, file_id)
    assert imported_file is not None
    unresolved_source = source.with_name("Daredevil 008.cbz")
    unresolved_source.write_bytes(b"comic pages")
    db_session.add(
        ImportedFile(
            import_job_id=job_id,
            import_series_id=imported_file.import_series_id,
            file_path=str(unresolved_source),
            file_name=unresolved_source.name,
            file_size=unresolved_source.stat().st_size,
            file_format="cbz",
            status=ImportedFileStatus.NO_MATCH,
            include_in_import=False,
        )
    )
    await db_session.commit()
    preview = await preview_one_page_source_cleanup(db_session, job_id, file_id, actor_id=42)
    assert preview.preview_token is not None

    await move_one_page_source_to_trash(
        db_session,
        job_id,
        file_id,
        actor_id=42,
        preview_token=preview.preview_token,
    )

    imported_series = await db_session.get(ImportedSeries, imported_file.import_series_id)
    assert imported_series is not None
    assert imported_series.status is ImportSeriesStatus.NO_MATCH


@pytest.mark.asyncio
async def test_source_is_restored_when_cleanup_database_commit_fails(
    db_session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, file_id, source, trash = await _seed_cleanup_case(
        db_session,
        tmp_path,
        allow_managed_writes=True,
    )
    preview = await preview_one_page_source_cleanup(db_session, job_id, file_id, actor_id=42)
    assert preview.preview_token is not None

    async def fail_commit() -> None:
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(db_session, "commit", fail_commit)

    with pytest.raises(RuntimeError, match="simulated database failure"):
        await move_one_page_source_to_trash(
            db_session,
            job_id,
            file_id,
            actor_id=42,
            preview_token=preview.preview_token,
        )

    assert source.read_bytes() == b"one-page source"
    assert list(trash.rglob("possible-cover.cbz")) == []


@pytest.mark.asyncio
async def test_cleanup_preview_rejects_unknown_and_inactive_jobs(db_session) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(NotFoundError):
        await preview_one_page_source_cleanup(db_session, 999, 1, actor_id=42)

    job = ImportJob(
        source_path="/tmp/import",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.SCANNING,
    )
    db_session.add(job)
    await db_session.commit()

    with pytest.raises(ValidationError, match="during review"):
        await preview_one_page_source_cleanup(db_session, job.id, 1, actor_id=42)


@pytest.mark.asyncio
async def test_cleanup_preview_rejects_missing_or_ineligible_file(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    job_id, file_id, _source, _trash = await _seed_cleanup_case(
        db_session,
        tmp_path,
        allow_managed_writes=True,
    )

    with pytest.raises(NotFoundError):
        await preview_one_page_source_cleanup(db_session, job_id, file_id + 99, actor_id=42)

    imported_file = await db_session.get(ImportedFile, file_id)
    assert imported_file is not None
    imported_file.status = ImportedFileStatus.MATCHED
    await db_session.commit()
    with pytest.raises(ValidationError, match="one-page archive"):
        await preview_one_page_source_cleanup(db_session, job_id, file_id, actor_id=42)


@pytest.mark.asyncio
async def test_cleanup_preview_rejects_symlinked_or_missing_source(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    job_id, file_id, source, _trash = await _seed_cleanup_case(
        db_session,
        tmp_path,
        allow_managed_writes=True,
    )
    target = source.with_name("target.cbz")
    target.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(target)

    with pytest.raises(ValidationError, match="Symlinked"):
        await preview_one_page_source_cleanup(db_session, job_id, file_id, actor_id=42)

    source.unlink()
    with pytest.raises(ValidationError, match="changed or is unavailable"):
        await preview_one_page_source_cleanup(db_session, job_id, file_id, actor_id=42)


@pytest.mark.asyncio
async def test_cleanup_preview_reports_unwritable_folder_source(
    db_session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    job_id, file_id, _source, _trash = await _seed_cleanup_case(
        db_session,
        tmp_path,
        allow_managed_writes=False,
        source_type=ImportSourceType.FILESYSTEM,
    )
    monkeypatch.setattr("pullbox.services.import_safety_source_cleanup.os.access", lambda *_: False)

    preview = await preview_one_page_source_cleanup(db_session, job_id, file_id, actor_id=42)

    assert preview.can_move_to_trash is False
    assert "permission" in preview.unavailable_reason


def test_source_cleanup_tokens_reject_invalid_expired_and_non_mapping_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="invalid"):
        _load_token("not-a-token")

    non_mapping = str(_serializer().dumps(["unexpected"]))
    with pytest.raises(ValidationError, match="invalid"):
        _load_token(non_mapping)

    class ExpiredSerializer:
        def loads(self, *_args: object, **_kwargs: object) -> object:
            raise SignatureExpired("old")

    monkeypatch.setattr(
        "pullbox.services.import_safety_source_cleanup._serializer",
        ExpiredSerializer,
    )
    with pytest.raises(ValidationError, match="expired"):
        _load_token("expired")


@pytest.mark.asyncio
async def test_cleanup_move_rejects_token_for_another_actor_or_missing_signature(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    job_id, file_id, _source, _trash = await _seed_cleanup_case(
        db_session,
        tmp_path,
        allow_managed_writes=True,
    )
    preview = await preview_one_page_source_cleanup(db_session, job_id, file_id, actor_id=42)
    assert preview.preview_token is not None

    with pytest.raises(ValidationError, match="does not match"):
        await move_one_page_source_to_trash(
            db_session,
            job_id,
            file_id,
            actor_id=7,
            preview_token=preview.preview_token,
        )

    missing_signature = str(
        _serializer().dumps({"job_id": job_id, "file_id": file_id, "actor_id": 42})
    )
    with pytest.raises(ValidationError, match="invalid"):
        await move_one_page_source_to_trash(
            db_session,
            job_id,
            file_id,
            actor_id=42,
            preview_token=missing_signature,
        )


@pytest.mark.asyncio
async def test_cleanup_move_rechecks_availability_after_preview(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    job_id, file_id, _source, _trash = await _seed_cleanup_case(
        db_session,
        tmp_path,
        allow_managed_writes=True,
    )
    preview = await preview_one_page_source_cleanup(db_session, job_id, file_id, actor_id=42)
    assert preview.preview_token is not None
    root = (await db_session.scalars(select(LibraryRoot))).one()
    root.allow_managed_writes = False
    await db_session.commit()

    with pytest.raises(ValidationError, match="reference-only"):
        await move_one_page_source_to_trash(
            db_session,
            job_id,
            file_id,
            actor_id=42,
            preview_token=preview.preview_token,
        )
