"""Reader-state continuity across library file lifecycle operations."""

from __future__ import annotations

import asyncio
import io
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from PIL import Image
from sqlalchemy import select, text

from pullbox.api.v1.reader import _manifest_initial_page
from pullbox.core.file_ops import register_library_file
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.reader import IssueReaderState
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.models.user import User
from pullbox.services.issue_file_service import delete_issue_library_file
from pullbox.services.library_convert_service import convert_library_file
from pullbox.services.library_rename_service import rename_library_entry
from pullbox.services.reader_content_service import (
    ReaderContentService,
    load_reader_source_record,
    resolve_reader_source,
)
from pullbox.services.reader_state_service import load_reader_state
from pullbox.services.reading_query_service import (
    list_continue_reading,
    list_want_to_read,
    load_reader_issue_access,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_STATE_FIELDS = (
    "id",
    "user_id",
    "issue_id",
    "last_page_index",
    "content_revision",
    "page_count",
    "progress_updated_at",
    "last_opened_at",
    "completed_at",
    "completion_updated_at",
    "want_to_read",
    "want_to_read_updated_at",
    "state_version",
    "created_at",
    "updated_at",
)


def _write_cbz(path: Path, *, page_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for page_index in range(page_count):
            page = io.BytesIO()
            with Image.new("RGB", (2, 2), color=(page_index, 0, 0)) as image:
                image.save(page, format="PNG")
            archive.writestr(f"{page_index + 1:03d}.png", page.getvalue())


def _state_values(state: IssueReaderState) -> dict[str, Any]:
    return {field: getattr(state, field) for field in _STATE_FIELDS}


async def _load_state_row(session: AsyncSession, state_id: int) -> IssueReaderState:
    state = await session.get(IssueReaderState, state_id)
    assert state is not None
    return state


async def _seed_reader_file(
    session: AsyncSession,
    *,
    root_path: Path,
    file_format: FileFormat = FileFormat.CBZ,
    page_count: int = 3,
    completed: bool = False,
) -> tuple[User, Issue, LibraryFile, IssueReaderState]:
    root_path.mkdir(parents=True, exist_ok=True)
    series_path = root_path / "Reader Series (2026)"
    series_path.mkdir(parents=True, exist_ok=True)
    file_path = series_path / f"Reader Series 001.{file_format.value}"
    _write_cbz(file_path, page_count=page_count)

    user = User(username="continuity-reader", password_hash="test-hash")
    root = LibraryRoot(name="Continuity", path=str(root_path), enabled=True)
    series = Series(
        title="Reader Series",
        sort_title="reader series",
        year_start=2026,
        path=str(series_path),
        status=SeriesStatus.CONTINUING,
        series_type=SeriesType.STANDARD,
        monitored=True,
        library_root=root,
    )
    issue = Issue(
        series=series,
        issue_number=1,
        title="Continuity",
        status=IssueStatus.OWNED,
    )
    stat = file_path.stat()
    library_file = LibraryFile(
        file_path=str(file_path),
        file_name=file_path.name,
        file_size=stat.st_size,
        file_format=file_format,
        file_modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        match_confidence=MatchConfidence.HIGH,
        issue=issue,
        library_root=root,
    )
    session.add_all((user, library_file))
    await session.flush()

    progress_at = datetime(2026, 8, 24, 10, 11, tzinfo=UTC)
    completion_at = datetime(2026, 8, 24, 10, 12, tzinfo=UTC)
    queued_at = datetime(2026, 8, 24, 10, 13, tzinfo=UTC)
    state = IssueReaderState(
        user_id=user.id,
        issue_id=issue.id,
        last_page_index=1,
        content_revision="original-revision",
        page_count=page_count,
        progress_updated_at=progress_at,
        last_opened_at=progress_at,
        completed_at=completion_at if completed else None,
        completion_updated_at=completion_at,
        want_to_read=not completed,
        want_to_read_updated_at=queued_at,
        state_version=7,
    )
    session.add(state)
    await session.flush()
    return user, issue, library_file, state


@pytest.mark.asyncio
async def test_file_rename_and_folder_relocation_preserve_exact_reader_state(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    _user, _issue, library_file, state = await _seed_reader_file(
        db_session,
        root_path=tmp_path / "library",
        completed=True,
    )
    await db_session.commit()
    before = _state_values(state)

    source_file = Path(library_file.file_path)
    renamed_file = source_file.with_name("Reader Series 001 Deluxe.cbz")
    await rename_library_entry(
        db_session,
        source=source_file,
        target=renamed_file,
        kind="file",
    )
    source_folder = renamed_file.parent
    relocated_folder = source_folder.with_name("Reader Series Relocated")
    await rename_library_entry(
        db_session,
        source=source_folder,
        target=relocated_folder,
        kind="folder",
    )

    preserved = await _load_state_row(db_session, state.id)
    await db_session.refresh(library_file)
    assert _state_values(preserved) == before
    assert library_file.file_path == str(relocated_folder / renamed_file.name)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_format",
    [FileFormat.CBR, FileFormat.CB7, FileFormat.CBT, FileFormat.PDF],
)
async def test_conversion_to_cbz_preserves_reader_state_and_issue_identity(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_format: FileFormat,
) -> None:
    _user, issue, library_file, state = await _seed_reader_file(
        db_session,
        root_path=tmp_path / "library",
        file_format=file_format,
    )
    await db_session.commit()
    before = _state_values(state)
    source = Path(library_file.file_path)

    async def preserving_converter(source_path: Path, _target_format: str) -> Path:
        target = source_path.with_suffix(".cbz")
        await asyncio.to_thread(shutil.copy2, source_path, target)
        return target

    monkeypatch.setattr(
        "pullbox.services.library_convert_service.convert_file",
        preserving_converter,
    )

    result = await convert_library_file(
        db_session,
        source=source,
        trash_dir=tmp_path / "trash",
        trash_relative_path=source.name,
    )

    preserved = await _load_state_row(db_session, state.id)
    await db_session.refresh(library_file)
    assert _state_values(preserved) == before
    assert library_file.issue_id == issue.id
    assert library_file.file_format is FileFormat.CBZ
    assert library_file.file_path == result.target_path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replacement_page_count", "expected_initial_page"),
    [(3, 1), (4, 0)],
)
async def test_replacement_preserves_state_and_reconciles_resume_by_page_count(
    db_session: AsyncSession,
    tmp_path: Path,
    replacement_page_count: int,
    expected_initial_page: int,
) -> None:
    user, issue, library_file, state = await _seed_reader_file(
        db_session,
        root_path=tmp_path / "library",
        page_count=3,
    )
    await db_session.commit()
    before = _state_values(state)
    user_id = user.id
    issue_id = issue.id
    state_id = state.id
    root_id = library_file.library_root_id
    replacement = tmp_path / "incoming" / "replacement.cbz"
    _write_cbz(replacement, page_count=replacement_page_count)

    registered = await register_library_file(
        db_session,
        replacement,
        issue,
        MatchConfidence.MANUAL,
        move_to_library=True,
        rename=False,
        library_root_id=root_id,
        transfer_method="copy",
        normalize_to_cbz=False,
        update_embedded_comicinfo_from_match=False,
        replace_existing_library_file=True,
    )
    await db_session.commit()
    db_session.expire_all()

    preserved = await _load_state_row(db_session, state_id)
    source_record = await load_reader_source_record(db_session, issue_id)
    resolved = resolve_reader_source(source_record)
    manifest = await ReaderContentService(cache_dir=tmp_path / "cache").get_manifest(resolved)
    snapshot = await load_reader_state(db_session, user_id=user_id, issue_id=issue_id)
    linked_files = (
        (await db_session.execute(select(LibraryFile).where(LibraryFile.issue_id == issue_id)))
        .scalars()
        .all()
    )

    assert registered.issue_id == issue_id
    assert [linked.id for linked in linked_files] == [registered.id]
    assert _state_values(preserved) == before
    assert snapshot is not None
    assert manifest.page_count == replacement_page_count
    assert _manifest_initial_page(snapshot, current_page_count=manifest.page_count) == (
        expected_initial_page
    )


@pytest.mark.asyncio
async def test_remove_and_reimport_same_issue_preserve_state_and_restore_readability(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    user, issue, library_file, state = await _seed_reader_file(
        db_session,
        root_path=tmp_path / "library",
    )
    await db_session.commit()
    before = _state_values(state)
    removed_path = Path(library_file.file_path)
    replacement = tmp_path / "incoming" / "same-issue.cbz"
    _write_cbz(replacement, page_count=3)

    await delete_issue_library_file(db_session, issue.id)
    await db_session.commit()

    unavailable_state = await _load_state_row(db_session, state.id)
    unavailable_access = await load_reader_issue_access(db_session, issue_id=issue.id)
    unavailable_continue = await list_continue_reading(
        db_session,
        user_id=user.id,
        page=1,
        per_page=10,
    )
    unavailable_queue = await list_want_to_read(
        db_session,
        user_id=user.id,
        page=1,
        per_page=10,
    )

    assert removed_path.exists() is False
    assert _state_values(unavailable_state) == before
    assert unavailable_access is not None and unavailable_access.readable is False
    assert unavailable_continue.items == ()
    assert len(unavailable_queue.items) == 1
    assert unavailable_queue.items[0].readable is False

    reimported = await register_library_file(
        db_session,
        replacement,
        issue,
        MatchConfidence.MANUAL,
        move_to_library=True,
        rename=False,
        library_root_id=library_file.library_root_id,
        transfer_method="copy",
        normalize_to_cbz=False,
        update_embedded_comicinfo_from_match=False,
    )
    await db_session.commit()

    restored_state = await _load_state_row(db_session, state.id)
    restored_access = await load_reader_issue_access(db_session, issue_id=issue.id)
    restored_continue = await list_continue_reading(
        db_session,
        user_id=user.id,
        page=1,
        per_page=10,
    )

    assert reimported.issue_id == issue.id
    assert _state_values(restored_state) == before
    assert restored_access is not None and restored_access.readable is True
    assert [item.issue_id for item in restored_continue.items] == [issue.id]


@pytest.mark.asyncio
async def test_deleting_canonical_issue_cascades_reader_state(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    await db_session.execute(text("PRAGMA foreign_keys=ON"))
    _user, issue, _library_file, state = await _seed_reader_file(
        db_session,
        root_path=tmp_path / "library",
    )
    issue_id = issue.id
    state_id = state.id
    await db_session.commit()

    await db_session.delete(issue)
    await db_session.commit()
    db_session.expire_all()

    assert await db_session.get(Issue, issue_id) is None
    assert await db_session.get(IssueReaderState, state_id) is None


def test_reader_state_schema_has_no_file_path_lookup_key() -> None:
    columns = set(IssueReaderState.__table__.columns.keys())
    foreign_keys = {
        str(foreign_key.column) for foreign_key in IssueReaderState.__table__.foreign_keys
    }

    assert "file_path" not in columns
    assert "library_file_id" not in columns
    assert foreign_keys == {"users.id", "issues.id"}
