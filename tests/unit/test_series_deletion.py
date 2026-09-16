"""Tests for series deletion with folder deletion and download cancellation (C-3.1).

Covers:
- Series record + cascade deletion
- LibraryFile deletion + physical file removal (delete_files=True)
- Series folder removal (delete_folder=True)
- Active download cancellation on client before deletion
- Progress cache cleared for active downloads
- Folder-as-symlink removed without following
- PermissionError on folder handled gracefully
- Nonexistent series raises NotFoundError
- Series with special characters in folder path
- Concurrent delete scenarios
- Series with no files, no downloads — clean delete

Run:
    pytest tests/unit/test_series_deletion.py -v
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.core.exceptions import NotFoundError
from pullbox.models import Base
from pullbox.models.download import (
    DownloadClientType,
    DownloadHistory,
    DownloadState,
)
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import (
    FileFormat,
    LibraryFile,
    LibraryFileStorageMode,
    LibraryRoot,
    MatchConfidence,
)
from pullbox.models.series import Series
from pullbox.services.series_service import SeriesService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-series-delete")

REGISTER_DOWNLOAD_CLIENTS_PATH = "pullbox.composition.providers.register_download_clients"


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """In-memory database with all tables."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def session(
    db_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    """Provide a single session for test methods."""
    async with db_factory() as s:
        yield s


@pytest.fixture
async def library_root(session: AsyncSession) -> LibraryRoot:
    """Create a LibraryRoot record for LibraryFile FK."""
    root = LibraryRoot(
        name="Test Root",
        path="/comics",
        enabled=True,
        last_scan_at=datetime.now(tz=UTC),
    )
    session.add(root)
    await session.flush()
    return root


# ── Helpers ───────────────────────────────────────────────────────────


async def _seed_series(
    session: AsyncSession,
    *,
    title: str = "Batman",
    year_start: int = 2020,
    path: str | None = "/comics/Batman (2020)",
) -> Series:
    """Create a Series record."""
    series = Series(
        title=title,
        sort_title=title.lower(),
        year_start=year_start,
        path=path,
        comicvine_id=12345,
        monitored=True,
    )
    session.add(series)
    await session.flush()
    return series


async def _seed_issue(
    session: AsyncSession,
    series_id: int,
    *,
    issue_number: float = 1.0,
    status: IssueStatus = IssueStatus.WANTED,
) -> Issue:
    """Create an Issue record linked to a series."""
    issue = Issue(
        series_id=series_id,
        issue_number=issue_number,
        title=f"Issue #{issue_number}",
        status=status,
    )
    session.add(issue)
    await session.flush()
    return issue


async def _seed_library_file(
    session: AsyncSession,
    issue_id: int,
    library_root_id: int,
    *,
    file_path: str = "/comics/Batman (2020)/Batman 001.cbz",
    storage_mode: LibraryFileStorageMode = LibraryFileStorageMode.MANAGED,
) -> LibraryFile:
    """Create a LibraryFile record linked to an issue."""
    lf = LibraryFile(
        file_path=file_path,
        file_name=Path(file_path).name,
        file_size=10_000_000,
        file_format=FileFormat.CBZ,
        match_confidence=MatchConfidence.HIGH,
        issue_id=issue_id,
        library_root_id=library_root_id,
        file_modified_at=datetime.now(tz=UTC),
        storage_mode=storage_mode,
    )
    session.add(lf)
    await session.flush()
    return lf


async def _seed_download(
    session: AsyncSession,
    issue_id: int,
    *,
    state: DownloadState = DownloadState.DOWNLOADING,
    client: DownloadClientType = DownloadClientType.SABNZBD,
    external_id: str | None = "nzo_abc123",
) -> DownloadHistory:
    """Create a DownloadHistory record."""
    dl = DownloadHistory(
        issue_id=issue_id,
        title="Batman.001.cbz",
        download_url="https://example.com/nzb/123",
        download_client=client,
        external_id=external_id,
        state=state,
    )
    session.add(dl)
    await session.flush()
    return dl


# ── Test: Basic Series Deletion ──────────────────────────────────────


class TestBasicDeletion:
    """Series record removal and cascade behavior."""

    @pytest.mark.asyncio
    async def test_delete_removes_series_record(self, session: AsyncSession) -> None:
        series = await _seed_series(session)
        sid = series.id

        await SeriesService.delete(session, sid)
        await session.flush()

        result = await session.get(Series, sid)
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_cascades_to_issues(self, session: AsyncSession) -> None:
        series = await _seed_series(session)
        issue = await _seed_issue(session, series.id)
        iid = issue.id

        await SeriesService.delete(session, series.id)
        await session.flush()

        result = await session.get(Issue, iid)
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_with_no_issues_no_files_no_downloads(self, session: AsyncSession) -> None:
        """Clean series with nothing attached deletes without error."""
        series = await _seed_series(session)
        await SeriesService.delete(session, series.id)
        await session.flush()

        assert await session.get(Series, series.id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_series_raises_not_found(self, session: AsyncSession) -> None:
        with pytest.raises(NotFoundError):
            await SeriesService.delete(session, 99999)

    @pytest.mark.asyncio
    async def test_delete_series_with_no_path(self, session: AsyncSession) -> None:
        """Series with no folder path deletes without folder-related errors."""
        series = await _seed_series(session, path=None)
        await SeriesService.delete(session, series.id, delete_folder=True)
        await session.flush()

        assert await session.get(Series, series.id) is None

    @pytest.mark.asyncio
    async def test_delete_removes_cached_cover_dirs(
        self,
        session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        """Deleting a series also clears any id-keyed cover cache directories."""
        series = await _seed_series(session)
        active_base = tmp_path / "active-covers"
        legacy_base = tmp_path / "legacy-covers"
        for base in (active_base, legacy_base):
            cover_dir = base / str(series.id)
            cover_dir.mkdir(parents=True)
            (cover_dir / "series.jpg").write_bytes(b"stale-cover")

        settings = SimpleNamespace(covers_dir=legacy_base)
        with (
            patch(
                "pullbox.services.cover_cache_service.resolve_covers_dir",
                AsyncMock(return_value=active_base),
            ),
            patch("pullbox.services.cover_cache_service.get_settings", return_value=settings),
        ):
            await SeriesService.delete(session, series.id)
            await session.flush()

        assert not (active_base / str(series.id)).exists()
        assert not (legacy_base / str(series.id)).exists()


# ── Test: File Deletion ──────────────────────────────────────────────


class TestFileDeletion:
    """Physical file and LibraryFile record removal."""

    @pytest.mark.asyncio
    async def test_delete_files_removes_library_file_records(
        self, session: AsyncSession, library_root: LibraryRoot
    ) -> None:
        series = await _seed_series(session)
        issue = await _seed_issue(session, series.id)
        lf = await _seed_library_file(session, issue.id, library_root.id)
        lf_id = lf.id

        with patch("pathlib.Path.is_file", return_value=True), patch("pathlib.Path.unlink"):
            await SeriesService.delete(session, series.id, delete_files=True)
            await session.flush()

        result = await session.get(LibraryFile, lf_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_files_unlinks_physical_files(
        self, session: AsyncSession, library_root: LibraryRoot, tmp_path: Path
    ) -> None:
        """Physical files are removed from disk."""
        comic_file = tmp_path / "Batman 001.cbz"
        comic_file.write_bytes(b"fake cbz data")

        series = await _seed_series(session, path=str(tmp_path))
        issue = await _seed_issue(session, series.id)
        await _seed_library_file(session, issue.id, library_root.id, file_path=str(comic_file))

        await SeriesService.delete(session, series.id, delete_files=True)
        await session.flush()

        assert not comic_file.exists()

    @pytest.mark.asyncio
    async def test_delete_files_detaches_referenced_file_without_unlinking_it(
        self, session: AsyncSession, library_root: LibraryRoot, tmp_path: Path
    ) -> None:
        comic_file = tmp_path / "Referenced Batman 001.cbz"
        original = b"user-owned comic"
        comic_file.write_bytes(original)
        series = await _seed_series(session, path=str(tmp_path))
        issue = await _seed_issue(session, series.id)
        library_file = await _seed_library_file(
            session,
            issue.id,
            library_root.id,
            file_path=str(comic_file),
            storage_mode=LibraryFileStorageMode.REFERENCED,
        )

        await SeriesService.delete(session, series.id, delete_files=True)
        await session.flush()

        assert comic_file.read_bytes() == original
        assert await session.get(LibraryFile, library_file.id) is None

    @pytest.mark.asyncio
    async def test_delete_files_handles_missing_file(
        self, session: AsyncSession, library_root: LibraryRoot
    ) -> None:
        """File already gone from disk — no error."""
        series = await _seed_series(session)
        issue = await _seed_issue(session, series.id)
        await _seed_library_file(
            session,
            issue.id,
            library_root.id,
            file_path="/nonexistent/Batman 001.cbz",
        )

        # Should not raise
        await SeriesService.delete(session, series.id, delete_files=True)
        await session.flush()

        assert await session.get(Series, series.id) is None

    @pytest.mark.asyncio
    async def test_delete_files_handles_unlink_os_error(
        self, session: AsyncSession, library_root: LibraryRoot
    ) -> None:
        """OSError on unlink is logged but doesn't abort deletion."""
        series = await _seed_series(session)
        issue = await _seed_issue(session, series.id)
        await _seed_library_file(session, issue.id, library_root.id)

        with (
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.unlink", side_effect=OSError("Permission denied")),
        ):
            await SeriesService.delete(session, series.id, delete_files=True)
            await session.flush()

        assert await session.get(Series, series.id) is None

    @pytest.mark.asyncio
    async def test_delete_without_delete_files_preserves_files(
        self, session: AsyncSession, library_root: LibraryRoot, tmp_path: Path
    ) -> None:
        """When delete_files=False, physical files remain."""
        comic_file = tmp_path / "Batman 001.cbz"
        comic_file.write_bytes(b"fake cbz data")

        series = await _seed_series(session, path=str(tmp_path))
        issue = await _seed_issue(session, series.id)
        await _seed_library_file(session, issue.id, library_root.id, file_path=str(comic_file))

        await SeriesService.delete(session, series.id, delete_files=False)
        await session.flush()

        # Series deleted but file remains on disk
        assert await session.get(Series, series.id) is None
        assert comic_file.exists()

    @pytest.mark.asyncio
    async def test_delete_files_multiple_issues(
        self, session: AsyncSession, library_root: LibraryRoot, tmp_path: Path
    ) -> None:
        """Multiple files across multiple issues all removed."""
        series = await _seed_series(session, path=str(tmp_path))
        files = []
        for i in range(3):
            issue = await _seed_issue(session, series.id, issue_number=float(i + 1))
            f = tmp_path / f"Batman {i + 1:03d}.cbz"
            f.write_bytes(b"data")
            await _seed_library_file(session, issue.id, library_root.id, file_path=str(f))
            files.append(f)

        await SeriesService.delete(session, series.id, delete_files=True)
        await session.flush()

        for f in files:
            assert not f.exists()


# ── Test: Folder Deletion ────────────────────────────────────────────


class TestFolderDeletion:
    """Series folder removal from disk."""

    @pytest.mark.asyncio
    async def test_delete_folder_removes_directory(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        folder = tmp_path / "Batman (2020)"
        folder.mkdir()
        (folder / "cover.jpg").write_bytes(b"image")

        series = await _seed_series(session, path=str(folder))
        await SeriesService.delete(session, series.id, delete_folder=True)
        await session.flush()

        assert not folder.exists()

    @pytest.mark.asyncio
    async def test_delete_folder_when_folder_missing(self, session: AsyncSession) -> None:
        """Folder already gone — no error."""
        series = await _seed_series(session, path="/nonexistent/Batman (2020)")
        await SeriesService.delete(session, series.id, delete_folder=True)
        await session.flush()

        assert await session.get(Series, series.id) is None

    @pytest.mark.asyncio
    async def test_delete_folder_symlink_removed_not_followed(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        """Symlink folder — remove the link, not the target."""
        target = tmp_path / "real_folder"
        target.mkdir()
        (target / "file.txt").write_text("keep me")

        link = tmp_path / "Batman (2020)"
        link.symlink_to(target)

        series = await _seed_series(session, path=str(link))
        await SeriesService.delete(session, series.id, delete_folder=True)
        await session.flush()

        assert not link.exists()
        assert target.exists(), "Target folder should not be deleted"
        assert (target / "file.txt").exists()

    @pytest.mark.asyncio
    async def test_delete_folder_permission_error(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        """PermissionError on rmtree is logged but deletion proceeds."""
        folder = tmp_path / "Batman (2020)"
        folder.mkdir()

        series = await _seed_series(session, path=str(folder))

        with patch("shutil.rmtree", side_effect=PermissionError("read-only")):
            await SeriesService.delete(session, series.id, delete_folder=True)
            await session.flush()

        # Series record still deleted even if folder removal failed
        assert await session.get(Series, series.id) is None

    @pytest.mark.asyncio
    async def test_delete_folder_with_non_pullbox_files(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        """Folder containing extra files — user chose to delete, so delete all."""
        folder = tmp_path / "Batman (2020)"
        folder.mkdir()
        (folder / "readme.txt").write_text("user notes")
        (folder / "extra.nfo").write_text("nfo data")

        series = await _seed_series(session, path=str(folder))
        await SeriesService.delete(session, series.id, delete_folder=True)
        await session.flush()

        assert not folder.exists()

    @pytest.mark.asyncio
    async def test_delete_folder_special_characters_in_path(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        """Folder with special characters in name."""
        folder = tmp_path / "Über-Comics! (2020) [Special]"
        folder.mkdir()

        series = await _seed_series(session, title="Über-Comics!", path=str(folder))
        await SeriesService.delete(session, series.id, delete_folder=True)
        await session.flush()

        assert not folder.exists()

    @pytest.mark.asyncio
    async def test_delete_files_and_folder_together(
        self, session: AsyncSession, library_root: LibraryRoot, tmp_path: Path
    ) -> None:
        """Both delete_files and delete_folder — files removed then folder removed."""
        folder = tmp_path / "Batman (2020)"
        folder.mkdir()
        comic = folder / "Batman 001.cbz"
        comic.write_bytes(b"data")

        series = await _seed_series(session, path=str(folder))
        issue = await _seed_issue(session, series.id)
        await _seed_library_file(session, issue.id, library_root.id, file_path=str(comic))

        await SeriesService.delete(session, series.id, delete_files=True, delete_folder=True)
        await session.flush()

        assert not folder.exists()
        assert await session.get(Series, series.id) is None

    @pytest.mark.asyncio
    async def test_delete_folder_preserves_folder_containing_referenced_file(
        self, session: AsyncSession, library_root: LibraryRoot, tmp_path: Path
    ) -> None:
        folder = tmp_path / "Referenced Batman"
        folder.mkdir()
        referenced = folder / "Batman 001.cbz"
        managed = folder / "Batman 002.cbz"
        referenced.write_bytes(b"reference")
        managed.write_bytes(b"managed")
        series = await _seed_series(session, path=str(folder))
        first_issue = await _seed_issue(session, series.id)
        second_issue = await _seed_issue(session, series.id, issue_number=2.0)
        await _seed_library_file(
            session,
            first_issue.id,
            library_root.id,
            file_path=str(referenced),
            storage_mode=LibraryFileStorageMode.REFERENCED,
        )
        await _seed_library_file(
            session,
            second_issue.id,
            library_root.id,
            file_path=str(managed),
        )

        await SeriesService.delete(session, series.id, delete_folder=True)
        await session.flush()

        assert folder.is_dir()
        assert referenced.read_bytes() == b"reference"
        assert not managed.exists()
        assert await session.get(Series, series.id) is None

    @pytest.mark.parametrize("use_trash", [False, True])
    @pytest.mark.parametrize("shared", [False, True])
    @pytest.mark.asyncio
    async def test_delete_shared_folder_preserves_other_series_reference(
        self,
        session: AsyncSession,
        library_root: LibraryRoot,
        tmp_path: Path,
        use_trash: bool,
        shared: bool,
    ) -> None:
        folder = tmp_path / "Shared_% Library"
        folder.mkdir()
        other_folder = folder if shared else tmp_path / "Shared_AB Library"
        other_folder.mkdir(exist_ok=True)
        referenced = other_folder / "other.cbz"
        referenced.write_bytes(b"user-owned")
        managed = folder / "managed.cbz"
        managed.write_bytes(b"managed")
        series = await _seed_series(session, path=str(folder))
        other = Series(title="Other Series", sort_title="other series", path=str(other_folder))
        session.add(other)
        await session.flush()
        owned_issue = await _seed_issue(session, series.id)
        other_issue = await _seed_issue(session, other.id)
        await _seed_library_file(session, owned_issue.id, library_root.id, file_path=str(managed))
        reference = await _seed_library_file(
            session,
            other_issue.id,
            library_root.id,
            file_path=str(referenced),
            storage_mode=LibraryFileStorageMode.REFERENCED,
        )
        await SeriesService.delete(
            session,
            series.id,
            delete_folder=True,
            trash_dir=tmp_path / "trash" if use_trash else None,
        )
        await session.flush()
        assert folder.is_dir() is shared
        assert referenced.read_bytes() == b"user-owned"
        assert not managed.exists()
        assert await session.get(Series, series.id) is None
        assert await session.get(Series, other.id) is other
        assert await session.get(LibraryFile, reference.id) is reference

    @pytest.mark.asyncio
    async def test_delete_folder_resolves_expected_folder_when_series_path_missing(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        """Delete-folder cleanup still finds the canonical folder when Series.path is empty."""
        root = LibraryRoot(
            name="Comics",
            path=str(tmp_path),
            enabled=True,
            last_scan_at=datetime.now(tz=UTC),
        )
        session.add(root)
        await session.flush()

        folder = tmp_path / "Batman (2020)"
        folder.mkdir()
        (folder / "Batman 001.cbz").write_bytes(b"data")

        series = Series(
            title="Batman",
            sort_title="batman",
            year_start=2020,
            path=None,
            comicvine_id=12345,
            monitored=True,
            library_root_id=root.id,
        )
        session.add(series)
        await session.flush()

        await SeriesService.delete(session, series.id, delete_folder=True)
        await session.flush()

        assert not folder.exists()
        assert await session.get(Series, series.id) is None

    @pytest.mark.asyncio
    async def test_delete_folder_resolves_cv_suffix_folder_when_series_path_missing(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        """Delete-folder cleanup also finds a collision-resolved [cv-id] folder."""
        root = LibraryRoot(
            name="Comics",
            path=str(tmp_path),
            enabled=True,
            last_scan_at=datetime.now(tz=UTC),
        )
        session.add(root)
        await session.flush()

        folder = tmp_path / "Batman (2020) [cv-12345]"
        folder.mkdir()
        (folder / "Batman 001.cbz").write_bytes(b"data")

        series = Series(
            title="Batman",
            sort_title="batman",
            year_start=2020,
            path=None,
            comicvine_id=12345,
            monitored=True,
            library_root_id=root.id,
        )
        session.add(series)
        await session.flush()

        await SeriesService.delete(session, series.id, delete_folder=True)
        await session.flush()

        assert not folder.exists()
        assert await session.get(Series, series.id) is None

    @pytest.mark.asyncio
    async def test_delete_folder_resolves_parent_from_linked_file_when_series_metadata_missing(
        self, session: AsyncSession, library_root: LibraryRoot, tmp_path: Path
    ) -> None:
        """Linked file paths can still supply the series folder when series.path/root are empty."""
        folder = tmp_path / "Babyteeth (2017)"
        folder.mkdir()
        comic = folder / "Babyteeth 001.cbz"
        comic.write_bytes(b"data")

        series = await _seed_series(
            session,
            title="Babyteeth",
            year_start=2017,
            path=None,
        )
        issue = await _seed_issue(session, series.id)
        await _seed_library_file(session, issue.id, library_root.id, file_path=str(comic))

        await SeriesService.delete(session, series.id, delete_folder=True)
        await session.flush()

        assert not folder.exists()
        assert await session.get(Series, series.id) is None


# ── Test: Download Cancellation ──────────────────────────────────────


class TestDownloadCancellation:
    """Active downloads are cancelled on the client before series deletion."""

    @pytest.mark.asyncio
    async def test_active_download_cancelled_on_sabnzbd(self, session: AsyncSession) -> None:
        mock_client = AsyncMock()
        mock_client.remove_download = AsyncMock(return_value=True)

        series = await _seed_series(session)
        issue = await _seed_issue(session, series.id, status=IssueStatus.DOWNLOADING)
        await _seed_download(
            session,
            issue.id,
            state=DownloadState.DOWNLOADING,
            client=DownloadClientType.SABNZBD,
            external_id="nzo_abc",
        )

        mock_registry = MagicMock()
        mock_registry.get_nzb_client.return_value = mock_client
        mock_registry.get_torrent_client.return_value = None

        with (
            patch(REGISTER_DOWNLOAD_CLIENTS_PATH, new_callable=AsyncMock),
            patch("pullbox.providers.base.ProviderRegistry", return_value=mock_registry),
            patch("pullbox.tasks.download_task._clear_progress"),
        ):
            await SeriesService.delete(session, series.id)
            await session.flush()

        mock_client.remove_download.assert_called_once_with("nzo_abc", delete_files=True)

    @pytest.mark.asyncio
    async def test_active_download_cancelled_on_qbittorrent(self, session: AsyncSession) -> None:
        mock_client = AsyncMock()
        mock_client.remove_download = AsyncMock(return_value=True)

        series = await _seed_series(session)
        issue = await _seed_issue(session, series.id, status=IssueStatus.DOWNLOADING)
        await _seed_download(
            session,
            issue.id,
            state=DownloadState.DOWNLOADING,
            client=DownloadClientType.QBITTORRENT,
            external_id="hash_abc",
        )

        mock_registry = MagicMock()
        mock_registry.get_torrent_client.return_value = mock_client
        mock_registry.get_nzb_client.return_value = None

        with (
            patch(REGISTER_DOWNLOAD_CLIENTS_PATH, new_callable=AsyncMock),
            patch("pullbox.providers.base.ProviderRegistry", return_value=mock_registry),
            patch("pullbox.tasks.download_task._clear_progress"),
        ):
            await SeriesService.delete(session, series.id)
            await session.flush()

        mock_client.remove_download.assert_called_once_with("hash_abc", delete_files=True)

    @pytest.mark.asyncio
    async def test_active_download_marks_as_failed(self, session: AsyncSession) -> None:
        series = await _seed_series(session)
        issue = await _seed_issue(session, series.id, status=IssueStatus.DOWNLOADING)
        await _seed_download(session, issue.id, state=DownloadState.DOWNLOADING)

        with (
            patch(REGISTER_DOWNLOAD_CLIENTS_PATH, new_callable=AsyncMock),
            patch("pullbox.providers.base.ProviderRegistry") as mock_reg_cls,
            patch("pullbox.tasks.download_task._clear_progress"),
        ):
            mock_reg = MagicMock()
            mock_reg.get_nzb_client.return_value = AsyncMock(
                remove_download=AsyncMock(return_value=True)
            )
            mock_reg_cls.return_value = mock_reg
            await SeriesService.delete(session, series.id)

        # Download record cascades with the series, so we can't check it after delete.
        # The important thing is it didn't raise.

    @pytest.mark.asyncio
    async def test_progress_cache_cleared_for_active_downloads(self, session: AsyncSession) -> None:
        series = await _seed_series(session)
        issue = await _seed_issue(session, series.id)
        dl = await _seed_download(session, issue.id, state=DownloadState.DOWNLOADING)
        dl_id = dl.id

        with (
            patch(REGISTER_DOWNLOAD_CLIENTS_PATH, new_callable=AsyncMock),
            patch("pullbox.providers.base.ProviderRegistry") as mock_reg_cls,
            patch("pullbox.tasks.download_task._clear_progress") as mock_clear,
        ):
            mock_reg = MagicMock()
            mock_reg.get_nzb_client.return_value = AsyncMock(
                remove_download=AsyncMock(return_value=True)
            )
            mock_reg_cls.return_value = mock_reg
            await SeriesService.delete(session, series.id)

        mock_clear.assert_called_once_with(dl_id)

    @pytest.mark.asyncio
    async def test_multiple_active_downloads_all_cancelled(self, session: AsyncSession) -> None:
        series = await _seed_series(session)
        issue1 = await _seed_issue(session, series.id, issue_number=1.0)
        issue2 = await _seed_issue(session, series.id, issue_number=2.0)
        await _seed_download(
            session, issue1.id, state=DownloadState.DOWNLOADING, external_id="nzo_1"
        )
        await _seed_download(session, issue2.id, state=DownloadState.SENT, external_id="nzo_2")

        mock_client = AsyncMock()
        mock_client.remove_download = AsyncMock(return_value=True)

        with (
            patch(REGISTER_DOWNLOAD_CLIENTS_PATH, new_callable=AsyncMock),
            patch("pullbox.providers.base.ProviderRegistry") as mock_reg_cls,
            patch("pullbox.tasks.download_task._clear_progress"),
        ):
            mock_reg = MagicMock()
            mock_reg.get_nzb_client.return_value = mock_client
            mock_reg_cls.return_value = mock_reg
            await SeriesService.delete(session, series.id)
            await session.flush()

        assert mock_client.remove_download.call_count == 2

    @pytest.mark.asyncio
    async def test_download_without_external_id_skips_client_call(
        self, session: AsyncSession
    ) -> None:
        series = await _seed_series(session)
        issue = await _seed_issue(session, series.id)
        await _seed_download(session, issue.id, state=DownloadState.QUEUED, external_id=None)

        reg_path = REGISTER_DOWNLOAD_CLIENTS_PATH
        with (
            patch(reg_path, new_callable=AsyncMock) as mock_reg_dl,
            patch("pullbox.tasks.download_task._clear_progress"),
        ):
            await SeriesService.delete(session, series.id)
            await session.flush()

        # register_download_clients not called since no external_id
        mock_reg_dl.assert_not_called()

    @pytest.mark.asyncio
    async def test_client_error_does_not_abort_deletion(self, session: AsyncSession) -> None:
        """Download client error is logged but series deletion proceeds."""
        series = await _seed_series(session)
        issue = await _seed_issue(session, series.id)
        await _seed_download(
            session, issue.id, state=DownloadState.DOWNLOADING, external_id="nzo_err"
        )

        mock_client = AsyncMock()
        mock_client.remove_download = AsyncMock(side_effect=Exception("Connection refused"))

        with (
            patch(REGISTER_DOWNLOAD_CLIENTS_PATH, new_callable=AsyncMock),
            patch("pullbox.providers.base.ProviderRegistry") as mock_reg_cls,
            patch("pullbox.tasks.download_task._clear_progress"),
        ):
            mock_reg = MagicMock()
            mock_reg.get_nzb_client.return_value = mock_client
            mock_reg_cls.return_value = mock_reg
            await SeriesService.delete(session, series.id)
            await session.flush()

        assert await session.get(Series, series.id) is None

    @pytest.mark.asyncio
    async def test_completed_downloads_not_cancelled_on_client(self, session: AsyncSession) -> None:
        """Terminal-state downloads are NOT sent to the client for cancellation."""
        series = await _seed_series(session)
        issue = await _seed_issue(session, series.id)
        await _seed_download(
            session, issue.id, state=DownloadState.COMPLETED, external_id="nzo_done"
        )

        reg_path = REGISTER_DOWNLOAD_CLIENTS_PATH
        with (
            patch(reg_path, new_callable=AsyncMock) as mock_reg_dl,
            patch("pullbox.tasks.download_task._clear_progress") as mock_clear,
        ):
            await SeriesService.delete(session, series.id)
            await session.flush()

        mock_reg_dl.assert_not_called()
        mock_clear.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_cancellable_states_handled(self, session: AsyncSession) -> None:
        """QUEUED, SENT, DOWNLOADING, PAUSED, RETRY_PENDING all cancelled."""
        cancellable = [
            DownloadState.QUEUED,
            DownloadState.SENT,
            DownloadState.DOWNLOADING,
            DownloadState.PAUSED,
            DownloadState.RETRY_PENDING,
        ]
        series = await _seed_series(session)
        for i, state in enumerate(cancellable):
            issue = await _seed_issue(session, series.id, issue_number=float(i + 1))
            await _seed_download(session, issue.id, state=state, external_id=f"ext_{i}")

        mock_client = AsyncMock()
        mock_client.remove_download = AsyncMock(return_value=True)

        with (
            patch(REGISTER_DOWNLOAD_CLIENTS_PATH, new_callable=AsyncMock),
            patch("pullbox.providers.base.ProviderRegistry") as mock_reg_cls,
            patch("pullbox.tasks.download_task._clear_progress"),
        ):
            mock_reg = MagicMock()
            mock_reg.get_nzb_client.return_value = mock_client
            mock_reg_cls.return_value = mock_reg
            await SeriesService.delete(session, series.id)
            await session.flush()

        assert mock_client.remove_download.call_count == 5


# ── Test: Combined Scenarios ─────────────────────────────────────────


class TestCombinedScenarios:
    """Multi-concern deletion scenarios."""

    @pytest.mark.asyncio
    async def test_files_downloads_and_folder_all_handled(
        self, session: AsyncSession, library_root: LibraryRoot, tmp_path: Path
    ) -> None:
        """Series with active downloads, library files, and folder — all cleaned up."""
        folder = tmp_path / "Batman (2020)"
        folder.mkdir()
        comic = folder / "Batman 001.cbz"
        comic.write_bytes(b"data")

        series = await _seed_series(session, path=str(folder))
        issue = await _seed_issue(session, series.id, status=IssueStatus.DOWNLOADING)
        await _seed_library_file(session, issue.id, library_root.id, file_path=str(comic))
        await _seed_download(
            session, issue.id, state=DownloadState.DOWNLOADING, external_id="nzo_combo"
        )

        mock_client = AsyncMock()
        mock_client.remove_download = AsyncMock(return_value=True)

        with (
            patch(REGISTER_DOWNLOAD_CLIENTS_PATH, new_callable=AsyncMock),
            patch("pullbox.providers.base.ProviderRegistry") as mock_reg_cls,
            patch("pullbox.tasks.download_task._clear_progress"),
        ):
            mock_reg = MagicMock()
            mock_reg.get_nzb_client.return_value = mock_client
            mock_reg_cls.return_value = mock_reg
            await SeriesService.delete(session, series.id, delete_files=True, delete_folder=True)
            await session.flush()

        mock_client.remove_download.assert_called_once()
        assert not folder.exists()
        assert await session.get(Series, series.id) is None

    @pytest.mark.asyncio
    async def test_mixed_download_states(self, session: AsyncSession) -> None:
        """Mix of active and terminal downloads — only active ones cancelled."""
        series = await _seed_series(session)
        issue1 = await _seed_issue(session, series.id, issue_number=1.0)
        issue2 = await _seed_issue(session, series.id, issue_number=2.0)
        issue3 = await _seed_issue(session, series.id, issue_number=3.0)

        await _seed_download(
            session, issue1.id, state=DownloadState.DOWNLOADING, external_id="active_1"
        )
        await _seed_download(
            session, issue2.id, state=DownloadState.COMPLETED, external_id="done_1"
        )
        await _seed_download(session, issue3.id, state=DownloadState.FAILED, external_id="fail_1")

        mock_client = AsyncMock()
        mock_client.remove_download = AsyncMock(return_value=True)

        with (
            patch(REGISTER_DOWNLOAD_CLIENTS_PATH, new_callable=AsyncMock),
            patch("pullbox.providers.base.ProviderRegistry") as mock_reg_cls,
            patch("pullbox.tasks.download_task._clear_progress"),
        ):
            mock_reg = MagicMock()
            mock_reg.get_nzb_client.return_value = mock_client
            mock_reg_cls.return_value = mock_reg
            await SeriesService.delete(session, series.id)
            await session.flush()

        # Only the DOWNLOADING one should trigger client cancel
        mock_client.remove_download.assert_called_once_with("active_1", delete_files=True)
