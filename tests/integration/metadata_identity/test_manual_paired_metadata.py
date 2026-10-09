"""Manual issue imports reconcile a pair before touching the managed library."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from zipfile import ZipFile

import py7zr
import pytest
from sqlalchemy import delete, select, update

from pullbox.config import get_settings
from pullbox.core.exceptions import ValidationError
from pullbox.core.file_safety import FileSafetyError
from pullbox.models.archive_metadata_publication import ArchiveMetadataPublication
from pullbox.models.config import SystemConfig
from pullbox.models.download import DownloadHistory
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile, LibraryFileStorageMode, LibraryRoot
from pullbox.models.library_conversion import LibraryConversion
from pullbox.services import manual_paired_metadata
from pullbox.services.archive_metadata_binding import ArchiveMetadataBindingError
from pullbox.services.issue_import_service import (
    ManualIssueImportError,
    execute_manual_issue_import,
    prepare_manual_issue_import,
)
from tests.integration.metadata_identity.test_archive_metadata_binding import seed
from tests.integration.metadata_identity.test_download_paired_metadata import completed_download

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.fixtures.metadata_identity_persistence import IdentityProbeDatabase


@pytest.fixture
def paired_manual_writer(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("PULLBOX_METADATA_PAIRED_IMPORT_WRITER_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
@pytest.mark.usefixtures("paired_manual_writer")
async def test_manual_import_reconciles_pair_and_preserves_source(
    identity_probe_db: IdentityProbeDatabase, tmp_path: Path
) -> None:
    _, factory, _ = identity_probe_db
    download_id, issue_id, source = await completed_download(factory, tmp_path, method="move")
    original = source.read_bytes()
    async with factory.begin() as session:
        await session.execute(delete(DownloadHistory).where(DownloadHistory.id == download_id))
    async with factory() as session:
        prepared = await prepare_manual_issue_import(
            session, issue_id=issue_id, file_path=str(source), move_to_library=True
        )
        result = await execute_manual_issue_import(session, prepared)
        await session.commit()
        assert result.library_file.has_comicinfo is True
        with ZipFile(result.library_file.file_path) as archive:
            assert "MetronInfo.xml" in archive.namelist()
            assert b"50-X" in archive.read("ComicInfo.xml")
            assert b"My local summary" in archive.read("MetronInfo.xml")
            assert archive.read("page.jpg") == b"page bytes"
    assert source.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.usefixtures("paired_manual_writer")
async def test_manual_conflict_keeps_existing_library_copy(
    identity_probe_db: IdentityProbeDatabase, tmp_path: Path
) -> None:
    _, factory, _ = identity_probe_db
    _, issue_id, source = await completed_download(factory, tmp_path, conflict=True)
    old = tmp_path / "comics" / "existing.cbz"
    old.parent.mkdir(exist_ok=True)
    with ZipFile(old, "w") as archive:
        archive.writestr("page.jpg", b"previous owned page")
    before = old.read_bytes()
    async with factory.begin() as session:
        root_id = await session.scalar(select(LibraryRoot.id).limit(1))
        session.add(
            LibraryFile(
                issue_id=issue_id,
                file_path=str(old),
                file_name=old.name,
                file_size=old.stat().st_size,
                file_modified_at=datetime.now(UTC),
                file_format="cbz",
                match_confidence="manual",
                library_root_id=root_id,
            )
        )
    async with factory() as session:
        prepared = await prepare_manual_issue_import(
            session, issue_id=issue_id, file_path=str(source), move_to_library=True
        )
        with pytest.raises(ManualIssueImportError, match="metadata needs review"):
            await execute_manual_issue_import(session, prepared)
        await session.rollback()
    assert old.read_bytes() == before
    async with factory() as session:
        assert await session.scalar(
            select(LibraryFile.file_path).where(LibraryFile.issue_id == issue_id)
        ) == str(old)


@pytest.mark.asyncio
@pytest.mark.usefixtures("paired_manual_writer")
async def test_manual_pair_can_replace_an_idle_managed_file(
    identity_probe_db: IdentityProbeDatabase, tmp_path: Path
) -> None:
    _, factory, _ = identity_probe_db
    file_id, issue_id, _, root_id, _ = await seed(factory, tmp_path)
    source = tmp_path / "new-copy.cbz"
    with ZipFile(source, "w") as archive:
        archive.writestr("page.jpg", b"replacement page")
    original = source.read_bytes()
    async with factory.begin() as session:
        await session.execute(
            update(LibraryRoot)
            .where(LibraryRoot.id == root_id)
            .values(is_default_managed_destination=True)
        )
        session.add(
            SystemConfig(key="update_embedded_comicinfo_from_match_on_import", value="true")
        )
    async with factory() as session:
        prepared = await prepare_manual_issue_import(
            session, issue_id=issue_id, file_path=str(source), move_to_library=True
        )
        result = await asyncio.wait_for(execute_manual_issue_import(session, prepared), timeout=10)
        await session.commit()
        assert result.library_file.id == file_id
        assert result.library_file.has_comicinfo is True
        with ZipFile(result.library_file.file_path) as archive:
            assert "MetronInfo.xml" in archive.namelist()
            assert archive.read("page.jpg") == b"replacement page"
    assert source.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.usefixtures("paired_manual_writer")
async def test_manual_metadata_change_during_staging_does_not_publish(
    identity_probe_db: IdentityProbeDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory, _ = identity_probe_db
    _, issue_id, source = await completed_download(factory, tmp_path)
    original = source.read_bytes()
    stage = manual_paired_metadata.stage_cbz_metadata_interruptible

    @asynccontextmanager
    async def change_after_stage(*args: Any, **kwargs: Any):
        async with stage(*args, **kwargs) as staged:
            async with factory.begin() as writer:
                await writer.execute(
                    update(Issue).where(Issue.id == issue_id).values(title="Changed")
                )
            yield staged

    monkeypatch.setattr(
        manual_paired_metadata, "stage_cbz_metadata_interruptible", change_after_stage
    )
    async with factory() as session:
        prepared = await prepare_manual_issue_import(
            session, issue_id=issue_id, file_path=str(source), move_to_library=True
        )
        with pytest.raises(ManualIssueImportError, match="metadata needs review"):
            await execute_manual_issue_import(session, prepared)
        await session.rollback()
    assert source.read_bytes() == original
    assert not list((tmp_path / "comics").rglob("*.cbz"))


@pytest.mark.asyncio
@pytest.mark.usefixtures("paired_manual_writer")
async def test_manual_paired_size_approval_is_file_scoped(
    identity_probe_db: IdentityProbeDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory, _ = identity_probe_db
    _, issue_id, source = await completed_download(factory, tmp_path)
    original = source.read_bytes()

    async def tiny_limit(_session: object) -> int:
        return 10

    monkeypatch.setattr(manual_paired_metadata, "get_archive_size_limit_bytes", tiny_limit)
    async with factory() as session:
        prepared = await prepare_manual_issue_import(
            session, issue_id=issue_id, file_path=str(source), move_to_library=True
        )
        with pytest.raises(FileSafetyError, match="exceeds limit"):
            await execute_manual_issue_import(session, prepared)
        await session.rollback()
    async with factory() as session:
        prepared = await prepare_manual_issue_import(
            session, issue_id=issue_id, file_path=str(source), move_to_library=True
        )
        result = await execute_manual_issue_import(
            session, prepared, allow_resource_safety_exception=True
        )
        await session.commit()
        with ZipFile(result.library_file.file_path) as archive:
            assert "MetronInfo.xml" in archive.namelist()
    assert source.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.usefixtures("paired_manual_writer")
async def test_manual_paired_cancel_never_publishes(
    identity_probe_db: IdentityProbeDatabase, tmp_path: Path
) -> None:
    _, factory, _ = identity_probe_db
    _, issue_id, source = await completed_download(factory, tmp_path)
    original = source.read_bytes()
    checks = 0

    async def check_control() -> None:
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise asyncio.CancelledError

    async with factory() as session:
        prepared = await prepare_manual_issue_import(
            session, issue_id=issue_id, file_path=str(source), move_to_library=True
        )
        with pytest.raises(asyncio.CancelledError):
            await execute_manual_issue_import(session, prepared, cancellation_check=check_control)
        await session.rollback()
    assert source.read_bytes() == original
    assert not list((tmp_path / "comics").rglob("*.cbz"))


@pytest.mark.asyncio
@pytest.mark.usefixtures("paired_manual_writer")
async def test_manual_embedding_disabled_keeps_existing_behavior(
    identity_probe_db: IdentityProbeDatabase, tmp_path: Path
) -> None:
    _, factory, _ = identity_probe_db
    _, issue_id, source = await completed_download(factory, tmp_path)
    original = source.read_bytes()
    async with factory.begin() as session:
        await session.execute(
            update(SystemConfig)
            .where(SystemConfig.key == "update_embedded_comicinfo_from_match_on_import")
            .values(value="false")
        )
    async with factory() as session:
        prepared = await prepare_manual_issue_import(
            session, issue_id=issue_id, file_path=str(source), move_to_library=True
        )
        result = await execute_manual_issue_import(session, prepared)
        await session.commit()
        assert Path(result.library_file.file_path).read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.usefixtures("paired_manual_writer")
async def test_manual_import_stops_metadata_disagreement_before_publication(
    identity_probe_db: IdentityProbeDatabase, tmp_path: Path
) -> None:
    _, factory, _ = identity_probe_db
    download_id, issue_id, source = await completed_download(factory, tmp_path, conflict=True)
    original = source.read_bytes()
    async with factory.begin() as session:
        await session.execute(delete(DownloadHistory).where(DownloadHistory.id == download_id))
    async with factory() as session:
        prepared = await prepare_manual_issue_import(
            session, issue_id=issue_id, file_path=str(source), move_to_library=True
        )
        with pytest.raises(ManualIssueImportError, match=r"metadata.*review|review.*metadata"):
            await execute_manual_issue_import(session, prepared)
        await session.rollback()
    assert source.read_bytes() == original
    assert not list((tmp_path / "comics").rglob("*.cbz"))
    async with factory() as session:
        assert (
            await session.scalar(select(LibraryFile.id).where(LibraryFile.issue_id == issue_id))
            is None
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("paired_manual_writer")
async def test_manual_native_conversion_preserves_original_and_writes_pair(
    identity_probe_db: IdentityProbeDatabase, tmp_path: Path
) -> None:
    _, factory, _ = identity_probe_db
    _, issue_id, cbz = await completed_download(factory, tmp_path)
    source = cbz.with_suffix(".cb7")
    contents = tmp_path / "archive-contents"
    contents.mkdir()
    with ZipFile(cbz) as archive:
        for name in archive.namelist():
            (contents / name).write_bytes(archive.read(name))
    with py7zr.SevenZipFile(source, "w") as archive:
        for path in contents.iterdir():
            archive.write(path, path.name)
    original = source.read_bytes()
    async with factory() as session:
        prepared = await prepare_manual_issue_import(
            session, issue_id=issue_id, file_path=str(source), move_to_library=True
        )
        result = await execute_manual_issue_import(session, prepared)
        await session.commit()
        with ZipFile(result.library_file.file_path) as archive:
            assert "MetronInfo.xml" in archive.namelist()
            assert archive.read("page.jpg") == b"page bytes"
    assert source.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.usefixtures("paired_manual_writer")
async def test_manual_size_approval_never_allows_traversal(
    identity_probe_db: IdentityProbeDatabase, tmp_path: Path
) -> None:
    _, factory, _ = identity_probe_db
    _, issue_id, source = await completed_download(factory, tmp_path)
    with ZipFile(source, "a") as archive:
        archive.writestr("../escaped.jpg", b"unsafe")
    original = source.read_bytes()
    async with factory() as session:
        prepared = await prepare_manual_issue_import(
            session, issue_id=issue_id, file_path=str(source), move_to_library=True
        )
        with pytest.raises(ManualIssueImportError, match="prepared safely"):
            await execute_manual_issue_import(
                session, prepared, allow_resource_safety_exception=True
            )
        await session.rollback()
    assert source.read_bytes() == original
    assert not list((tmp_path / "comics").rglob("*.cbz"))


@pytest.mark.asyncio
async def test_manual_pair_cannot_replace_a_referenced_library_file(
    identity_probe_db: IdentityProbeDatabase, tmp_path: Path
) -> None:
    _, factory, _ = identity_probe_db
    file_id, issue_id, _, root_id, source = await seed(factory, tmp_path)
    original = source.read_bytes()
    async with factory.begin() as session:
        await session.execute(
            update(LibraryRoot)
            .where(LibraryRoot.id == root_id)
            .values(is_default_managed_destination=True)
        )
        await session.execute(
            update(LibraryFile)
            .where(LibraryFile.id == file_id)
            .values(storage_mode=LibraryFileStorageMode.REFERENCED)
        )
    async with factory() as session:
        with pytest.raises(ValidationError, match="Referenced library files"):
            await manual_paired_metadata.read_manual_metadata_plan(session, issue_id)
    assert source.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["metadata", "conversion"])
async def test_manual_pair_cannot_replace_a_file_with_an_active_operation(
    identity_probe_db: IdentityProbeDatabase, tmp_path: Path, operation: str
) -> None:
    _, factory, _ = identity_probe_db
    file_id, issue_id, _, root_id, source = await seed(factory, tmp_path)
    original = source.read_bytes()
    async with factory.begin() as session:
        await session.execute(
            update(LibraryRoot)
            .where(LibraryRoot.id == root_id)
            .values(is_default_managed_destination=True)
        )
        if operation == "metadata":
            session.add(
                ArchiveMetadataPublication(
                    operation_id=str(uuid4()),
                    library_file_id=file_id,
                    active_file_id=file_id,
                    active_path_key="a" * 64,
                    plan_json="{}",
                )
            )
        else:
            session.add(
                LibraryConversion(
                    operation_id=str(uuid4()), library_file_id=file_id, plan_json="{}"
                )
            )
    async with factory() as session:
        with pytest.raises(ArchiveMetadataBindingError, match="publication_busy"):
            await manual_paired_metadata.read_manual_metadata_plan(session, issue_id)
    assert source.read_bytes() == original
