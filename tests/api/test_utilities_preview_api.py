"""Focused API coverage for wave 2 utilities preview flows."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: TC002

from pullbox.models.config import SystemConfig
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series, SeriesStatus
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest_plugins = ["conftest_security"]

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-utilities-preview")


def _csrf_header_for(client) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """Build a valid CSRF header from the authenticated session cookie."""
    token = client.cookies.get(SESSION_COOKIE_NAME) or ""
    csrf = AuthService.get_csrf_token_from_session(token) or ""
    return {"X-CSRF-Token": csrf}


@pytest.fixture
async def preview_paths(
    sec_db: async_sessionmaker,
    tmp_path: Path,
) -> dict[str, str]:
    """Seed a minimal library shape for utilities preview endpoints."""
    library_root_path = tmp_path / "library"
    folder_path = library_root_path / "batman-folder"
    folder_path.mkdir(parents=True)
    comic_path = folder_path / "batman-001.cbz"
    comic_path.write_text("comic")
    volume_path = folder_path / "batman-002.cbz"
    volume_path.write_text("volume")
    one_shot_path = folder_path / "ignite-prime.cbz"
    one_shot_path.write_text("one-shot")
    al15_folder_path = library_root_path / "al15-folder"
    al15_folder_path.mkdir(parents=True)
    al15_tpb_path = al15_folder_path / "al15-001.cbz"
    al15_tpb_path.write_text("tpb")
    al15_volume_path = al15_folder_path / "al15-002.cbz"
    al15_volume_path.write_text("volume")
    convert_source_path = folder_path / "detective-002.cbr"
    convert_source_path.write_text("source")
    stale_path = library_root_path / "stale.cbz"
    stale_path.write_text("stale")
    stale_series_actual_folder = library_root_path / "Nightwing [2222]"
    stale_series_actual_folder.mkdir(parents=True)
    stale_series_file_path = stale_series_actual_folder / "nightwing-001.cbz"
    stale_series_file_path.write_text("nightwing")
    alternate_root_path = tmp_path / "alternate-library"
    alternate_root_path.mkdir(parents=True)
    root_mismatch_folder = alternate_root_path / "superman-folder"
    root_mismatch_folder.mkdir(parents=True)
    root_mismatch_file_path = root_mismatch_folder / "superman-001.cbz"
    root_mismatch_file_path.write_text("superman")

    async with sec_db() as session:
        publisher = Publisher(name="DC Comics")
        root = LibraryRoot(name="Preview Library", path=str(library_root_path), enabled=True)
        alternate_root = LibraryRoot(
            name="Alternate Preview Library",
            path=str(alternate_root_path),
            enabled=True,
        )
        session.add_all([publisher, root, alternate_root])
        await session.flush()

        series = Series(
            title="Batman",
            sort_title="Batman",
            year_start=2016,
            status=SeriesStatus.CONTINUING,
            monitored=True,
            publisher_id=publisher.id,
            library_root_id=root.id,
            path=str(folder_path),
        )
        session.add(series)
        await session.flush()

        issue = Issue(
            series_id=series.id,
            issue_number=1.0,
            title="Batman #1",
            status=IssueStatus.OWNED,
        )
        volume_issue = Issue(
            series_id=series.id,
            issue_number=2.0,
            title="Broken Dreams",
            status=IssueStatus.OWNED,
            issue_type=IssueType.VOLUME,
        )
        one_shot_issue = Issue(
            series_id=series.id,
            issue_number=3.0,
            title="Zero Hour",
            status=IssueStatus.OWNED,
            issue_type=IssueType.ONE_SHOT,
        )
        session.add_all([issue, volume_issue, one_shot_issue])
        await session.flush()

        session.add(
            LibraryFile(
                issue_id=issue.id,
                library_root_id=root.id,
                file_path=str(comic_path),
                file_name=comic_path.name,
                file_size=4096,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(tz=UTC),
                match_confidence=MatchConfidence.HIGH,
            )
        )
        session.add(
            LibraryFile(
                issue_id=volume_issue.id,
                library_root_id=root.id,
                file_path=str(volume_path),
                file_name=volume_path.name,
                file_size=4096,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(tz=UTC),
                match_confidence=MatchConfidence.HIGH,
            )
        )
        session.add(
            LibraryFile(
                issue_id=one_shot_issue.id,
                library_root_id=root.id,
                file_path=str(one_shot_path),
                file_name=one_shot_path.name,
                file_size=4096,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(tz=UTC),
                match_confidence=MatchConfidence.HIGH,
            )
        )
        session.add(
            LibraryFile(
                issue_id=issue.id,
                library_root_id=root.id,
                file_path=str(folder_path / "missing.cbz"),
                file_name="missing.cbz",
                file_size=2048,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(tz=UTC),
                match_confidence=MatchConfidence.HIGH,
            )
        )
        session.add(
            SystemConfig(
                key="comic_file_template",
                value="{Series} ({Year}) #{Issue:03d}",
                value_type="string",
            )
        )
        al15_series = Series(
            title="AL15",
            sort_title="AL15",
            year_start=2021,
            status=SeriesStatus.CONTINUING,
            monitored=True,
            publisher_id=publisher.id,
            library_root_id=root.id,
            path=str(al15_folder_path),
        )
        session.add(al15_series)
        await session.flush()

        al15_tpb_issue = Issue(
            series_id=al15_series.id,
            issue_number=1.0,
            title="Volume 1",
            status=IssueStatus.OWNED,
            issue_type=IssueType.TPB,
        )
        al15_volume_issue = Issue(
            series_id=al15_series.id,
            issue_number=2.0,
            title="Broken Dreams",
            status=IssueStatus.OWNED,
            issue_type=IssueType.VOLUME,
        )
        session.add_all([al15_tpb_issue, al15_volume_issue])
        await session.flush()

        session.add_all(
            [
                LibraryFile(
                    issue_id=al15_tpb_issue.id,
                    library_root_id=root.id,
                    file_path=str(al15_tpb_path),
                    file_name=al15_tpb_path.name,
                    file_size=4096,
                    file_format=FileFormat.CBZ,
                    file_modified_at=datetime.now(tz=UTC),
                    match_confidence=MatchConfidence.HIGH,
                ),
                LibraryFile(
                    issue_id=al15_volume_issue.id,
                    library_root_id=root.id,
                    file_path=str(al15_volume_path),
                    file_name=al15_volume_path.name,
                    file_size=4096,
                    file_format=FileFormat.CBZ,
                    file_modified_at=datetime.now(tz=UTC),
                    match_confidence=MatchConfidence.HIGH,
                ),
            ]
        )

        session.add(
            SystemConfig(
                key="non_standard_file_template",
                value="{Series} ({Year}) {Type} {Volume:02d} - {Title}",
                value_type="string",
            )
        )
        session.add(
            SystemConfig(
                key="single_non_standard_file_template",
                value="{Series} ({Year}) {Type} - {Title}",
                value_type="string",
            )
        )
        stale_series = Series(
            title="Nightwing",
            sort_title="Nightwing",
            comicvine_id=2222,
            year_start=2016,
            status=SeriesStatus.CONTINUING,
            monitored=True,
            publisher_id=publisher.id,
            library_root_id=root.id,
            path=str(library_root_path / "nightwing-old-folder"),
        )
        root_mismatch_series = Series(
            title="Superman",
            sort_title="Superman",
            year_start=2018,
            status=SeriesStatus.CONTINUING,
            monitored=True,
            publisher_id=publisher.id,
            library_root_id=root.id,
            path=str(root_mismatch_folder),
        )
        session.add_all([stale_series, root_mismatch_series])
        await session.flush()

        stale_series_issue = Issue(
            series_id=stale_series.id,
            issue_number=1.0,
            title="Nightwing #1",
            status=IssueStatus.OWNED,
        )
        root_mismatch_issue = Issue(
            series_id=root_mismatch_series.id,
            issue_number=1.0,
            title="Superman #1",
            status=IssueStatus.OWNED,
        )
        session.add_all([stale_series_issue, root_mismatch_issue])
        await session.flush()

        session.add_all(
            [
                LibraryFile(
                    issue_id=stale_series_issue.id,
                    library_root_id=root.id,
                    file_path=str(stale_series_file_path),
                    file_name=stale_series_file_path.name,
                    file_size=4096,
                    file_format=FileFormat.CBZ,
                    file_modified_at=datetime.now(tz=UTC),
                    match_confidence=MatchConfidence.HIGH,
                ),
                LibraryFile(
                    issue_id=root_mismatch_issue.id,
                    library_root_id=root.id,
                    file_path=str(root_mismatch_file_path),
                    file_name=root_mismatch_file_path.name,
                    file_size=4096,
                    file_format=FileFormat.CBZ,
                    file_modified_at=datetime.now(tz=UTC),
                    match_confidence=MatchConfidence.HIGH,
                ),
            ]
        )
        await session.commit()

    return {
        "library_root": str(library_root_path),
        "alternate_root": str(alternate_root_path),
        "folder": str(folder_path),
        "al15_folder": str(al15_folder_path),
        "file": str(comic_path),
        "volume_file": str(volume_path),
        "one_shot_file": str(one_shot_path),
        "al15_tpb_file": str(al15_tpb_path),
        "al15_volume_file": str(al15_volume_path),
        "convert_source": str(convert_source_path),
        "stale_series_folder": str(stale_series_actual_folder),
        "root_mismatch_folder": str(root_mismatch_folder),
    }


@pytest.mark.asyncio
async def test_convert_preview_returns_output_paths_with_target_extension(
    authenticated_client,
    preview_paths: dict[str, str],
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.post(
        "/api/v1/utilities/convert/preview",
        json={
            "source_format": "cbr",
            "target_format": "cbz",
            "scope": "manual",
            "file_paths": [preview_paths["convert_source"]],
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total_count"] == 1
    assert data["files"][0]["path"].endswith(".cbr")
    assert data["files"][0]["output_path"].endswith(".cbz")
    assert data["files"][0]["output_path"] != data["files"][0]["path"]


@pytest.mark.asyncio
async def test_convert_preview_rejects_files_outside_library_roots(
    authenticated_client,
    preview_paths: dict[str, str],
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    assert preview_paths["library_root"]
    outside = tmp_path / "outside-import.cbr"
    outside.write_text("outside")

    response = await authenticated_client.post(
        "/api/v1/utilities/convert/preview",
        json={
            "source_format": "cbr",
            "target_format": "cbz",
            "scope": "manual",
            "file_paths": [str(outside)],
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total_count"] == 0
    assert data["files"] == []


@pytest.mark.asyncio
async def test_mass_rename_preview_returns_folder_and_file_proposals(
    authenticated_client,
    preview_paths: dict[str, str],
) -> None:  # type: ignore[no-untyped-def]
    file_response = await authenticated_client.post(
        "/api/v1/utilities/rename/preview",
        json={"target": "files", "scope": "manual", "file_paths": [preview_paths["file"]]},
        headers=_csrf_header_for(authenticated_client),
    )
    assert file_response.status_code == 200
    file_data = file_response.json()
    assert file_data["scope"] == "manual"
    assert file_data["actionable_count"] == 1
    assert file_data["items"][0]["current_name"] == "batman-001.cbz"
    assert file_data["items"][0]["proposed_name"] == "Batman (2016) #001.cbz"
    assert file_data["items"][0]["template_key"] == "comic_file_template"

    folder_response = await authenticated_client.post(
        "/api/v1/utilities/rename/preview",
        json={"target": "folders", "scope": "manual", "file_paths": [preview_paths["folder"]]},
        headers=_csrf_header_for(authenticated_client),
    )
    assert folder_response.status_code == 200
    folder_data = folder_response.json()
    assert folder_data["scope"] == "manual"
    assert folder_data["items"][0]["proposed_name"] == "Batman (2016)"
    assert folder_data["items"][0]["status"] == "ready"


@pytest.mark.asyncio
async def test_mass_convert_preview_returns_library_candidates(
    authenticated_client,
    preview_paths: dict[str, str],
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.post(
        "/api/v1/utilities/mass-convert/preview",
        json={"scope": "library"},
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["scope"] == "library"
    assert data["item_count"] >= 1
    assert data["items"][0]["output_name"].endswith(".cbz")
    assert data["items"][0]["source_format"] in {"CBR", "CB7", "CBZ", "PDF"}


@pytest.mark.asyncio
async def test_mass_convert_preview_accepts_multiple_folders(
    authenticated_client,
    preview_paths: dict[str, str],
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.post(
        "/api/v1/utilities/mass-convert/preview",
        json={
            "scope": "folder",
            "file_paths": [preview_paths["folder"], preview_paths["library_root"]],
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["scope"] == "folder"
    assert data["item_count"] >= 1
    assert any(item["source_name"] == "batman-001.cbz" for item in data["items"])


@pytest.mark.asyncio
async def test_mass_convert_preview_rejects_folder_outside_library_roots(
    authenticated_client,
    preview_paths: dict[str, str],
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    assert preview_paths["library_root"]
    outside_folder = tmp_path / "outside"
    outside_folder.mkdir()
    (outside_folder / "outside.cbz").write_text("outside")

    response = await authenticated_client.post(
        "/api/v1/utilities/mass-convert/preview",
        json={"scope": "folder", "file_paths": [str(outside_folder)]},
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 422
    assert "outside enabled library roots" in response.text


@pytest.mark.asyncio
async def test_library_permissions_preview_counts_recursive_folder_scope(
    authenticated_client,
    preview_paths: dict[str, str],
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.post(
        "/api/v1/utilities/permissions/preview",
        json={
            "scope": "folder",
            "file_paths": [preview_paths["folder"]],
            "folder_mode": "750",
            "file_mode": "640",
            "include_folders": True,
            "include_files": True,
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["scope"] == "folder"
    assert data["folder_count"] == 1
    assert data["file_count"] >= 4
    assert data["item_count"] == data["folder_count"] + data["file_count"]
    assert any(item["name"] == "batman-001.cbz" for item in data["items"])
    assert any(item["item_type"] == "folder" for item in data["items"])


@pytest.mark.asyncio
async def test_library_permissions_preview_rejects_paths_outside_library_roots(
    authenticated_client,
    preview_paths: dict[str, str],
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    assert preview_paths["library_root"]
    outside_file = tmp_path / "outside.cbz"
    outside_file.write_text("outside")

    response = await authenticated_client.post(
        "/api/v1/utilities/permissions/preview",
        json={
            "scope": "files",
            "file_paths": [str(outside_file)],
            "folder_mode": "750",
            "file_mode": "640",
            "include_folders": False,
            "include_files": True,
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 422
    assert "outside enabled library roots" in response.text


@pytest.mark.asyncio
async def test_library_permissions_preview_respects_include_toggles(
    authenticated_client,
    preview_paths: dict[str, str],
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.post(
        "/api/v1/utilities/permissions/preview",
        json={
            "scope": "folder",
            "file_paths": [preview_paths["folder"]],
            "folder_mode": "750",
            "file_mode": "640",
            "include_folders": False,
            "include_files": True,
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["folder_count"] == 0
    assert data["file_count"] >= 4
    assert all(item["item_type"] != "folder" for item in data["items"])


@pytest.mark.asyncio
async def test_mass_rename_preview_uses_split_non_standard_templates(
    authenticated_client,
    preview_paths: dict[str, str],
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.post(
        "/api/v1/utilities/rename/preview",
        json={
            "target": "files",
            "scope": "manual",
            "file_paths": [preview_paths["volume_file"], preview_paths["one_shot_file"]],
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    data = response.json()
    items = {item["file_path"]: item for item in data["items"]}

    volume_item = items[preview_paths["volume_file"]]
    assert volume_item["template_key"] == "non_standard_file_template"
    assert volume_item["template_label"] == "Collection Template"
    assert volume_item["proposed_name"] == "Batman (2016) Vol 02 - Broken Dreams.cbz"

    one_shot_item = items[preview_paths["one_shot_file"]]
    assert one_shot_item["template_key"] == "single_non_standard_file_template"
    assert one_shot_item["template_label"] == "Single-Release Template"
    assert one_shot_item["proposed_name"] == "Batman (2016) One-Shot - Zero Hour.cbz"


@pytest.mark.asyncio
async def test_mass_rename_preview_normalizes_mixed_tpb_volume_series_to_vol(
    authenticated_client,
    preview_paths: dict[str, str],
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.post(
        "/api/v1/utilities/rename/preview",
        json={
            "target": "files",
            "scope": "manual",
            "file_paths": [preview_paths["al15_tpb_file"], preview_paths["al15_volume_file"]],
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    data = response.json()
    items = {item["file_path"]: item for item in data["items"]}

    assert (
        items[preview_paths["al15_tpb_file"]]["proposed_name"]
        == "AL15 (2021) Vol 01 - Volume 1.cbz"
    )
    assert (
        items[preview_paths["al15_volume_file"]]["proposed_name"]
        == "AL15 (2021) Vol 02 - Broken Dreams.cbz"
    )


@pytest.mark.asyncio
async def test_mass_rename_preview_supports_library_and_folder_scopes(
    authenticated_client,
    preview_paths: dict[str, str],
) -> None:  # type: ignore[no-untyped-def]
    library_response = await authenticated_client.post(
        "/api/v1/utilities/rename/preview",
        json={"target": "files", "scope": "library", "file_paths": []},
        headers=_csrf_header_for(authenticated_client),
    )

    assert library_response.status_code == 200
    library_data = library_response.json()
    assert library_data["scope"] == "library"
    assert library_data["item_count"] >= 2
    assert any(item["file_path"] == preview_paths["file"] for item in library_data["items"])

    folder_response = await authenticated_client.post(
        "/api/v1/utilities/rename/preview",
        json={
            "target": "files",
            "scope": "folder",
            "file_paths": [preview_paths["folder"], preview_paths["al15_folder"]],
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert folder_response.status_code == 200
    folder_data = folder_response.json()
    assert folder_data["scope"] == "folder"
    assert folder_data["item_count"] >= 4
    assert any(
        item["file_path"].startswith(preview_paths["folder"]) for item in folder_data["items"]
    )
    assert any(
        item["file_path"].startswith(preview_paths["al15_folder"]) for item in folder_data["items"]
    )


@pytest.mark.asyncio
async def test_mass_rename_preview_rejects_manual_paths_outside_library_roots(
    authenticated_client,
    preview_paths: dict[str, str],
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    assert preview_paths["library_root"]
    outside_file = tmp_path / "outside.cbz"
    outside_file.write_text("outside")

    response = await authenticated_client.post(
        "/api/v1/utilities/rename/preview",
        json={"target": "files", "scope": "manual", "file_paths": [str(outside_file)]},
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 422
    assert "outside enabled library roots" in response.text


@pytest.mark.asyncio
async def test_db_check_preview_returns_orphan_and_stale_findings(
    authenticated_client,
    preview_paths: dict[str, str],
) -> None:  # type: ignore[no-untyped-def]
    trash_dir = Path(preview_paths["library_root"]) / ".trash"
    trash_dir.mkdir()
    trashed_file = trash_dir / "trashed.cbz"
    trashed_file.write_text("trashed")

    response = await authenticated_client.post(
        "/api/v1/utilities/db-check/preview",
        json={
            "checks": ["orphans", "stale", "reindex"],
            "library_root": preview_paths["library_root"],
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["finding_count"] >= 3
    check_types = {finding["check_type"] for finding in data["findings"]}
    assert "orphans" in check_types
    assert "stale" in check_types
    assert "reindex" in check_types

    orphan = next(finding for finding in data["findings"] if finding["check_type"] == "orphans")
    assert orphan["suggested_action"] == "delete"
    assert orphan["allowed_actions"] == ["delete", "skip"]

    stale = next(finding for finding in data["findings"] if finding["check_type"] == "stale")
    assert stale["suggested_action"] == "add"
    assert stale["allowed_actions"] == ["add", "skip"]
    assert all(
        "/.trash/" not in (finding.get("file_path") or "")
        for finding in data["findings"]
        if finding["check_type"] == "stale"
    )


@pytest.mark.asyncio
async def test_db_check_preview_rejects_stale_scan_outside_library_roots(
    authenticated_client,
    preview_paths: dict[str, str],
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    assert preview_paths["library_root"]
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    (outside_root / "stray.cbz").write_text("stray")

    response = await authenticated_client.post(
        "/api/v1/utilities/db-check/preview",
        json={
            "checks": ["stale"],
            "library_root": str(outside_root),
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 422
    assert "outside enabled library roots" in response.text


@pytest.mark.asyncio
async def test_db_check_preview_returns_repairable_path_consistency_findings(
    authenticated_client,
    preview_paths: dict[str, str],
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.post(
        "/api/v1/utilities/db-check/preview",
        json={
            "checks": ["referential"],
            "library_root": preview_paths["library_root"],
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    data = response.json()

    stale_series = next(
        finding
        for finding in data["findings"]
        if finding["record_type"] == "series"
        and finding["suggested_action"] == "repair"
        and "stored series path" in finding["description"].lower()
    )
    assert stale_series["allowed_actions"] == ["repair", "skip"]
    assert stale_series["context"]["repair_kind"] == "series_path"
    assert stale_series["context"]["target_path"] == preview_paths["stale_series_folder"]

    root_fix = next(
        finding
        for finding in data["findings"]
        if finding["record_type"] == "library_file"
        and finding["suggested_action"] == "repair"
        and "library root" in finding["description"].lower()
    )
    assert root_fix["allowed_actions"] == ["repair", "skip"]
    assert root_fix["context"]["repair_kind"] == "library_file_root_id"
    assert root_fix["context"]["target_root_path"] == preview_paths["alternate_root"]
