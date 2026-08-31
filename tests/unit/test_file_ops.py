"""Tests for Task R-2.1 — register_library_file() core utility.

Verifies file registration, move/rename logic, duplicate detection,
error handling, and Issue status updates.
"""

from __future__ import annotations

import asyncio
import os
import stat
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.core.exceptions import ConfigurationError
from pullbox.models import Base
from pullbox.models.config import SystemConfig
from pullbox.models.download import DownloadClientType
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import (
    FileFormat,
    LibraryFile,
    LibraryFileStorageMode,
    LibraryRoot,
    LibraryRootPolicy,
    LibraryRootPolicySource,
    MatchConfidence,
)
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-file-ops")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


async def _enable_library_permissions(
    session: AsyncSession,
    *,
    file_mode: str = "640",
    folder_mode: str = "750",
    apply_to_created_folders: str = "true",
    apply_to_materialized_files: str = "true",
) -> None:
    session.add_all(
        [
            SystemConfig(
                key="library_permissions_enabled",
                value="true",
                value_type="bool",
            ),
            SystemConfig(
                key="library_permissions_file_mode",
                value=file_mode,
                value_type="string",
            ),
            SystemConfig(
                key="library_permissions_folder_mode",
                value=folder_mode,
                value_type="string",
            ),
            SystemConfig(
                key="library_permissions_apply_to_created_folders",
                value=apply_to_created_folders,
                value_type="bool",
            ),
            SystemConfig(
                key="library_permissions_apply_to_materialized_files",
                value=apply_to_materialized_files,
                value_type="bool",
            ),
        ]
    )
    await session.flush()


@pytest.fixture
async def db() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def session(db: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncSession, None]:
    async with db() as s:
        yield s


@pytest.fixture
async def library_root(session: AsyncSession) -> LibraryRoot:
    root = LibraryRoot(name="Comics", path="/tmp/test-comics", enabled=True)
    session.add(root)
    await session.flush()
    return root


@pytest.fixture
async def comics_dir_config(session: AsyncSession, tmp_path: Path) -> Path:
    """Set up comics_directory config and LibraryRoot pointing to tmp_path."""
    comics_dir = tmp_path / "comics"
    comics_dir.mkdir()
    config = SystemConfig(key="comics_directory", value=str(comics_dir), value_type="string")
    session.add(config)
    root = LibraryRoot(name="Comics", path=str(comics_dir), enabled=True)
    session.add(root)
    await session.flush()
    return comics_dir


@pytest.fixture
async def publisher(session: AsyncSession) -> Publisher:
    pub = Publisher(name="DC Comics")
    session.add(pub)
    await session.flush()
    return pub


@pytest.fixture
async def series(session: AsyncSession, publisher: Publisher, comics_dir_config: Path) -> Series:
    # Get the library root
    result = await session.execute(select(LibraryRoot).limit(1))
    root = result.scalars().first()
    s = Series(
        title="Batman",
        sort_title="Batman",
        year_start=2024,
        publisher_id=publisher.id,
        library_root_id=root.id if root else None,
    )
    session.add(s)
    await session.flush()
    return s


@pytest.fixture
async def issue(session: AsyncSession, series: Series) -> Issue:
    iss = Issue(
        series_id=series.id,
        issue_number=17.0,
        title="The Brave and the Bold",
        status=IssueStatus.WANTED,
        issue_type=IssueType.ISSUE,
    )
    session.add(iss)
    await session.flush()
    return iss


@pytest.fixture
def source_file(tmp_path: Path) -> Path:
    """Create a fake .cbz source file."""
    src = tmp_path / "downloads" / "Batman 017 (2024).cbz"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"PK" + b"\x00" * 100)  # minimal zip-like content
    return src


class TestMoveToLibrary:
    """File is moved to comics directory under correct series folder."""

    @pytest.mark.asyncio
    async def test_move_to_library(
        self, session: AsyncSession, issue: Issue, source_file: Path, comics_dir_config: Path
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        lf = await register_library_file(
            session, source_file, issue, MatchConfidence.HIGH, move_to_library=True
        )

        assert lf is not None
        assert lf.issue_id == issue.id
        assert lf.match_confidence == MatchConfidence.HIGH
        # File should no longer be at source
        assert not source_file.exists()
        # File should be inside comics directory
        assert Path(lf.file_path).exists()
        assert str(comics_dir_config) in lf.file_path

    @pytest.mark.asyncio
    async def test_move_with_rename(
        self, session: AsyncSession, issue: Issue, source_file: Path, comics_dir_config: Path
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        # Enable rename
        session.add(SystemConfig(key="rename_on_import", value="true", value_type="bool"))
        await session.flush()

        lf = await register_library_file(
            session, source_file, issue, MatchConfidence.HIGH, move_to_library=True, rename=True
        )

        # File should be renamed per naming template (not keep original name)
        assert lf is not None
        assert Path(lf.file_path).exists()
        # Original filename was "Batman 017 (2024).cbz"
        # Renamed should follow template pattern
        assert lf.file_name.endswith(".cbz")

    @pytest.mark.asyncio
    async def test_move_without_rename(
        self, session: AsyncSession, issue: Issue, source_file: Path, comics_dir_config: Path
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        lf = await register_library_file(
            session, source_file, issue, MatchConfidence.HIGH, move_to_library=True, rename=False
        )

        # File keeps original name but moves to series folder
        assert lf is not None
        assert Path(lf.file_path).exists()
        assert lf.file_name == "Batman 017 (2024).cbz"
        assert str(comics_dir_config) in lf.file_path

    @pytest.mark.asyncio
    async def test_move_to_library_with_identity_mapped_issue_uses_eager_reload(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        session.expire(issue, ["series"])

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
        )

        assert lf is not None
        assert Path(lf.file_path).exists()
        assert str(comics_dir_config) in lf.file_path


class TestTransferCompatibility:
    """Current transfer-method contracts before seed-safe planning is introduced."""

    @pytest.mark.asyncio
    async def test_move_applies_enabled_library_file_permissions(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        source_file.chmod(0o600)
        await _enable_library_permissions(
            session,
            file_mode="640",
            apply_to_created_folders="false",
        )

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
            transfer_method="move",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=False,
        )

        library_path = Path(lf.file_path)
        assert _mode(library_path) == 0o640

    @pytest.mark.asyncio
    async def test_copy_applies_enabled_library_file_permissions(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        source_file.chmod(0o600)
        await _enable_library_permissions(
            session,
            file_mode="640",
            apply_to_created_folders="false",
        )

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
            transfer_method="copy",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=False,
        )

        library_path = Path(lf.file_path)
        assert source_file.exists()
        assert _mode(source_file) == 0o600
        assert _mode(library_path) == 0o640

    @pytest.mark.asyncio
    async def test_converted_artifact_gets_enabled_library_file_permissions(
        self,
        session: AsyncSession,
        issue: Issue,
        tmp_path: Path,
        comics_dir_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        source_archive = tmp_path / "downloads" / "Batman 017 (2024).cbr"
        source_archive.parent.mkdir(parents=True, exist_ok=True)
        source_archive.write_bytes(b"Rar!" + b"\x00" * 100)
        source_archive.chmod(0o600)
        await _enable_library_permissions(
            session,
            file_mode="640",
            apply_to_created_folders="false",
        )

        async def fake_convert_file(
            source_path: Path,
            target_format: str,
            destination: Path,
        ) -> Path:
            assert source_path == source_archive
            assert target_format == "cbz"
            converted_path = destination / f"{source_path.stem}.cbz"
            converted_path.write_bytes(b"PK" + b"\x00" * 100)
            converted_path.chmod(0o600)
            return converted_path

        monkeypatch.setattr("pullbox.core.file_ops.convert_file", fake_convert_file)

        lf = await register_library_file(
            session,
            source_archive,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
            transfer_method="move",
            normalize_to_cbz=True,
            update_embedded_comicinfo_from_match=False,
        )

        library_path = Path(lf.file_path)
        assert not source_archive.exists()
        assert library_path.suffix == ".cbz"
        assert _mode(library_path) == 0o640

    @pytest.mark.asyncio
    async def test_comicinfo_mutated_artifact_gets_enabled_library_file_permissions(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        source_file.chmod(0o600)
        await _enable_library_permissions(
            session,
            file_mode="640",
            apply_to_created_folders="false",
        )

        def fake_apply_comicinfo(artifact_path: Path, _payload: dict[str, object]) -> None:
            assert artifact_path.exists()
            artifact_path.chmod(0o600)

        monkeypatch.setattr(
            "pullbox.core.file_ops._apply_comicinfo_to_imported_artifact",
            fake_apply_comicinfo,
        )

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
            transfer_method="move",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=True,
            comicinfo_payload={"Series": "Batman", "Number": "17"},
        )

        assert _mode(Path(lf.file_path)) == 0o640

    @pytest.mark.asyncio
    async def test_new_imported_file_runs_embedded_comicinfo_update(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        seen: dict[str, object] = {}

        def fake_apply_comicinfo(
            artifact_path: Path,
            payload: dict[str, object],
            *,
            progress_callback=None,
        ) -> None:
            seen["path"] = artifact_path
            seen["payload"] = payload
            seen["progress_callback"] = progress_callback

        monkeypatch.setattr(
            "pullbox.core.file_ops._apply_comicinfo_to_imported_artifact",
            fake_apply_comicinfo,
        )

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
            transfer_method="move",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=True,
            comicinfo_payload={"Series": "Batman", "Number": "17"},
        )

        assert lf is not None
        assert seen["path"] == Path(lf.file_path)
        assert seen["payload"] == {"Series": "Batman", "Number": "17"}
        assert seen["progress_callback"] is None

    @pytest.mark.asyncio
    async def test_cbz_import_can_materialize_with_comicinfo_in_one_pass(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        seen: dict[str, object] = {}

        async def fail_transfer(*_args: object, **_kwargs: object) -> Path:
            raise AssertionError("separate transfer should not run")

        def fail_embed(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("separate ComicInfo rewrite should not run")

        async def materialize_with_comicinfo(
            source_path: Path,
            target_path: Path,
            payload: dict[str, object],
            *,
            transfer_method: str,
            progress_callback=None,
        ) -> bool:
            seen["source_path"] = source_path
            seen["target_path"] = target_path
            seen["payload"] = payload
            seen["transfer_method"] = transfer_method
            seen["progress_callback"] = progress_callback
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(source_path.read_bytes())
            source_path.unlink()
            return True

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
            transfer_method="move",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=True,
            comicinfo_payload={"Series": "Batman", "Number": "17"},
            artifact_transfer=fail_transfer,
            comicinfo_embedder=fail_embed,
            comicinfo_materializer=materialize_with_comicinfo,
        )

        assert lf is not None
        assert seen["source_path"] == source_file
        assert seen["payload"] == {"Series": "Batman", "Number": "17"}
        assert seen["transfer_method"] == "move"
        assert Path(lf.file_path) == seen["target_path"]
        assert not source_file.exists()

    @pytest.mark.asyncio
    async def test_cbz_import_announces_placement_before_materialization(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        placement_events: list[dict[str, object]] = []

        async def placement_started(**kwargs: object) -> None:
            placement_events.append(dict(kwargs))

        async def materialize_with_comicinfo(
            _source_path: Path,
            target_path: Path,
            _payload: dict[str, object],
            *,
            transfer_method: str,
            progress_callback=None,
        ) -> bool:
            target_path.write_text("partial target", encoding="utf-8")
            raise RuntimeError("materialization failed after target planning")

        with pytest.raises(RuntimeError, match="materialization failed"):
            await register_library_file(
                session,
                source_file,
                issue,
                MatchConfidence.HIGH,
                move_to_library=True,
                rename=True,
                transfer_method="move",
                normalize_to_cbz=False,
                update_embedded_comicinfo_from_match=True,
                comicinfo_payload={"Series": "Batman", "Number": "17"},
                comicinfo_materializer=materialize_with_comicinfo,
                placement_started_callback=placement_started,
            )

        assert len(placement_events) == 1
        event = placement_events[0]
        assert event["artifact_source_path"] == source_file
        assert Path(str(event["target_path"])).parent == comics_dir_config / "Batman (2024)"
        assert event["transfer_method"] == "move"
        assert event["series_folder_created"] is True

    @pytest.mark.asyncio
    async def test_embedded_comicinfo_payload_uses_inspected_archive_page_count(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        issue.page_count = 99
        with zipfile.ZipFile(source_file, "w") as archive:
            archive.writestr("001.jpg", b"page one")
            archive.writestr("002.png", b"page two")
            archive.writestr("metadata/ComicInfo.xml", b"<ComicInfo />")
        await session.flush()

        seen: dict[str, object] = {}

        def fake_apply_comicinfo(
            artifact_path: Path,
            payload: dict[str, object],
            *,
            progress_callback=None,
        ) -> None:
            seen["path"] = artifact_path
            seen["payload"] = payload
            seen["progress_callback"] = progress_callback

        monkeypatch.setattr(
            "pullbox.core.file_ops._apply_comicinfo_to_imported_artifact",
            fake_apply_comicinfo,
        )

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
            transfer_method="move",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=True,
        )

        assert seen["path"] == Path(lf.file_path)
        assert seen["payload"]["PageCount"] == 2
        assert seen["progress_callback"] is None

    @pytest.mark.asyncio
    async def test_created_series_folder_gets_enabled_library_folder_permissions(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        await _enable_library_permissions(session, file_mode="640", folder_mode="750")

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
            transfer_method="move",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=False,
        )

        library_path = Path(lf.file_path)
        assert _mode(library_path.parent) == 0o750

    @pytest.mark.asyncio
    async def test_existing_series_folder_is_not_chmodded_by_import_policy(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import _build_series_folder_name, register_library_file
        from pullbox.core.library_policy import load_library_ingest_policy

        ingest_policy = await load_library_ingest_policy(session)
        existing_folder = comics_dir_config / _build_series_folder_name(issue.series, ingest_policy)
        existing_folder.mkdir(parents=True)
        existing_folder.chmod(0o700)
        await _enable_library_permissions(session, file_mode="640", folder_mode="750")

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
            transfer_method="move",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=False,
        )

        assert Path(lf.file_path).parent == existing_folder
        assert _mode(existing_folder) == 0o700

    @pytest.mark.asyncio
    async def test_created_folder_permission_setting_can_be_disabled(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        await _enable_library_permissions(
            session,
            file_mode="640",
            folder_mode="750",
            apply_to_created_folders="false",
        )

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
            transfer_method="move",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=False,
        )

        library_path = Path(lf.file_path)
        assert _mode(library_path.parent) != 0o750

    @pytest.mark.asyncio
    async def test_materialized_file_permission_setting_can_be_disabled(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        source_file.chmod(0o600)
        await _enable_library_permissions(
            session,
            file_mode="640",
            folder_mode="750",
            apply_to_materialized_files="false",
        )

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
            transfer_method="move",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=False,
        )

        library_path = Path(lf.file_path)
        assert _mode(library_path) == 0o600

    @pytest.mark.asyncio
    async def test_hardlink_path_only_register_preserves_source_inode(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        original_stat = source_file.stat()

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
            transfer_method="hardlink",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=False,
        )

        library_path = Path(lf.file_path)
        assert source_file.exists()
        assert library_path.exists()
        assert source_file.stat().st_ino == library_path.stat().st_ino
        assert source_file.stat().st_dev == library_path.stat().st_dev
        assert source_file.read_bytes() == library_path.read_bytes()
        assert source_file.stat().st_mtime_ns == original_stat.st_mtime_ns

    @pytest.mark.asyncio
    async def test_hardlink_skips_enabled_library_file_permissions_by_default(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        source_file.chmod(0o600)
        await _enable_library_permissions(
            session,
            file_mode="640",
            apply_to_created_folders="false",
        )

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
            transfer_method="hardlink",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=False,
        )

        library_path = Path(lf.file_path)
        assert source_file.stat().st_ino == library_path.stat().st_ino
        assert _mode(source_file) == 0o600
        assert _mode(library_path) == 0o600

    @pytest.mark.asyncio
    async def test_symlink_skips_enabled_library_file_permissions_by_default(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        source_file.chmod(0o600)
        await _enable_library_permissions(session, file_mode="640")

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
            transfer_method="symlink",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=False,
        )

        library_path = Path(lf.file_path)
        assert library_path.is_symlink()
        assert _mode(source_file) == 0o600

    @pytest.mark.asyncio
    async def test_permission_failure_does_not_fail_library_registration(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core.file_ops import register_library_file
        from pullbox.core.library_permission_engine import (
            PermissionAction,
            PermissionChangeResult,
            PermissionReason,
            PermissionTargetKind,
        )

        await _enable_library_permissions(
            session,
            file_mode="640",
            apply_to_created_folders="false",
        )
        calls: list[Path] = []

        def fake_apply_permission_change(
            path: Path,
            requested_mode: int,
            *,
            dry_run: bool,
            skip_hardlinks: bool = True,
            skip_symlinks: bool = True,
        ) -> PermissionChangeResult:
            del dry_run, skip_hardlinks, skip_symlinks
            calls.append(path)
            return PermissionChangeResult(
                path=path,
                target_kind=PermissionTargetKind.FILE,
                action=PermissionAction.FAILED,
                reason=PermissionReason.PERMISSION_DENIED,
                requested_mode=requested_mode,
                previous_mode=0o600,
                resulting_mode=0o600,
                error_message="operation not permitted",
            )

        monkeypatch.setattr(
            "pullbox.core.library_permission_application.apply_permission_change",
            fake_apply_permission_change,
        )

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
            transfer_method="move",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=False,
        )

        library_path = Path(lf.file_path)
        assert library_path.exists()
        assert calls == [library_path]

    @pytest.mark.asyncio
    async def test_seed_safe_torrent_path_only_uses_hardlink_and_preserves_source(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        session.add(
            SystemConfig(
                key="torrent_import_strategy",
                value="seed_safe",
                value_type="string",
            )
        )
        await session.flush()

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
            transfer_method="move",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=False,
            download_client=DownloadClientType.QBITTORRENT,
        )

        library_path = Path(lf.file_path)
        assert source_file.exists()
        assert library_path.exists()
        assert source_file.stat().st_ino == library_path.stat().st_ino

    @pytest.mark.asyncio
    async def test_seed_safe_torrent_content_mutation_copies_and_preserves_source(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        session.add(
            SystemConfig(
                key="torrent_import_strategy",
                value="seed_safe",
                value_type="string",
            )
        )
        await session.flush()
        monkeypatch.setattr(
            "pullbox.core.file_ops._apply_comicinfo_to_imported_artifact",
            lambda _path, _payload: None,
        )

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
            transfer_method="hardlink",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=True,
            download_client=DownloadClientType.QBITTORRENT,
        )

        library_path = Path(lf.file_path)
        assert source_file.exists()
        assert library_path.exists()
        assert source_file.stat().st_ino != library_path.stat().st_ino
        assert source_file.read_bytes() == library_path.read_bytes()

    @pytest.mark.asyncio
    async def test_seed_safe_torrent_content_mutation_permissions_do_not_touch_source(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        source_file.chmod(0o600)
        session.add(
            SystemConfig(
                key="torrent_import_strategy",
                value="seed_safe",
                value_type="string",
            )
        )
        await _enable_library_permissions(
            session,
            file_mode="640",
            apply_to_created_folders="false",
        )
        await session.flush()

        def fake_apply_comicinfo(artifact_path: Path, _payload: dict[str, object]) -> None:
            assert artifact_path.exists()
            artifact_path.chmod(0o600)

        monkeypatch.setattr(
            "pullbox.core.file_ops._apply_comicinfo_to_imported_artifact",
            fake_apply_comicinfo,
        )

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
            transfer_method="hardlink",
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=True,
            comicinfo_payload={"Series": "Batman", "Number": "17"},
            download_client=DownloadClientType.QBITTORRENT,
        )

        library_path = Path(lf.file_path)
        assert source_file.exists()
        assert library_path.exists()
        assert source_file.stat().st_ino != library_path.stat().st_ino
        assert _mode(source_file) == 0o600
        assert _mode(library_path) == 0o640

    @pytest.mark.asyncio
    async def test_hardlink_rejected_when_archive_normalization_requested(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        with pytest.raises(ConfigurationError, match="Archive normalization requires"):
            await register_library_file(
                session,
                source_file,
                issue,
                MatchConfidence.HIGH,
                move_to_library=True,
                transfer_method="hardlink",
                normalize_to_cbz=True,
                update_embedded_comicinfo_from_match=False,
            )

        assert source_file.exists()

    @pytest.mark.asyncio
    async def test_hardlink_rejected_when_embedded_comicinfo_update_requested(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        with pytest.raises(ConfigurationError, match="Move or Copy"):
            await register_library_file(
                session,
                source_file,
                issue,
                MatchConfidence.HIGH,
                move_to_library=True,
                transfer_method="hardlink",
                normalize_to_cbz=False,
                update_embedded_comicinfo_from_match=True,
            )

        assert source_file.exists()


class TestLeaveInPlace:
    """File stays where it is, LibraryFile points to original path."""

    @pytest.mark.asyncio
    async def test_leave_in_place(
        self, session: AsyncSession, issue: Issue, source_file: Path, comics_dir_config: Path
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        with pytest.raises(ConfigurationError, match="inside an enabled library root"):
            await register_library_file(
                session,
                source_file,
                issue,
                MatchConfidence.HIGH,
                move_to_library=False,
            )

        # Rejected referenced registration never mutates the source.
        assert source_file.exists()

    @pytest.mark.asyncio
    async def test_leave_in_place_source_inside_comics_dir(
        self, session: AsyncSession, issue: Issue, comics_dir_config: Path
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        # Create source file inside comics directory
        src = comics_dir_config / "existing" / "Batman 017.cbz"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"PK" + b"\x00" * 100)

        lf = await register_library_file(
            session, src, issue, MatchConfidence.HIGH, move_to_library=False, rename=False
        )

        # No move, just register
        assert src.exists()
        assert lf.file_path == str(src)
        assert lf.storage_mode == LibraryFileStorageMode.REFERENCED
        assert lf.source_signature == {
            "schema_version": 1,
            "resolved_path": str(src.resolve()),
            "size": src.stat().st_size,
            "mtime_ns": src.stat().st_mtime_ns,
            "device": src.stat().st_dev,
            "inode": src.stat().st_ino,
        }

    @pytest.mark.asyncio
    async def test_leave_in_place_rejects_source_mutation_options(
        self,
        session: AsyncSession,
        issue: Issue,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        src = comics_dir_config / "existing" / "loose-name.cbz"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"PK" + b"\x00" * 100)

        with pytest.raises(ConfigurationError, match="cannot rename"):
            await register_library_file(
                session,
                src,
                issue,
                MatchConfidence.HIGH,
                move_to_library=False,
                rename=True,
            )

        assert src.exists()
        assert src.name == "loose-name.cbz"

    @pytest.mark.asyncio
    async def test_leave_in_place_rejects_source_changed_since_scan(
        self,
        session: AsyncSession,
        issue: Issue,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file
        from pullbox.core.library_file_ownership import (
            ReferencedFileValidationError,
            build_file_identity_signature,
        )

        src = comics_dir_config / "existing" / "Batman 017.cbz"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"original comic")
        scan_signature = build_file_identity_signature(src)
        src.write_bytes(b"replacement comic")

        with pytest.raises(ReferencedFileValidationError) as exc_info:
            await register_library_file(
                session,
                src,
                issue,
                MatchConfidence.HIGH,
                move_to_library=False,
                rename=False,
                expected_source_signature=scan_signature,
            )

        assert exc_info.value.reason == "source_changed"
        assert src.read_bytes() == b"replacement comic"


class TestLibraryFileRecord:
    """LibraryFile record is created with correct fields."""

    @pytest.mark.asyncio
    async def test_record_fields(
        self, session: AsyncSession, issue: Issue, source_file: Path, comics_dir_config: Path
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        lf = await register_library_file(
            session, source_file, issue, MatchConfidence.HIGH, move_to_library=True
        )

        assert lf.file_format == FileFormat.CBZ
        assert lf.file_size > 0
        assert lf.issue_id == issue.id
        assert lf.match_confidence == MatchConfidence.HIGH
        assert lf.storage_mode == LibraryFileStorageMode.MANAGED
        assert lf.library_root_id is not None


class TestIssueStatusUpdate:
    """Issue.status is updated to OWNED."""

    @pytest.mark.asyncio
    async def test_issue_marked_owned(
        self, session: AsyncSession, issue: Issue, source_file: Path, comics_dir_config: Path
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        assert issue.status == IssueStatus.WANTED

        await register_library_file(
            session, source_file, issue, MatchConfidence.HIGH, move_to_library=True
        )

        await session.refresh(issue)
        assert issue.status == IssueStatus.OWNED


class TestDuplicateDetection:
    """If LibraryFile already exists for that path, return existing record."""

    @pytest.mark.asyncio
    async def test_duplicate_returns_existing(
        self, session: AsyncSession, issue: Issue, comics_dir_config: Path
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        # Create source file inside comics dir (leave in place, no rename)
        src = comics_dir_config / "existing" / "Batman 017.cbz"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"PK" + b"\x00" * 100)

        lf1 = await register_library_file(
            session, src, issue, MatchConfidence.HIGH, move_to_library=False, rename=False
        )

        lf2 = await register_library_file(
            session, src, issue, MatchConfidence.HIGH, move_to_library=False, rename=False
        )

        assert lf1.id == lf2.id

    @pytest.mark.asyncio
    async def test_same_referenced_path_rejects_different_issue(
        self,
        session: AsyncSession,
        issue: Issue,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        src = comics_dir_config / "existing" / "Batman 017.cbz"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"PK" + b"\x00" * 100)
        other_issue = Issue(
            series_id=issue.series_id,
            issue_number=18.0,
            status=IssueStatus.WANTED,
            issue_type=IssueType.ISSUE,
        )
        session.add(other_issue)
        await session.flush()
        await register_library_file(
            session,
            src,
            issue,
            MatchConfidence.HIGH,
            move_to_library=False,
            rename=False,
        )

        with pytest.raises(ConfigurationError, match="different issue"):
            await register_library_file(
                session,
                src,
                other_issue,
                MatchConfidence.HIGH,
                move_to_library=False,
                rename=False,
            )

        assert src.exists()

    @pytest.mark.asyncio
    async def test_existing_file_ownership_cannot_flip_during_registration(
        self,
        session: AsyncSession,
        issue: Issue,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        src = comics_dir_config / "existing" / "Batman 017.cbz"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"PK" + b"\x00" * 100)
        existing = await register_library_file(
            session,
            src,
            issue,
            MatchConfidence.HIGH,
            move_to_library=False,
            rename=False,
        )
        existing.storage_mode = LibraryFileStorageMode.MANAGED
        await session.flush()

        with pytest.raises(ConfigurationError, match="ownership cannot be changed"):
            await register_library_file(
                session,
                src,
                issue,
                MatchConfidence.HIGH,
                move_to_library=False,
                rename=False,
            )

        assert src.exists()


class TestReplacementRegistration:
    """Explicit replacement refreshes the existing library file row."""

    @pytest.mark.asyncio
    async def test_replacement_same_path_updates_existing_metadata(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        result = await session.execute(select(LibraryRoot).limit(1))
        root = result.scalars().first()
        assert root is not None

        final_path = comics_dir_config / "Batman (2024)" / "Batman (2024) #017.cbz"
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_bytes(b"old")
        old_size = final_path.stat().st_size

        existing = LibraryFile(
            file_path=str(final_path),
            file_name=final_path.name,
            file_size=old_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.fromtimestamp(final_path.stat().st_mtime, tz=UTC),
            match_confidence=MatchConfidence.HIGH,
            issue_id=issue.id,
            library_root_id=root.id,
        )
        session.add(existing)
        await session.flush()

        replacement_size = source_file.stat().st_size
        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.MANUAL,
            move_to_library=True,
            library_root_id=root.id,
            loaded_issue=issue,
            replace_existing_library_file=True,
            replacement_trash_dir=None,
        )

        assert lf.id == existing.id
        assert final_path.exists()
        assert final_path.stat().st_size == replacement_size
        assert lf.file_size == replacement_size
        assert lf.file_size != old_size
        assert lf.match_confidence == MatchConfidence.MANUAL

    @pytest.mark.asyncio
    async def test_replacement_staged_original_is_removed_only_after_commit(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        result = await session.execute(select(LibraryRoot).limit(1))
        root = result.scalars().first()
        assert root is not None

        final_path = comics_dir_config / "Batman (2024)" / "Batman (2024) #017.cbz"
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_bytes(b"old")
        existing = LibraryFile(
            file_path=str(final_path),
            file_name=final_path.name,
            file_size=final_path.stat().st_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.fromtimestamp(final_path.stat().st_mtime, tz=UTC),
            match_confidence=MatchConfidence.HIGH,
            issue_id=issue.id,
            library_root_id=root.id,
        )
        session.add(existing)
        await session.commit()
        replacement_bytes = source_file.read_bytes()

        await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.MANUAL,
            move_to_library=True,
            library_root_id=root.id,
            loaded_issue=issue,
            replace_existing_library_file=True,
            replacement_trash_dir=None,
        )

        staged_paths = list(final_path.parent.glob(f".{final_path.name}.pullbox-replace*"))
        assert len(staged_paths) == 1
        assert staged_paths[0].read_bytes() == b"old"
        assert final_path.read_bytes() == replacement_bytes

        await session.commit()

        assert not staged_paths[0].exists()
        assert final_path.read_bytes() == replacement_bytes

    @pytest.mark.asyncio
    async def test_replacement_rollback_restores_staged_original_after_registration(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        result = await session.execute(select(LibraryRoot).limit(1))
        root = result.scalars().first()
        assert root is not None

        final_path = comics_dir_config / "Batman (2024)" / "Batman (2024) #017.cbz"
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_bytes(b"old")
        existing = LibraryFile(
            file_path=str(final_path),
            file_name=final_path.name,
            file_size=final_path.stat().st_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.fromtimestamp(final_path.stat().st_mtime, tz=UTC),
            match_confidence=MatchConfidence.HIGH,
            issue_id=issue.id,
            library_root_id=root.id,
        )
        session.add(existing)
        await session.commit()
        replacement_bytes = source_file.read_bytes()

        await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.MANUAL,
            move_to_library=True,
            library_root_id=root.id,
            loaded_issue=issue,
            replace_existing_library_file=True,
            replacement_trash_dir=None,
        )

        staged_paths = list(final_path.parent.glob(f".{final_path.name}.pullbox-replace*"))
        assert len(staged_paths) == 1
        assert final_path.read_bytes() == replacement_bytes

        await session.rollback()

        assert final_path.read_bytes() == b"old"
        assert not staged_paths[0].exists()

    @pytest.mark.asyncio
    async def test_replacement_cancellation_restores_staged_original(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        result = await session.execute(select(LibraryRoot).limit(1))
        root = result.scalars().first()
        assert root is not None

        final_path = comics_dir_config / "Batman (2024)" / "Batman (2024) #017.cbz"
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_bytes(b"old")
        original_size = final_path.stat().st_size

        existing = LibraryFile(
            file_path=str(final_path),
            file_name=final_path.name,
            file_size=original_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.fromtimestamp(final_path.stat().st_mtime, tz=UTC),
            match_confidence=MatchConfidence.HIGH,
            issue_id=issue.id,
            library_root_id=root.id,
        )
        session.add(existing)
        await session.flush()

        async def cancelling_transfer(*_args: object, **_kwargs: object) -> None:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await register_library_file(
                session,
                source_file,
                issue,
                MatchConfidence.MANUAL,
                move_to_library=True,
                library_root_id=root.id,
                loaded_issue=issue,
                replace_existing_library_file=True,
                replacement_trash_dir=None,
                artifact_transfer=cancelling_transfer,
            )

        assert final_path.exists()
        assert final_path.read_bytes() == b"old"
        assert final_path.stat().st_size == original_size
        assert not list(final_path.parent.glob(f".{final_path.name}.pullbox-replace*"))


class TestLibraryRootResolution:
    """library_root_id is correctly resolved."""

    @pytest.mark.asyncio
    async def test_explicit_root_id(
        self, session: AsyncSession, issue: Issue, source_file: Path, comics_dir_config: Path
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        result = await session.execute(select(LibraryRoot).limit(1))
        root = result.scalars().first()
        assert root is not None

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            library_root_id=root.id,
        )

        assert lf.library_root_id == root.id

    @pytest.mark.asyncio
    async def test_detected_from_path(
        self, session: AsyncSession, issue: Issue, comics_dir_config: Path
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        # Source inside comics dir — should detect root
        src = comics_dir_config / "some_folder" / "test.cbz"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"PK" + b"\x00" * 100)

        lf = await register_library_file(
            session, src, issue, MatchConfidence.HIGH, move_to_library=False
        )

        result = await session.execute(
            select(LibraryRoot).where(LibraryRoot.path == str(comics_dir_config))
        )
        root = result.scalars().first()
        assert root is not None
        assert lf.library_root_id == root.id

    @pytest.mark.asyncio
    async def test_referenced_path_does_not_fallback_to_primary(
        self, session: AsyncSession, issue: Issue, source_file: Path, comics_dir_config: Path
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        with pytest.raises(ConfigurationError, match="inside an enabled library root"):
            await register_library_file(
                session,
                source_file,
                issue,
                MatchConfidence.HIGH,
                move_to_library=False,
            )

        assert source_file.exists()


class TestRenameInPlace:
    """Referenced files cannot be renamed during registration."""

    @pytest.mark.asyncio
    async def test_rename_in_place(
        self, session: AsyncSession, issue: Issue, comics_dir_config: Path
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        src = comics_dir_config / "Batman" / "batman_017_raw.cbz"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"PK" + b"\x00" * 100)

        session.add(SystemConfig(key="rename_on_import", value="true", value_type="bool"))
        await session.flush()

        with pytest.raises(ConfigurationError, match="cannot rename"):
            await register_library_file(
                session,
                src,
                issue,
                MatchConfidence.HIGH,
                move_to_library=False,
                rename=True,
            )

        assert src.exists()
        assert src.name == "batman_017_raw.cbz"


class TestSeriesFolderCreation:
    """Series folder is created when it doesn't exist."""

    @pytest.mark.asyncio
    async def test_series_folder_created(
        self, session: AsyncSession, issue: Issue, source_file: Path, comics_dir_config: Path
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        lf = await register_library_file(
            session, source_file, issue, MatchConfidence.HIGH, move_to_library=True
        )

        # The series folder should exist inside comics dir
        file_path = Path(lf.file_path)
        assert file_path.parent.exists()
        assert file_path.parent.parent == comics_dir_config

    @pytest.mark.asyncio
    async def test_first_library_file_materializes_series_path_and_naming_snapshot(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        series = issue.series
        assert series is not None
        assert series.path is None

        lf = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
        )
        await session.refresh(series)

        final_path = Path(lf.file_path)
        assert series.path == str(final_path.parent)
        assert final_path.parent.parent == comics_dir_config
        assert lf.naming_snapshot["source_path"] == str(source_file)
        assert lf.naming_snapshot["target_path"] == lf.file_path
        assert lf.naming_snapshot["target_file_name"] == lf.file_name
        assert lf.naming_snapshot["rename_enabled"] is True
        assert lf.naming_snapshot["series"]["title"] == "Batman"
        assert lf.naming_snapshot["series"]["year_start"] == 2024
        assert lf.naming_snapshot["issue"]["issue_number"] == 17.0
        assert lf.naming_snapshot["issue"]["effective_issue_type"] == IssueType.ISSUE.value
        assert (
            lf.naming_snapshot["templates"]["comic_file_template"]
            == "{Series} ({Year}) #{Issue:03d}"
        )

    @pytest.mark.asyncio
    async def test_registration_uses_effective_root_policy_for_new_series(
        self,
        session: AsyncSession,
        issue: Issue,
        source_file: Path,
        comics_dir_config: Path,
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        root = (
            await session.execute(
                select(LibraryRoot).where(LibraryRoot.path == str(comics_dir_config))
            )
        ).scalar_one()
        root_policy = LibraryRootPolicy(
            library_root_id=root.id,
            schema_version=1,
            series_path_template="{Publisher}/{Series} ({Year})",
            comic_file_template="{Series} {IssueTitle} Issue {Issue:03d}",
            annual_file_template="{Series} Annual Issue {Issue:03d}",
            non_standard_file_template=("{Series} {Type} {Volume:02d} - {IssueTitle}"),
            single_non_standard_file_template="{Series} {Type} - {IssueTitle}",
            replace_illegal_characters=True,
            colon_replacement="dash",
            source=LibraryRootPolicySource.MANUAL,
            revision=1,
        )
        session.add(root_policy)
        await session.flush()

        library_file = await register_library_file(
            session,
            source_file,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
        )

        final_path = Path(library_file.file_path)
        assert final_path.parent == comics_dir_config / "DC Comics" / "Batman (2024)"
        assert final_path.name == "Batman The Brave and the Bold Issue 017.cbz"
        assert library_file.naming_snapshot["root_policy"] == {
            "id": root_policy.id,
            "source": "manual",
            "revision": 1,
            "source_import_job_id": None,
        }
        assert (
            library_file.naming_snapshot["templates"]["series_path_template"]
            == "{Publisher}/{Series} ({Year})"
        )


class TestErrorHandling:
    """Error handling for missing files and missing config."""

    @pytest.mark.asyncio
    async def test_source_missing_recovers_existing_materialized_target(
        self,
        session: AsyncSession,
        issue: Issue,
        comics_dir_config: Path,
        tmp_path: Path,
    ) -> None:
        """If a prior move succeeded, register the existing library file on retry."""
        from pullbox.core.file_ops import register_library_file, resolve_library_destination

        session.add(SystemConfig(key="rename_on_import", value="true", value_type="bool"))
        await session.flush()

        missing_source = tmp_path / "downloads" / "Batman 017 (2024).cbz"
        target_path, _root = await resolve_library_destination(
            session,
            missing_source,
            issue,
            rename=True,
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"PK" + b"\x00" * 100)

        lf = await register_library_file(
            session,
            missing_source,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            rename=True,
        )

        assert lf.file_path == str(target_path)
        assert Path(lf.file_path).exists()
        await session.refresh(issue)
        assert issue.status == IssueStatus.OWNED

    @pytest.mark.asyncio
    async def test_source_not_found(
        self, session: AsyncSession, issue: Issue, comics_dir_config: Path
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        missing = Path("/tmp/nonexistent/file.cbz")
        with pytest.raises(FileNotFoundError):
            await register_library_file(
                session, missing, issue, MatchConfidence.HIGH, move_to_library=True
            )

    @pytest.mark.asyncio
    async def test_no_comics_directory_configured(
        self, session: AsyncSession, source_file: Path
    ) -> None:
        from pullbox.core.file_ops import register_library_file

        # Create a bare issue with no comics directory or library root configured
        bare_series = Series(title="Test", sort_title="Test")
        session.add(bare_series)
        await session.flush()
        bare_issue = Issue(
            series_id=bare_series.id,
            issue_number=1.0,
            status=IssueStatus.WANTED,
            issue_type=IssueType.ISSUE,
        )
        session.add(bare_issue)
        await session.flush()

        with pytest.raises(ConfigurationError):
            await register_library_file(
                session, source_file, bare_issue, MatchConfidence.HIGH, move_to_library=True
            )


class TestEdgeCases:
    """Edge cases for format detection, config resolution, confidence levels, and naming."""

    @pytest.mark.asyncio
    async def test_cbr_format_detection(
        self, session: AsyncSession, issue: Issue, comics_dir_config: Path, tmp_path: Path
    ) -> None:
        """Register a .cbr file and verify file_format == FileFormat.CBR."""
        from pullbox.core.file_ops import register_library_file

        src = tmp_path / "downloads" / "Batman 017 (2024).cbr"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"\x00" * 100)

        lf = await register_library_file(
            session, src, issue, MatchConfidence.HIGH, move_to_library=True, rename=False
        )

        assert lf.file_format == FileFormat.CBR

    @pytest.mark.asyncio
    async def test_pdf_format_detection(
        self, session: AsyncSession, issue: Issue, comics_dir_config: Path, tmp_path: Path
    ) -> None:
        """Register a .pdf file and verify file_format == FileFormat.PDF."""
        from pullbox.core.file_ops import register_library_file

        src = tmp_path / "downloads" / "Batman 017 (2024).pdf"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"%PDF" + b"\x00" * 100)

        lf = await register_library_file(
            session, src, issue, MatchConfidence.HIGH, move_to_library=True, rename=False
        )

        assert lf.file_format == FileFormat.PDF

    @pytest.mark.asyncio
    async def test_cb7_format_detection(
        self, session: AsyncSession, issue: Issue, comics_dir_config: Path, tmp_path: Path
    ) -> None:
        """Register a .cb7 file and verify file_format == FileFormat.CB7."""
        from pullbox.core.file_ops import register_library_file

        src = tmp_path / "downloads" / "Batman 017 (2024).cb7"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"\x00" * 100)

        lf = await register_library_file(
            session, src, issue, MatchConfidence.HIGH, move_to_library=True, rename=False
        )

        assert lf.file_format == FileFormat.CB7

    @pytest.mark.asyncio
    async def test_unknown_extension_fallback(
        self, session: AsyncSession, issue: Issue, comics_dir_config: Path, tmp_path: Path
    ) -> None:
        """Register a .xyz file (unknown extension) and verify fallback to FileFormat.CBZ."""
        from pullbox.core.file_ops import register_library_file

        src = tmp_path / "downloads" / "Batman 017 (2024).xyz"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"\x00" * 100)

        lf = await register_library_file(
            session, src, issue, MatchConfidence.HIGH, move_to_library=True, rename=False
        )

        assert lf.file_format == FileFormat.CBZ

    @pytest.mark.asyncio
    async def test_rename_none_reads_config_false(
        self, session: AsyncSession, issue: Issue, comics_dir_config: Path, tmp_path: Path
    ) -> None:
        """When rename=None (default) and rename_on_import config is 'false', file NOT renamed."""
        from pullbox.core.file_ops import register_library_file

        session.add(SystemConfig(key="rename_on_import", value="false", value_type="bool"))
        await session.flush()

        src = tmp_path / "downloads" / "Batman_raw_017.cbz"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"PK" + b"\x00" * 100)

        lf = await register_library_file(
            session, src, issue, MatchConfidence.HIGH, move_to_library=True
        )

        # File should keep its original name (not renamed per template)
        assert lf.file_name == "Batman_raw_017.cbz"


class TestTargetFilename:
    """Target filename generation for rename/import paths."""

    @pytest.mark.asyncio
    async def test_non_standard_uses_issue_number_as_volume(
        self, session: AsyncSession, series: Series
    ) -> None:
        from pullbox.core.file_ops import _compute_target_filename

        issue = Issue(
            series_id=series.id,
            issue_number=2.0,
            title="Broken Dreams",
            status=IssueStatus.OWNED,
            issue_type=IssueType.VOLUME,
        )
        session.add(issue)
        await session.flush()

        result = _compute_target_filename(
            issue,
            series,
            Path("Batman #002.cbz"),
            {
                "non_standard_file_template": "{Series} ({Year}) {Type} {Volume:02d} {Edition}",
                "single_non_standard_file_template": "{Series} ({Year}) {Type} - {Title}",
                "replace_illegal_characters": "true",
                "colon_replacement": "dash",
            },
        )

        assert result == "Batman (2024) Vol 02.cbz"

    @pytest.mark.asyncio
    async def test_mixed_tpb_volume_series_normalizes_tpb_to_vol_label(
        self, session: AsyncSession, series: Series
    ) -> None:
        from pullbox.core.file_ops import _resolve_naming_issue_type

        tpb_issue = Issue(
            series_id=series.id,
            issue_number=1.0,
            title="Volume 1",
            status=IssueStatus.OWNED,
            issue_type=IssueType.TPB,
        )
        volume_issue = Issue(
            series_id=series.id,
            issue_number=2.0,
            title="Broken Dreams",
            status=IssueStatus.OWNED,
            issue_type=IssueType.VOLUME,
        )
        session.add_all([tpb_issue, volume_issue])
        await session.flush()

        result = await _resolve_naming_issue_type(session, tpb_issue)

        assert result == "volume"

    @pytest.mark.asyncio
    async def test_standalone_tpb_keeps_tpb_label(
        self, session: AsyncSession, series: Series
    ) -> None:
        from pullbox.core.file_ops import _resolve_naming_issue_type

        tpb_issue = Issue(
            series_id=series.id,
            issue_number=1.0,
            title="Volume 1",
            status=IssueStatus.OWNED,
            issue_type=IssueType.TPB,
        )
        session.add(tpb_issue)
        await session.flush()

        result = await _resolve_naming_issue_type(session, tpb_issue)

        assert result == "tpb"

    @pytest.mark.asyncio
    async def test_one_shot_does_not_synthesize_volume_number(
        self, session: AsyncSession, series: Series
    ) -> None:
        from pullbox.core.file_ops import _compute_target_filename

        issue = Issue(
            series_id=series.id,
            issue_number=1.0,
            title="Zero Hour",
            status=IssueStatus.OWNED,
            issue_type=IssueType.ONE_SHOT,
        )
        session.add(issue)
        await session.flush()

        result = _compute_target_filename(
            issue,
            series,
            Path("Ignite Prime.cbz"),
            {
                "non_standard_file_template": "{Series} ({Year}) {Type} {Volume:02d} - {Title}",
                "single_non_standard_file_template": "{Series} ({Year}) {Type} - {Title}",
                "replace_illegal_characters": "true",
                "colon_replacement": "dash",
            },
        )

        assert result == "Batman (2024) One-Shot - Zero Hour.cbz"

    @pytest.mark.asyncio
    async def test_graphic_novel_uses_single_release_template(
        self, session: AsyncSession, series: Series
    ) -> None:
        from pullbox.core.file_ops import _compute_target_filename

        issue = Issue(
            series_id=series.id,
            issue_number=1.0,
            title="Freshman Year",
            status=IssueStatus.OWNED,
            issue_type=IssueType.GN,
        )
        session.add(issue)
        await session.flush()

        result = _compute_target_filename(
            issue,
            series,
            Path("College Try.cbz"),
            {
                "non_standard_file_template": "{Series} ({Year}) {Type} {Volume:02d} - {Title}",
                "single_non_standard_file_template": "{Series} ({Year}) {Type} - {Title}",
                "replace_illegal_characters": "true",
                "colon_replacement": "dash",
            },
        )

        assert result == "Batman (2024) GN - Freshman Year.cbz"

    @pytest.mark.asyncio
    async def test_rename_none_defaults_to_true(
        self, session: AsyncSession, issue: Issue, comics_dir_config: Path, tmp_path: Path
    ) -> None:
        """When rename=None (default) and no rename_on_import config key, file IS renamed."""
        from pullbox.core.file_ops import register_library_file

        # Ensure no rename_on_import key is set (comics_dir_config doesn't add one)
        src = tmp_path / "downloads" / "Batman_raw_017.cbz"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"PK" + b"\x00" * 100)

        lf = await register_library_file(
            session, src, issue, MatchConfidence.HIGH, move_to_library=True
        )

        # File should be renamed (not the original name)
        assert lf.file_name != "Batman_raw_017.cbz"
        assert lf.file_name.endswith(".cbz")

    @pytest.mark.asyncio
    async def test_already_owned_issue(
        self, session: AsyncSession, issue: Issue, source_file: Path, comics_dir_config: Path
    ) -> None:
        """Register file for an issue already in OWNED status — no crash, stays OWNED."""
        from pullbox.core.file_ops import register_library_file

        issue.status = IssueStatus.OWNED
        await session.flush()

        lf = await register_library_file(
            session, source_file, issue, MatchConfidence.HIGH, move_to_library=True
        )

        assert lf is not None
        await session.refresh(issue)
        assert issue.status == IssueStatus.OWNED

    @pytest.mark.asyncio
    async def test_medium_confidence(
        self, session: AsyncSession, issue: Issue, source_file: Path, comics_dir_config: Path
    ) -> None:
        """Register with MatchConfidence.MEDIUM and verify it is stored."""
        from pullbox.core.file_ops import register_library_file

        lf = await register_library_file(
            session, source_file, issue, MatchConfidence.MEDIUM, move_to_library=True
        )

        assert lf.match_confidence == MatchConfidence.MEDIUM

    @pytest.mark.asyncio
    async def test_low_confidence(
        self, session: AsyncSession, issue: Issue, source_file: Path, comics_dir_config: Path
    ) -> None:
        """Register with MatchConfidence.LOW and verify it is stored."""
        from pullbox.core.file_ops import register_library_file

        lf = await register_library_file(
            session, source_file, issue, MatchConfidence.LOW, move_to_library=True
        )

        assert lf.match_confidence == MatchConfidence.LOW

    @pytest.mark.asyncio
    async def test_unicode_filename(
        self, session: AsyncSession, issue: Issue, comics_dir_config: Path, tmp_path: Path
    ) -> None:
        """Register a file with unicode characters in the name."""
        from pullbox.core.file_ops import register_library_file

        src = tmp_path / "downloads" / "\u30d0\u30c3\u30c8\u30de\u30f3 001.cbz"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"PK" + b"\x00" * 100)

        lf = await register_library_file(
            session, src, issue, MatchConfidence.HIGH, move_to_library=True, rename=False
        )

        assert lf is not None
        assert Path(lf.file_path).exists()
        assert lf.file_name == "\u30d0\u30c3\u30c8\u30de\u30f3 001.cbz"

    @pytest.mark.asyncio
    async def test_name_collision_counter(
        self, session: AsyncSession, issue: Issue, comics_dir_config: Path, tmp_path: Path
    ) -> None:
        """When target filename already exists in library, counter suffix (1) is applied."""
        from pullbox.core.file_ops import register_library_file
        from pullbox.core.naming import format_series_folder

        # Pre-create the series folder and a file with the same name as what will be placed
        folder_name = format_series_folder(
            "Batman",
            year=2024,
            publisher="DC Comics",
        )
        series_folder = comics_dir_config / folder_name
        series_folder.mkdir(parents=True, exist_ok=True)

        # Create a file at the exact target path (original name, no rename)
        existing = series_folder / "Batman 017 (2024).cbz"
        existing.write_bytes(b"PK" + b"\x00" * 50)

        src = tmp_path / "downloads" / "Batman 017 (2024).cbz"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"PK" + b"\x00" * 100)

        lf = await register_library_file(
            session, src, issue, MatchConfidence.HIGH, move_to_library=True, rename=False
        )

        # File should have the counter suffix
        assert lf.file_name == "Batman 017 (2024) (1).cbz"
        assert Path(lf.file_path).exists()

    @pytest.mark.asyncio
    async def test_leave_in_place_rename_outside_comics_dir(
        self, session: AsyncSession, issue: Issue, comics_dir_config: Path, tmp_path: Path
    ) -> None:
        """Referenced registration rejects rename without touching an external source."""
        from pullbox.core.file_ops import register_library_file

        # Create source file outside comics dir
        src = tmp_path / "external" / "batman_raw_017.cbz"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"PK" + b"\x00" * 100)

        with pytest.raises(ConfigurationError, match="cannot rename"):
            await register_library_file(
                session,
                src,
                issue,
                MatchConfidence.HIGH,
                move_to_library=False,
                rename=True,
            )

        # Rejected registration leaves the external source untouched.
        assert src.exists()
        assert src.name == "batman_raw_017.cbz"

    @pytest.mark.asyncio
    async def test_epub_format_detection(
        self, session: AsyncSession, issue: Issue, comics_dir_config: Path, tmp_path: Path
    ) -> None:
        """Register a .epub file and verify file_format == FileFormat.EPUB."""
        from pullbox.core.file_ops import register_library_file

        src = tmp_path / "downloads" / "Batman 017 (2024).epub"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"PK" + b"\x00" * 100)

        lf = await register_library_file(
            session, src, issue, MatchConfidence.HIGH, move_to_library=True, rename=False
        )

        assert lf.file_format == FileFormat.EPUB
