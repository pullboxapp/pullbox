"""Unit tests for comics_directory setting — Task R-1.1."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.models.config import SystemConfig
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series
from pullbox.services.library_service import (
    get_comics_directory,
    reconcile_runtime_library_paths,
    set_comics_directory,
)


class TestGetComicsDirectory:
    """Test retrieving the comics_directory setting."""

    @pytest.mark.asyncio
    async def test_returns_none_when_not_set(self, db_session: AsyncSession) -> None:
        result = await get_comics_directory(db_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_path_when_set(self, db_session: AsyncSession, tmp_path: Path) -> None:
        comics_dir = tmp_path / "comics"
        comics_dir.mkdir()
        db_session.add(
            SystemConfig(key="comics_directory", value=str(comics_dir), value_type="string")
        )
        await db_session.flush()

        result = await get_comics_directory(db_session)
        assert result == comics_dir


class TestSetComicsDirectory:
    """Test setting the comics_directory with validation and LibraryRoot auto-creation."""

    @pytest.mark.asyncio
    async def test_rejects_nonexistent_path(self, db_session: AsyncSession) -> None:
        fake_path = Path("/nonexistent/comics/path")
        with pytest.raises(ValueError, match="does not exist"):
            await set_comics_directory(db_session, fake_path)

    @pytest.mark.asyncio
    async def test_rejects_non_directory_path(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        file_path = tmp_path / "not_a_dir.txt"
        file_path.write_text("hello")
        with pytest.raises(ValueError, match="not a directory"):
            await set_comics_directory(db_session, file_path)

    @pytest.mark.asyncio
    async def test_stores_setting_in_system_config(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        comics_dir = tmp_path / "comics"
        comics_dir.mkdir()

        await set_comics_directory(db_session, comics_dir)

        row = await db_session.get(SystemConfig, "comics_directory")
        assert row is not None
        assert row.value == str(comics_dir)
        assert row.value_type == "string"

    @pytest.mark.asyncio
    async def test_stores_resolved_directory_path(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        comics_dir = tmp_path / "comics"
        comics_dir.mkdir()
        spelled_with_parent = comics_dir / ".." / "comics"

        root = await set_comics_directory(db_session, spelled_with_parent)

        row = await db_session.get(SystemConfig, "comics_directory")
        assert row is not None
        assert row.value == str(comics_dir.resolve())
        assert root.path == str(comics_dir.resolve())

    @pytest.mark.asyncio
    async def test_creates_library_root_for_new_path(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        comics_dir = tmp_path / "comics"
        comics_dir.mkdir()

        root = await set_comics_directory(db_session, comics_dir)

        assert root.path == str(comics_dir)
        assert root.name == "Comics Directory"
        assert root.enabled is True

        # Verify it's in the DB
        result = await db_session.execute(
            select(LibraryRoot).where(LibraryRoot.path == str(comics_dir))
        )
        assert result.scalar_one_or_none() is not None

    @pytest.mark.asyncio
    async def test_reuses_existing_library_root(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        comics_dir = tmp_path / "comics"
        comics_dir.mkdir()

        existing_root = LibraryRoot(name="Old Name", path=str(comics_dir), enabled=False)
        db_session.add(existing_root)
        await db_session.flush()
        root_id = existing_root.id

        root = await set_comics_directory(db_session, comics_dir)

        assert root.id == root_id
        assert root.name == "Comics Directory"
        assert root.enabled is True

    @pytest.mark.asyncio
    async def test_changing_directory_updates_config(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        dir_a = tmp_path / "comics_a"
        dir_a.mkdir()
        dir_b = tmp_path / "comics_b"
        dir_b.mkdir()

        await set_comics_directory(db_session, dir_a)
        await set_comics_directory(db_session, dir_b)

        row = await db_session.get(SystemConfig, "comics_directory")
        assert row is not None
        assert row.value == str(dir_b)

    @pytest.mark.asyncio
    async def test_changing_directory_creates_new_root(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        dir_a = tmp_path / "comics_a"
        dir_a.mkdir()
        dir_b = tmp_path / "comics_b"
        dir_b.mkdir()

        root_a = await set_comics_directory(db_session, dir_a)
        root_b = await set_comics_directory(db_session, dir_b)

        # Both roots exist (old one remains for tracking)
        assert root_a.id != root_b.id
        assert root_b.path == str(dir_b)

    @pytest.mark.asyncio
    async def test_promoting_an_existing_root_preserves_its_unique_name(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        primary_path = tmp_path / "primary"
        archive_path = tmp_path / "archive"
        primary_path.mkdir()
        archive_path.mkdir()
        await set_comics_directory(db_session, primary_path)
        archive = LibraryRoot(
            name="Archive",
            path=str(archive_path),
            enabled=True,
            allow_referenced_registrations=True,
            allow_managed_writes=True,
        )
        db_session.add(archive)
        await db_session.flush()

        promoted = await set_comics_directory(db_session, archive_path)

        assert promoted.id == archive.id
        assert promoted.name == "Archive"
        assert promoted.is_default_managed_destination is True
        row = await db_session.get(SystemConfig, "comics_directory")
        assert row is not None
        assert row.value == str(archive_path)

    @pytest.mark.asyncio
    async def test_returns_library_root(self, db_session: AsyncSession, tmp_path: Path) -> None:
        comics_dir = tmp_path / "comics"
        comics_dir.mkdir()

        result = await set_comics_directory(db_session, comics_dir)
        assert isinstance(result, LibraryRoot)


class TestReconcileRuntimeLibraryPaths:
    """Test safe runtime-root bootstrap without implicit path rebinding."""

    @pytest.mark.asyncio
    async def test_seeds_runtime_root_for_fresh_install(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        comics_dir = (tmp_path / "comics").resolve()
        comics_dir.mkdir(parents=True)

        result = await reconcile_runtime_library_paths(db_session, comics_dir)

        assert result is not None
        assert result["status"] == "bootstrapped"
        assert result["old_root"] == ""
        assert result["new_root"] == str(comics_dir)
        assert result["series_updated"] == 0
        assert result["library_files_updated"] == 0

        comics_directory = await db_session.get(SystemConfig, "comics_directory")
        assert comics_directory is not None
        assert comics_directory.value == str(comics_dir)

        root = (
            await db_session.execute(select(LibraryRoot).where(LibraryRoot.path == str(comics_dir)))
        ).scalar_one()
        assert root.name == "Comics Directory"
        assert root.enabled is True

    @pytest.mark.asyncio
    async def test_established_runtime_mismatch_requires_rebind_without_mutation(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        old_root_path = (tmp_path / "old-runtime" / "comics").resolve()
        new_root_path = (tmp_path / "docker-runtime" / "comics").resolve()
        old_root_path.mkdir(parents=True)
        new_root_path.mkdir(parents=True)

        old_root = LibraryRoot(name="Comics", path=str(old_root_path), enabled=True)
        db_session.add(old_root)
        db_session.add(
            SystemConfig(key="comics_directory", value=str(old_root_path), value_type="string")
        )
        await db_session.flush()

        series = Series(
            title="Absolute Batman",
            sort_title="absolute batman",
            path=str(old_root_path / "Absolute Batman (2024) [160294]"),
            library_root_id=old_root.id,
        )
        db_session.add(series)
        await db_session.flush()

        library_file = LibraryFile(
            file_path=str(
                old_root_path / "Absolute Batman (2024) [160294]" / "Absolute Batman 001.cbz"
            ),
            file_name="Absolute Batman 001.cbz",
            file_size=1024,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
            library_root_id=old_root.id,
        )
        db_session.add(library_file)
        await db_session.flush()

        result = await reconcile_runtime_library_paths(db_session, new_root_path)

        assert result is not None
        assert result["status"] == "rebind_required"
        assert result["old_root"] == str(old_root_path)
        assert result["new_root"] == str(new_root_path)
        assert result["series_updated"] == 0
        assert result["library_files_updated"] == 0

        comics_directory = await db_session.get(SystemConfig, "comics_directory")
        assert comics_directory is not None
        assert comics_directory.value == str(old_root_path)
        assert old_root.path == str(old_root_path)
        assert series.path == str(old_root_path / "Absolute Batman (2024) [160294]")
        assert library_file.file_path == str(
            old_root_path / "Absolute Batman (2024) [160294]" / "Absolute Batman 001.cbz"
        )
        assert library_file.file_name == "Absolute Batman 001.cbz"
        assert (
            await db_session.scalar(
                select(LibraryRoot.id).where(LibraryRoot.path == str(new_root_path))
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_noops_when_runtime_root_already_matches(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        comics_dir = (tmp_path / "comics").resolve()
        comics_dir.mkdir(parents=True)
        db_session.add(
            SystemConfig(key="comics_directory", value=str(comics_dir), value_type="string")
        )
        db_session.add(LibraryRoot(name="Comics Directory", path=str(comics_dir), enabled=True))
        await db_session.flush()

        result = await reconcile_runtime_library_paths(db_session, comics_dir)

        assert result is None
