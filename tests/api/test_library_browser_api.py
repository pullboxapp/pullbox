"""Focused API coverage for library browser entry actions."""

from __future__ import annotations

import os
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from pullbox.api.v1 import library as library_api
from pullbox.core.exceptions import ValidationError
from pullbox.models.config import SystemConfig
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import (
    FileFormat,
    LibraryFile,
    LibraryFileStorageMode,
    LibraryRoot,
    MatchConfidence,
)
from pullbox.models.series import Series
from pullbox.schemas.library import (
    LibraryBrowserConvertRequest,
    LibraryBrowserDeleteRequest,
    LibraryBrowserManualRenameRequest,
    ManualMatchRequest,
)
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest_plugins = ["conftest_security"]

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-library-browser-api")


def _csrf_header_for(client) -> dict[str, str]:  # type: ignore[no-untyped-def]
    token = client.cookies.get(SESSION_COOKIE_NAME) or ""
    csrf = AuthService.get_csrf_token_from_session(token) or ""
    return {"X-CSRF-Token": csrf}


def _write_zip_archive(path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("page_0001.txt", "convert me")


@pytest.fixture
async def seeded_library_browser_data(
    sec_db: async_sessionmaker,
    tmp_path: Path,
) -> dict[str, Path]:
    root_path = tmp_path / "library"
    root_path.mkdir(parents=True)

    collection_folder = root_path / "Collections"
    collection_folder.mkdir()

    series_folder = collection_folder / "Series Folder"
    series_folder.mkdir()
    series_file = series_folder / "Issue 001.cbz"
    series_file.write_bytes(b"series-file")

    series_without_issues_folder = collection_folder / "Series Without Issues (2012) [50771]"
    series_without_issues_folder.mkdir()
    stale_series_file = series_without_issues_folder / "Issue 002.cbz"
    stale_series_file.write_bytes(b"stale-series-file")

    loose_file = root_path / "Loose File.cbz"
    loose_file.write_bytes(b"loose")

    loose_folder = root_path / "Loose Folder"
    loose_folder.mkdir()
    loose_folder_file = loose_folder / "Loose Folder 001.cbz"
    loose_folder_file.write_bytes(b"loose-folder")

    untracked_file = root_path / "Not Pullbox Owned.cbz"
    untracked_file.write_bytes(b"external")

    untracked_folder = root_path / "External Manager Folder"
    untracked_folder.mkdir()
    (untracked_folder / "External Issue.cbz").write_bytes(b"external")

    convertible_file = root_path / "Convert Me.cbr"
    _write_zip_archive(convertible_file)
    referenced_file = root_path / "Referenced.cbr"
    _write_zip_archive(referenced_file)

    trash_dir = tmp_path / ".trash"
    trash_dir.mkdir()

    async with sec_db() as session:
        root = LibraryRoot(name="Primary Root", path=str(root_path), enabled=True)
        session.add(root)
        session.add(
            SystemConfig(
                key="utility_trash_folder",
                value=str(trash_dir),
                value_type="string",
            )
        )
        await session.flush()

        series = Series(
            title="Series Folder",
            sort_title="series folder",
            path=str(series_folder),
            library_root_id=root.id,
            monitored=True,
        )
        session.add(series)
        session.add(
            Series(
                title="Series Without Issues",
                sort_title="series without issues",
                comicvine_id=50771,
                path=str(collection_folder / "Series Without Issues (2012)"),
                library_root_id=root.id,
                monitored=False,
            )
        )
        await session.flush()

        issue = Issue(
            series_id=series.id,
            issue_number=1.0,
            status=IssueStatus.OWNED,
        )
        session.add(issue)
        await session.flush()

        session.add_all(
            [
                LibraryFile(
                    library_root_id=root.id,
                    file_path=str(loose_file),
                    file_name=loose_file.name,
                    file_size=loose_file.stat().st_size,
                    file_format=FileFormat.CBZ,
                    file_modified_at=datetime.fromtimestamp(loose_file.stat().st_mtime, tz=UTC),
                    match_confidence=MatchConfidence.UNMATCHED,
                    has_comicinfo=False,
                ),
                LibraryFile(
                    library_root_id=root.id,
                    file_path=str(referenced_file),
                    file_name=referenced_file.name,
                    file_size=referenced_file.stat().st_size,
                    file_format=FileFormat.CBR,
                    file_modified_at=datetime.fromtimestamp(
                        referenced_file.stat().st_mtime, tz=UTC
                    ),
                    match_confidence=MatchConfidence.UNMATCHED,
                    has_comicinfo=False,
                    storage_mode=LibraryFileStorageMode.REFERENCED,
                ),
                LibraryFile(
                    library_root_id=root.id,
                    file_path=str(series_file),
                    file_name=series_file.name,
                    file_size=series_file.stat().st_size,
                    file_format=FileFormat.CBZ,
                    file_modified_at=datetime.fromtimestamp(series_file.stat().st_mtime, tz=UTC),
                    match_confidence=MatchConfidence.MANUAL,
                    has_comicinfo=False,
                    issue_id=issue.id,
                ),
                LibraryFile(
                    library_root_id=root.id,
                    file_path=str(convertible_file),
                    file_name=convertible_file.name,
                    file_size=convertible_file.stat().st_size,
                    file_format=FileFormat.CBR,
                    file_modified_at=datetime.fromtimestamp(
                        convertible_file.stat().st_mtime, tz=UTC
                    ),
                    match_confidence=MatchConfidence.UNMATCHED,
                    has_comicinfo=False,
                ),
                LibraryFile(
                    library_root_id=root.id,
                    file_path=str(loose_folder_file),
                    file_name=loose_folder_file.name,
                    file_size=loose_folder_file.stat().st_size,
                    file_format=FileFormat.CBZ,
                    file_modified_at=datetime.fromtimestamp(
                        loose_folder_file.stat().st_mtime, tz=UTC
                    ),
                    match_confidence=MatchConfidence.UNMATCHED,
                    has_comicinfo=False,
                ),
            ]
        )
        await session.commit()

    return {
        "root_path": root_path,
        "collection_folder": collection_folder,
        "series_folder": series_folder,
        "series_file": series_file,
        "series_without_issues_folder": series_without_issues_folder,
        "stale_series_file": stale_series_file,
        "loose_file": loose_file,
        "loose_folder": loose_folder,
        "loose_folder_file": loose_folder_file,
        "untracked_file": untracked_file,
        "untracked_folder": untracked_folder,
        "convertible_file": convertible_file,
        "referenced_file": referenced_file,
        "trash_dir": trash_dir,
    }


@pytest.mark.asyncio
async def test_library_browser_entry_returns_real_metadata_and_actions(
    authenticated_client,
    seeded_library_browser_data: dict[str, Path],
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.get(
        "/api/v1/library/browser/entry",
        params={"path": str(seeded_library_browser_data["convertible_file"])},
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Convert Me.cbr"
    assert data["kind"] == "file"
    assert data["root_name"] == "Primary Root"
    assert data["actions"]["can_properties"] is True
    assert data["actions"]["can_rename"] is True
    assert data["actions"]["can_auto_rename"] is True
    assert data["actions"]["can_convert"] is True
    assert data["actions"]["can_delete"] is True
    assert data["delete_context"]["mode"] == "file"
    assert data["delete_context"]["trash_enabled"] is True
    assert data["delete_context"]["has_linked_issue"] is False
    assert data["delete_context"]["issue_status_after_delete"] is None
    assert data["rename_context"]["stale_reference"] is False


@pytest.mark.asyncio
async def test_library_browser_entry_disables_mutations_for_referenced_file(
    authenticated_client,
    seeded_library_browser_data: dict[str, Path],
) -> None:  # type: ignore[no-untyped-def]
    referenced_file = seeded_library_browser_data["referenced_file"]
    response = await authenticated_client.get(
        "/api/v1/library/browser/entry",
        params={"path": str(referenced_file)},
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["delete_context"]["referenced_file_count"] == 1
    assert data["delete_context"]["managed_file_count"] == 0
    assert data["actions"] == {
        "can_properties": True,
        "can_rename": False,
        "can_auto_rename": False,
        "can_convert": False,
        "can_delete": True,
    }

    rename_response = await authenticated_client.post(
        "/api/v1/library/browser/rename/manual/validate",
        json={"path": str(referenced_file), "proposed_name": "Renamed.cbr"},
        headers=_csrf_header_for(authenticated_client),
    )
    convert_response = await authenticated_client.post(
        "/api/v1/library/browser/convert",
        json={"path": str(referenced_file)},
        headers=_csrf_header_for(authenticated_client),
    )

    assert rename_response.status_code == 422
    assert (
        "Referenced library files cannot be renamed" in rename_response.json()["error"]["message"]
    )
    assert convert_response.status_code == 422
    assert (
        "Referenced library files cannot be converted"
        in convert_response.json()["error"]["message"]
    )
    assert referenced_file.exists()


@pytest.mark.asyncio
async def test_library_browser_entry_allows_direct_tracked_file_parent_folder(
    authenticated_client,
    seeded_library_browser_data: dict[str, Path],
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.get(
        "/api/v1/library/browser/entry",
        params={"path": str(seeded_library_browser_data["loose_folder"])},
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Loose Folder"
    assert data["kind"] == "folder"
    assert data["actions"]["can_properties"] is True
    assert data["actions"]["can_rename"] is True
    assert data["actions"]["can_auto_rename"] is True
    assert data["actions"]["can_convert"] is False
    assert data["actions"]["can_delete"] is True
    assert data["delete_context"]["mode"] == "folder"
    assert data["delete_context"]["tracked_file_count"] == 1


@pytest.mark.asyncio
async def test_folder_descendant_detection_uses_normalized_library_file_paths(
    monkeypatch: pytest.MonkeyPatch,
    sec_db: async_sessionmaker,
    tmp_path: Path,
) -> None:
    target = tmp_path / "visible-root"
    target.mkdir()
    stored_path = "/private/tmp/pullbox-visible-root/Series/Issue 001.cbz"

    def fake_normalize(path_value: str | Path | None) -> str | None:
        if path_value is None:
            return None
        path_text = str(path_value)
        if path_text == str(target):
            return "/tmp/pullbox-visible-root"
        if path_text == stored_path:
            return "/tmp/pullbox-visible-root/Series/Issue 001.cbz"
        return path_text

    monkeypatch.setattr(library_api, "_normalize_library_path", fake_normalize)

    async with sec_db() as session:
        root = LibraryRoot(name="Primary Root", path=str(target), enabled=True)
        session.add(root)
        await session.flush()
        session.add(
            LibraryFile(
                library_root_id=root.id,
                file_path=stored_path,
                file_name="Issue 001.cbz",
                file_size=12,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(UTC),
                match_confidence=MatchConfidence.UNMATCHED,
                has_comicinfo=False,
            )
        )
        await session.commit()

    async with sec_db() as session:
        assert await library_api._folder_has_tracked_descendants(session, target) is True


@pytest.mark.asyncio
async def test_folder_descendant_detection_uses_normalized_series_paths(
    monkeypatch: pytest.MonkeyPatch,
    sec_db: async_sessionmaker,
    tmp_path: Path,
) -> None:
    target = tmp_path / "visible-root"
    target.mkdir()
    stored_path = "/private/tmp/pullbox-visible-root/Series"

    def fake_normalize(path_value: str | Path | None) -> str | None:
        if path_value is None:
            return None
        path_text = str(path_value)
        if path_text == str(target):
            return "/tmp/pullbox-visible-root"
        if path_text == stored_path:
            return "/tmp/pullbox-visible-root/Series"
        return path_text

    monkeypatch.setattr(library_api, "_normalize_library_path", fake_normalize)

    async with sec_db() as session:
        root = LibraryRoot(name="Primary Root", path=str(target), enabled=True)
        session.add(root)
        await session.flush()
        session.add(
            Series(
                title="Series",
                sort_title="series",
                path=stored_path,
                library_root_id=root.id,
                monitored=True,
            )
        )
        await session.commit()

    async with sec_db() as session:
        assert await library_api._folder_has_tracked_descendants(session, target) is True


@pytest.mark.asyncio
async def test_library_browser_entry_reports_post_delete_issue_status_for_tracked_file(
    authenticated_client,
    seeded_library_browser_data: dict[str, Path],
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.get(
        "/api/v1/library/browser/entry",
        params={"path": str(seeded_library_browser_data["series_file"])},
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["kind"] == "file"
    assert data["delete_context"]["mode"] == "file"
    assert data["delete_context"]["tracked_file_count"] == 1
    assert data["delete_context"]["has_linked_issue"] is True
    assert data["delete_context"]["issue_status_after_delete"] == "wanted"
    assert data["delete_context"]["issue_status_reason"] == "series_monitored"


@pytest.mark.asyncio
async def test_library_browser_entry_detects_series_folder_delete_context(
    authenticated_client,
    seeded_library_browser_data: dict[str, Path],
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.get(
        "/api/v1/library/browser/entry",
        params={"path": str(seeded_library_browser_data["series_folder"])},
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["kind"] == "folder"
    assert data["delete_context"]["mode"] == "series"
    assert data["delete_context"]["series_title"] == "Series Folder"
    assert data["delete_context"]["linked_file_count"] == 1


@pytest.mark.asyncio
async def test_library_browser_entry_detects_series_folder_without_issues(
    authenticated_client,
    seeded_library_browser_data: dict[str, Path],
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.get(
        "/api/v1/library/browser/entry",
        params={"path": str(seeded_library_browser_data["series_without_issues_folder"])},
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["kind"] == "folder"
    assert data["delete_context"]["mode"] == "series"
    assert data["delete_context"]["series_title"] == "Series Without Issues"
    assert data["delete_context"]["linked_file_count"] == 0
    assert data["rename_context"]["stale_reference"] is True
    assert data["rename_context"]["reason_code"] == "stale_series_path"


@pytest.mark.asyncio
async def test_library_browser_entry_rejects_untracked_file_inside_stale_series_folder(
    authenticated_client,
    seeded_library_browser_data: dict[str, Path],
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.get(
        "/api/v1/library/browser/entry",
        params={"path": str(seeded_library_browser_data["stale_series_file"])},
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 422
    assert "not tracked by Pullbox" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_library_browser_entry_rejects_untracked_disk_file(
    authenticated_client,
    seeded_library_browser_data: dict[str, Path],
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.get(
        "/api/v1/library/browser/entry",
        params={"path": str(seeded_library_browser_data["untracked_file"])},
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 422
    assert "not tracked by Pullbox" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_library_manual_rename_validation_rejects_extension_change(
    authenticated_client,
    seeded_library_browser_data: dict[str, Path],
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.post(
        "/api/v1/library/browser/rename/manual/validate",
        json={
            "path": str(seeded_library_browser_data["convertible_file"]),
            "proposed_name": "Convert Me.cbz",
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 422
    assert response.json()["error"]["message"] == "File rename must keep the existing extension."


@pytest.mark.asyncio
async def test_library_convert_executes_immediately_and_updates_tracked_file_record(
    authenticated_client,
    seeded_library_browser_data: dict[str, Path],
    sec_db: async_sessionmaker,
) -> None:  # type: ignore[no-untyped-def]
    original_file = seeded_library_browser_data["convertible_file"]
    converted_file = original_file.with_suffix(".cbz")

    response = await authenticated_client.post(
        "/api/v1/library/browser/convert",
        json={"path": str(original_file)},
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "file"
    assert payload["source_path"] == str(original_file)
    assert payload["target_path"] == str(converted_file)
    assert payload["original_trash_path"].startswith(str(seeded_library_browser_data["trash_dir"]))
    assert original_file.exists() is False
    assert converted_file.exists() is True
    assert Path(payload["original_trash_path"]).exists() is True

    async with sec_db() as session:
        library_file = (
            await session.execute(
                select(LibraryFile).where(LibraryFile.file_path == str(converted_file))
            )
        ).scalar_one()
        assert library_file.file_name == converted_file.name
        assert library_file.file_format == FileFormat.CBZ
        assert library_file.file_size == converted_file.stat().st_size


@pytest.mark.asyncio
async def test_library_convert_rejects_folder_targets(
    authenticated_client,
    seeded_library_browser_data: dict[str, Path],
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.post(
        "/api/v1/library/browser/convert",
        json={"path": str(seeded_library_browser_data["collection_folder"])},
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Only files can be converted from the browser."


@pytest.mark.asyncio
async def test_library_manual_rename_validation_rejects_stale_series_folder(
    authenticated_client,
    seeded_library_browser_data: dict[str, Path],
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.post(
        "/api/v1/library/browser/rename/manual/validate",
        json={
            "path": str(seeded_library_browser_data["series_without_issues_folder"]),
            "proposed_name": "Series Without Issues (2012) [50771] Renamed",
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 422
    assert (
        "stored series path does not match the folder on disk"
        in response.json()["error"]["message"].lower()
    )


@pytest.mark.asyncio
async def test_library_manual_rename_validation_rejects_file_inside_stale_series_folder(
    authenticated_client,
    seeded_library_browser_data: dict[str, Path],
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.post(
        "/api/v1/library/browser/rename/manual/validate",
        json={
            "path": str(seeded_library_browser_data["stale_series_file"]),
            "proposed_name": "Issue 002 Renamed.cbz",
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 422
    assert "not tracked by Pullbox" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_library_manual_rename_executes_immediately_and_updates_tracked_file_record(
    authenticated_client,
    seeded_library_browser_data: dict[str, Path],
    sec_db: async_sessionmaker,
) -> None:  # type: ignore[no-untyped-def]
    original_file = seeded_library_browser_data["loose_file"]
    renamed_file = original_file.with_name("Loose File Deluxe.cbz")

    response = await authenticated_client.post(
        "/api/v1/library/browser/rename",
        json={
            "path": str(original_file),
            "proposed_name": renamed_file.name,
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "file"
    assert payload["source_path"] == str(original_file)
    assert payload["target_path"] == str(renamed_file)
    assert original_file.exists() is False
    assert renamed_file.exists() is True

    async with sec_db() as session:
        library_file = (
            await session.execute(
                select(LibraryFile).where(LibraryFile.file_name == renamed_file.name)
            )
        ).scalar_one()
        assert library_file.file_path == str(renamed_file)
        assert library_file.file_name == renamed_file.name


@pytest.mark.asyncio
async def test_library_folder_rename_updates_descendant_series_and_file_paths(
    authenticated_client,
    seeded_library_browser_data: dict[str, Path],
    sec_db: async_sessionmaker,
) -> None:  # type: ignore[no-untyped-def]
    original_folder = seeded_library_browser_data["series_folder"]
    original_series_file = seeded_library_browser_data["series_file"]
    renamed_folder = original_folder.with_name("Series Folder Deluxe")

    response = await authenticated_client.post(
        "/api/v1/library/browser/rename",
        json={
            "path": str(original_folder),
            "proposed_name": renamed_folder.name,
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "folder"
    assert payload["source_path"] == str(original_folder)
    assert payload["target_path"] == str(renamed_folder)
    assert original_folder.exists() is False
    assert renamed_folder.exists() is True
    assert (renamed_folder / original_series_file.name).exists() is True

    async with sec_db() as session:
        series = (
            await session.execute(select(Series).where(Series.title == "Series Folder"))
        ).scalar_one()
        library_file = (
            await session.execute(
                select(LibraryFile).where(LibraryFile.file_name == original_series_file.name)
            )
        ).scalar_one()

        assert series.path == str(renamed_folder)
        assert library_file.file_path == str(renamed_folder / original_series_file.name)
        assert library_file.file_name == original_series_file.name


@pytest.mark.asyncio
async def test_library_delete_moves_tracked_file_and_updates_issue_status(
    authenticated_client,
    seeded_library_browser_data: dict[str, Path],
    sec_db: async_sessionmaker,
) -> None:  # type: ignore[no-untyped-def]
    tracked_file = seeded_library_browser_data["series_file"]

    response = await authenticated_client.post(
        "/api/v1/library/browser/delete",
        json={"path": str(tracked_file)},
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "file"
    assert payload["mode"] == "file"
    assert payload["deleted_via_trash"] is True
    assert tracked_file.exists() is False
    assert Path(payload["result_path"]).exists() is True

    async with sec_db() as session:
        remaining = await session.execute(
            select(LibraryFile).where(LibraryFile.file_path == str(tracked_file))
        )
        issue = (await session.execute(select(Issue).where(Issue.issue_number == 1.0))).scalar_one()
        assert remaining.scalar_one_or_none() is None
        assert issue.status == IssueStatus.WANTED


@pytest.mark.asyncio
async def test_library_delete_catalog_ancestor_folder_is_rejected(
    authenticated_client,
    seeded_library_browser_data: dict[str, Path],
) -> None:  # type: ignore[no-untyped-def]
    folder = seeded_library_browser_data["collection_folder"]
    child_file = seeded_library_browser_data["series_file"]

    response = await authenticated_client.post(
        "/api/v1/library/browser/delete",
        json={"path": str(folder)},
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 422
    assert "tracked series folders" in response.json()["error"]["message"]
    assert folder.exists() is True
    assert child_file.exists() is True


@pytest.mark.asyncio
async def test_library_delete_series_folder_routes_to_series_delete_behavior(
    authenticated_client,
    seeded_library_browser_data: dict[str, Path],
    sec_db: async_sessionmaker,
) -> None:  # type: ignore[no-untyped-def]
    folder = seeded_library_browser_data["series_folder"]
    child_file = seeded_library_browser_data["series_file"]

    response = await authenticated_client.post(
        "/api/v1/library/browser/delete",
        json={
            "path": str(folder),
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "series"
    assert folder.exists() is False
    assert child_file.exists() is False

    async with sec_db() as session:
        series = (
            await session.execute(select(Series).where(Series.title == "Series Folder"))
        ).scalar_one_or_none()
        assert series is None


@pytest.mark.asyncio
async def test_library_delete_series_folder_without_issues_uses_series_path(
    authenticated_client,
    seeded_library_browser_data: dict[str, Path],
    sec_db: async_sessionmaker,
) -> None:  # type: ignore[no-untyped-def]
    folder = seeded_library_browser_data["series_without_issues_folder"]

    response = await authenticated_client.post(
        "/api/v1/library/browser/delete",
        json={
            "path": str(folder),
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "series"
    assert folder.exists() is False

    async with sec_db() as session:
        series = (
            await session.execute(select(Series).where(Series.title == "Series Without Issues"))
        ).scalar_one_or_none()
        assert series is None


class _BrokenFileLike:
    def is_file(self) -> bool:
        return True

    def is_dir(self) -> bool:
        return False

    def stat(self):  # type: ignore[no-untyped-def]
        raise OSError("boom")


def test_library_browser_helper_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root_path = tmp_path / "library"
    root_path.mkdir()
    child = root_path / "Child"
    child.mkdir()
    visible = child / "visible.cbz"
    visible.write_bytes(b"abc")
    hidden = child / ".hidden.cbz"
    hidden.write_bytes(b"hidden")
    outside = tmp_path / "outside.cbz"
    outside.write_bytes(b"outside")

    root = LibraryRoot(id=7, name="Root", path=str(root_path), enabled=True)

    assert library_api._is_relative_to(child, root_path) is True
    assert library_api._is_relative_to(tmp_path / "outside", root_path) is False
    assert library_api._sanitize_library_name('  Bad<>:"/\\|?*  Name... ') == "Bad Name"
    assert library_api._normalize_library_path(None) is None
    assert library_api._library_entry_kind(root_path, root_path=root_path) == "root"
    assert library_api._library_entry_kind(child, root_path=root_path) == "folder"
    assert library_api._library_entry_kind(visible, root_path=root_path) == "file"
    assert library_api._library_file_format(visible) == "CBZ"
    assert library_api._library_file_format(root_path / "README") is None
    assert library_api._visible_child_count(visible) is None
    assert library_api._visible_child_count(child) == 1
    assert library_api._entry_size_bytes(visible) == 3
    assert library_api._entry_size_bytes(root_path / "missing") is None
    assert library_api._entry_size_bytes(child) == 3
    assert library_api._entry_modified_at(visible) is not None
    assert library_api._entry_permissions_label(visible)
    assert (
        library_api._build_library_actions(
            kind="root", file_format=None, tracking_scope="root"
        ).can_rename
        is False
    )
    assert (
        library_api._build_library_actions(
            kind="file", file_format="cbr", tracking_scope="tracked_file"
        ).can_convert
        is True
    )
    assert (
        library_api._build_library_actions(
            kind="folder",
            file_format=None,
            tracking_scope="tracked_descendant_folder",
        ).can_delete
        is False
    )
    assert library_api._kind_label("root") == "Library Root"
    assert library_api._kind_label("folder") == "Folder"
    assert library_api._kind_label("file") == "File"

    resolved, resolved_root = library_api._resolve_library_target(str(child), roots=[root])
    assert resolved == child.resolve()
    assert resolved_root is root
    for path, message in (
        ("", "required"),
        (str(outside), "outside"),
        (str(root_path / "missing"), "no longer exists"),
    ):
        with pytest.raises(ValidationError, match=message):
            library_api._resolve_library_target(path, roots=[root])

    broken = _BrokenFileLike()
    assert library_api._entry_size_bytes(broken) is None  # type: ignore[arg-type]
    assert library_api._entry_modified_at(broken) is None  # type: ignore[arg-type]
    assert library_api._entry_permissions_label(broken) is None  # type: ignore[arg-type]

    def broken_scandir(_path: Path):  # type: ignore[no-untyped-def]
        raise OSError("boom")

    monkeypatch.setattr(library_api.os, "scandir", broken_scandir)
    assert library_api._visible_child_count(child) is None

    monkeypatch.setattr(
        library_api.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=25, free=75),
    )
    storage = library_api._root_storage_summary(root_path)
    assert storage.total_bytes == 100
    assert storage.used_pct == 25.0
    monkeypatch.setattr(
        library_api.shutil,
        "disk_usage",
        lambda _path: (_ for _ in ()).throw(OSError("boom")),
    )
    assert library_api._root_storage_summary(root_path).total_bytes is None


@pytest.mark.asyncio
async def test_library_unmatched_stats_and_manual_match_routes(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "library"
    root_path.mkdir()
    unmatched_path = root_path / "Unmatched.cbz"
    matched_path = root_path / "Matched.cbz"
    unmatched_path.write_bytes(b"unmatched")
    matched_path.write_bytes(b"matched")

    root = LibraryRoot(name="Root", path=str(root_path), enabled=True)
    db_session.add(root)
    await db_session.flush()
    unmatched_file = LibraryFile(
        library_root_id=root.id,
        file_path=str(unmatched_path),
        file_name=unmatched_path.name,
        file_size=unmatched_path.stat().st_size,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime.now(tz=UTC),
        match_confidence=MatchConfidence.UNMATCHED,
    )
    matched_file = LibraryFile(
        library_root_id=root.id,
        file_path=str(matched_path),
        file_name=matched_path.name,
        file_size=matched_path.stat().st_size,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime.now(tz=UTC),
        match_confidence=MatchConfidence.HIGH,
    )
    db_session.add_all([unmatched_file, matched_file])
    await db_session.flush()

    unmatched = await library_api.list_unmatched(object(), db_session, limit=1, offset=0)
    assert unmatched.total == 1
    assert unmatched.has_more is False
    assert unmatched.items[0].file_name == unmatched_path.name

    stats = await library_api.library_stats(object(), db_session)
    assert stats.total_files == 2
    assert stats.matched_files == 1
    assert stats.unmatched_files == 1
    assert stats.roots_count == 1
    assert stats.format_counts[FileFormat.CBZ.value] == 2

    class FakeMatchingService:
        async def manual_match(
            self,
            _session: AsyncSession,
            library_file_id: int,
            issue_id: int,
        ) -> LibraryFile:
            assert library_file_id == unmatched_file.id
            assert issue_id == 99
            return unmatched_file

    monkeypatch.setattr(
        "pullbox.composition.services.build_matching_service",
        lambda: FakeMatchingService(),
    )
    matched = await library_api.manual_match(
        ManualMatchRequest(library_file_id=unmatched_file.id, issue_id=99),
        object(),
        db_session,
    )
    assert matched.id == unmatched_file.id


@pytest.mark.asyncio
async def test_library_browser_validation_and_wrapper_branches(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "library"
    root_path.mkdir()
    source = root_path / "Comic 001.cbz"
    source.write_bytes(b"comic")
    duplicate = root_path / "Duplicate.cbz"
    duplicate.write_bytes(b"dupe")
    convertible = root_path / "Convert Me.cbr"
    convertible.write_bytes(b"rar-ish")
    convertible.with_suffix(".cbz").write_bytes(b"already")
    folder = root_path / "Folder"
    folder.mkdir()

    root = LibraryRoot(name="Root", path=str(root_path), enabled=True)
    db_session.add(root)
    await db_session.flush()
    db_session.add_all(
        [
            LibraryFile(
                library_root_id=root.id,
                file_path=str(source),
                file_name=source.name,
                file_size=source.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.fromtimestamp(source.stat().st_mtime, tz=UTC),
                match_confidence=MatchConfidence.UNMATCHED,
                has_comicinfo=False,
            ),
            LibraryFile(
                library_root_id=root.id,
                file_path=str(convertible),
                file_name=convertible.name,
                file_size=convertible.stat().st_size,
                file_format=FileFormat.CBR,
                file_modified_at=datetime.fromtimestamp(convertible.stat().st_mtime, tz=UTC),
                match_confidence=MatchConfidence.UNMATCHED,
                has_comicinfo=False,
            ),
        ]
    )
    await db_session.flush()

    with pytest.raises(ValidationError, match="Library roots cannot be renamed"):
        await library_api._validate_library_browser_rename(
            db_session,
            body=LibraryBrowserManualRenameRequest(path=str(root_path), proposed_name="Renamed"),
        )
    with pytest.raises(ValidationError, match="Enter a valid"):
        await library_api._validate_library_browser_rename(
            db_session,
            body=LibraryBrowserManualRenameRequest(path=str(source), proposed_name="<>"),
        )
    with pytest.raises(ValidationError, match="filesystem limit"):
        await library_api._validate_library_browser_rename(
            db_session,
            body=LibraryBrowserManualRenameRequest(
                path=str(source),
                proposed_name=f"{'a' * 260}.cbz",
            ),
        )
    with pytest.raises(ValidationError, match="Name is unchanged"):
        await library_api._validate_library_browser_rename(
            db_session,
            body=LibraryBrowserManualRenameRequest(path=str(source), proposed_name=source.name),
        )
    with pytest.raises(ValidationError, match="already exists"):
        await library_api._validate_library_browser_rename(
            db_session,
            body=LibraryBrowserManualRenameRequest(path=str(source), proposed_name=duplicate.name),
        )
    case_only = await library_api._validate_library_browser_rename(
        db_session,
        body=LibraryBrowserManualRenameRequest(path=str(source), proposed_name="comic 001.cbz"),
    )
    assert case_only.target_path == str(root_path / "comic 001.cbz")

    with pytest.raises(ValidationError, match="cannot be converted"):
        await library_api._validate_library_browser_convert(
            db_session,
            body=LibraryBrowserConvertRequest(path=str(source)),
        )
    with pytest.raises(ValidationError, match="already exists"):
        await library_api._validate_library_browser_convert(
            db_session,
            body=LibraryBrowserConvertRequest(path=str(convertible)),
        )
    with pytest.raises(ValidationError, match="Library roots cannot be deleted"):
        await library_api.delete_library_browser_entry(
            LibraryBrowserDeleteRequest(path=str(root_path)),
            object(),
            db_session,
        )

    async def fake_rename(_session, *, source, target, kind):  # type: ignore[no-untyped-def]
        return SimpleNamespace(kind=kind, source_path=str(source), target_path=str(target))

    monkeypatch.setattr(library_api, "rename_library_entry", fake_rename)
    renamed = await library_api.rename_library_browser_entry(
        LibraryBrowserManualRenameRequest(path=str(source), proposed_name="Comic 002.cbz"),
        object(),
        db_session,
    )
    assert renamed["status"] == "ok"
    assert renamed["target_path"] == str(root_path / "Comic 002.cbz")

    async def fake_delete(_session, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            kind=kwargs["kind"],
            mode="file",
            source_path=str(kwargs["target"]),
            deleted_via_trash=False,
            result_path=None,
            managed_files_deleted=1,
            referenced_files_detached=0,
        )

    monkeypatch.setattr(library_api, "delete_library_entry", fake_delete)
    deleted = await library_api.delete_library_browser_entry(
        LibraryBrowserDeleteRequest(path=str(source)),
        object(),
        db_session,
    )
    assert deleted["status"] == "ok"
    assert deleted["source_path"] == str(source.resolve())

    convertible.with_suffix(".cbz").unlink()

    async def fake_convert_trash_dir(_session) -> Path:  # type: ignore[no-untyped-def]
        return tmp_path / ".trash"

    monkeypatch.setattr(library_api, "_load_library_convert_trash_dir", fake_convert_trash_dir)

    async def fake_convert(_session, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            kind="file",
            source_path=str(kwargs["source"]),
            target_path=str(kwargs["source"].with_suffix(".cbz")),
            original_trash_path=str(kwargs["trash_dir"] / kwargs["trash_relative_path"]),
        )

    monkeypatch.setattr(library_api, "convert_library_file", fake_convert)
    converted = await library_api.convert_library_browser_entry(
        LibraryBrowserConvertRequest(path=str(convertible)),
        object(),
        db_session,
    )
    assert converted["status"] == "ok"
    assert converted["target_path"] == str(convertible.with_suffix(".cbz"))
    assert "Root" in str(converted["original_trash_path"])
