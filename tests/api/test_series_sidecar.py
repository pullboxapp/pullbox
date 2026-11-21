"""Explicit sidecar output preserves reference libraries and existing user data."""

import json
import os
import sys
from datetime import UTC, datetime

import pytest

from pullbox.models import Issue, LibraryFile, LibraryRoot, Series
from pullbox.models.library import FileFormat, LibraryFileStorageMode
from pullbox.models.metadata_identity import SeriesExternalIdentity
from tests.api.test_metadata_sources_api import csrf

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest_plugins = ["conftest_security"]


@pytest.fixture
async def sidecar_target(authenticated_client, sec_db, tmp_path):
    root_path = tmp_path / "managed"
    root_path.mkdir()
    folder = root_path / "Series"
    folder.mkdir()
    async with sec_db.begin() as session:
        root = LibraryRoot(name="Managed", path=str(root_path))
        session.add(root)
        await session.flush()
        series = Series(
            title="Test series",
            sort_title="test series",
            comicvine_id=42,
            path=str(folder),
            library_root_id=root.id,
            issue_count=3,
            alternate_names=["Alternate title"],
        )
        session.add(series)
        await session.flush()
        session.add(
            SeriesExternalIdentity(
                series_id=series.id,
                identity_namespace="comicvine",
                external_id="42",
                verification_state="verified",
                evidence_kind="user_selection",
            )
        )
        series_id = series.id
    return series_id, folder, root_path


def url(series_id):
    return f"/api/v1/series/{series_id}/sidecar"


async def preview(client, series_id):
    return await client.post(url(series_id) + "/preview", json={}, headers=csrf(client))


async def write(client, series_id, data):
    return await client.post(
        url(series_id) + "/write",
        json={"review_key": data["review_key"]},
        headers=csrf(client),
    )


@pytest.fixture
def provider_refresh(monkeypatch):
    from pullbox.services import metadata_series_refresh

    async def refresh(session, series_id):
        series = await session.get(Series, series_id)
        series.title = "Refreshed title"
        return series

    monkeypatch.setattr(metadata_series_refresh, "refresh_series_from_sources", refresh)


async def test_refresh_updates_managed_split_but_preserves_referenced_folder(
    authenticated_client, sec_db, sidecar_target, provider_refresh
):
    from pullbox.models.config import SystemConfig

    series_id, folder, root = sidecar_target
    split, reference = root / "Split", root / "Keep"
    (folder / "series.json").write_text('{"metadata":{"comicid":42},"custom":"keep"}')
    async with sec_db.begin() as session:
        session.add(SystemConfig(key="series_sidecars_on_refresh", value="true", value_type="bool"))
        series = await session.get(Series, series_id)
        for n, (path, mode) in enumerate(
            [
                (split, LibraryFileStorageMode.MANAGED),
                (reference, LibraryFileStorageMode.REFERENCED),
            ],
            start=1,
        ):
            path.mkdir()
            comic = path / "issue.cbz"
            comic.write_bytes(b"original comic")
            issue = Issue(series_id=series_id, issue_number=n)
            session.add(issue)
            await session.flush()
            session.add(
                LibraryFile(
                    file_path=str(comic),
                    file_name=comic.name,
                    file_size=comic.stat().st_size,
                    file_format=FileFormat.CBZ,
                    file_modified_at=datetime.now(UTC),
                    library_root_id=series.library_root_id,
                    issue_id=issue.id,
                    storage_mode=mode,
                )
            )
    result = await authenticated_client.post(
        f"/api/v1/series/{series_id}/refresh", headers=csrf(authenticated_client)
    )
    assert result.status_code == 200, result.text
    assert result.json()["title"] == "Refreshed title"
    assert (split / "series.json").is_file()
    primary = json.loads((folder / "series.json").read_bytes())
    assert primary["custom"] == "keep"
    assert primary["metadata"]["name"] == "Refreshed title"
    assert primary["pullbox"] == json.loads((split / "series.json").read_bytes())["pullbox"]
    assert not (reference / "series.json").exists()
    assert all(
        (path / "issue.cbz").read_bytes() == b"original comic" for path in (reference, split)
    )
    assert result.headers["X-Pullbox-Series-Json"] == "review"


@pytest.mark.parametrize("scenario", ["default", "api-key", "refresh-failed", "unsafe-json"])
async def test_refresh_sidecar_boundaries(
    authenticated_client,
    unauthenticated_client,
    sec_api_key,
    sidecar_target,
    sec_db,
    provider_refresh,
    monkeypatch,
    scenario,
):
    from pullbox.models.config import SystemConfig
    from pullbox.services import metadata_series_refresh

    series_id, folder, _ = sidecar_target
    client, headers = authenticated_client, csrf(authenticated_client)
    sidecar = folder / "series.json"
    if scenario != "default":
        async with sec_db.begin() as session:
            session.add(
                SystemConfig(key="series_sidecars_on_refresh", value="true", value_type="bool")
            )
    if scenario == "api-key":
        client, headers = unauthenticated_client, {"X-API-Key": sec_api_key}
    if scenario == "refresh-failed":

        async def fail(session, series_id):
            (await session.get(Series, series_id)).title = "Not committed"
            raise metadata_series_refresh.SeriesRefreshError("Catalog needs review")

        monkeypatch.setattr(metadata_series_refresh, "refresh_series_from_sources", fail)
    if scenario == "unsafe-json":
        sidecar.write_bytes(b"invalid original")
    result = await client.post(f"/api/v1/series/{series_id}/refresh", headers=headers)
    assert result.status_code == (409 if scenario == "refresh-failed" else 200), result.text
    async with sec_db() as session:
        title = (await session.get(Series, series_id)).title
        assert title == ("Test series" if scenario == "refresh-failed" else "Refreshed title")
    if scenario == "unsafe-json":
        assert result.headers["X-Pullbox-Series-Json"] == "review"
        assert sidecar.read_bytes() == b"invalid original"
    else:
        assert "X-Pullbox-Series-Json" not in result.headers
        assert not sidecar.exists()


async def test_preview_then_write_canonical_sidecar_without_issue_catalog_reads(
    authenticated_client, sidecar_target, monkeypatch
):
    series_id, folder, _ = sidecar_target
    (folder / "series.json").write_text(
        json.dumps({"version": "1.0.2", "metadata": {"comicid": 42}, "custom": {"note": "Keep me"}})
    )
    before = (folder / "series.json").read_bytes()
    from pullbox.services import metadata_series_refresh_state
    from pullbox.services.metadata_discovery import MetadataSourceRegistry

    async def forbidden(*args, **kwargs):
        raise AssertionError("Writing saved series metadata must not fetch providers")

    monkeypatch.setattr(MetadataSourceRegistry, "series", forbidden)
    monkeypatch.setattr(metadata_series_refresh_state, "read_issue_credits", forbidden)
    result = await preview(authenticated_client, series_id)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["ready"] and data["targets"][0]["action"] == "update"
    assert (folder / "series.json").read_bytes() == before
    result = await write(authenticated_client, series_id, data)
    assert result.status_code == 200, result.text
    assert result.json()["written"] == 1
    stored = json.loads((folder / "series.json").read_bytes())
    assert stored["custom"] == {"note": "Keep me"}
    assert stored["version"] == "1.0.2"
    assert stored["metadata"]["comicid"] == 42
    assert stored["metadata"]["name"] == "Test series"
    assert stored["pullbox"]["schema_version"] == 1
    assert stored["pullbox"]["snapshot"]["values"]["issue_count"] == 3
    assert stored["pullbox"]["aliases"] == ["Alternate title"]
    assert "issues" not in stored["pullbox"]
    repeat = await preview(authenticated_client, series_id)
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["targets"][0]["action"] == "unchanged"


async def test_review_key_rejects_user_edit_and_sidecar_edit(
    authenticated_client, sidecar_target, sec_db
):
    series_id, folder, _ = sidecar_target
    result = await preview(authenticated_client, series_id)
    assert result.status_code == 200, result.text
    async with sec_db.begin() as session:
        (await session.get(Series, series_id)).title = "New user title"
    rejected = await write(authenticated_client, series_id, result.json())
    assert rejected.status_code == 409, rejected.text
    assert not (folder / "series.json").exists()
    result = await preview(authenticated_client, series_id)
    assert result.status_code == 200, result.text
    (folder / "series.json").write_text('{"personal":"New note"}')
    rejected = await write(authenticated_client, series_id, result.json())
    assert rejected.status_code == 409, rejected.text
    assert (folder / "series.json").read_text() == '{"personal":"New note"}'


async def test_reference_only_folder_is_not_a_write_target(
    authenticated_client, sidecar_target, sec_db
):
    series_id, folder, _ = sidecar_target
    comic = folder / "comic.cbz"
    comic.write_bytes(b"untouched source")
    async with sec_db.begin() as session:
        series = await session.get(Series, series_id)
        issue = Issue(series_id=series_id, issue_number=1)
        session.add(issue)
        await session.flush()
        session.add(
            LibraryFile(
                file_path=str(comic),
                file_name=comic.name,
                file_size=comic.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(UTC),
                library_root_id=series.library_root_id,
                issue_id=issue.id,
                storage_mode=LibraryFileStorageMode.REFERENCED,
            )
        )
    result = await preview(authenticated_client, series_id)
    assert result.status_code == 200, result.text
    assert not result.json()["ready"]
    assert result.json()["targets"][0]["action"] == "blocked"
    assert not (folder / "series.json").exists()
    assert comic.read_bytes() == b"untouched source"


@pytest.mark.parametrize(
    "kind",
    [
        "invalid_json",
        "wrong_id",
        "symlink",
        "hardlink",
        "private",
        "path",
        "unregistered",
        "readonly",
        "future_version",
        "duplicate_keys",
    ],
)
async def test_unsafe_or_ambiguous_sidecar_target_remains_unchanged(
    authenticated_client, sidecar_target, tmp_path, sec_db, kind
):
    series_id, folder, _ = sidecar_target
    target = folder / "series.json"
    if kind == "invalid_json":
        target.write_text("{bad")
    elif kind == "wrong_id":
        target.write_text('{"ComicID":99}')
    elif kind == "private":
        target.write_text('{"api_key":"must-not-copy"}')
    elif kind == "path":
        target.write_text('{"source_path":"/private/library"}')
    elif kind == "future_version":
        target.write_text('{"pullbox":{"schema_version":99}}')
    elif kind == "duplicate_keys":
        target.write_text('{"comicid":99,"comicid":42}')
    elif kind in {"symlink", "hardlink"}:
        outside = tmp_path / "original.json"
        outside.write_text('{"personal":"unchanged"}')
        if kind == "symlink":
            target.symlink_to(outside)
        else:
            os.link(outside, target)
    elif kind == "unregistered":
        (folder / "Other series 001.cbz").write_bytes(b"untouched comic")
    else:
        async with sec_db.begin() as session:
            series = await session.get(Series, series_id)
            (await session.get(LibraryRoot, series.library_root_id)).allow_managed_writes = False
    before = target.read_bytes() if target.exists() else None
    result = await preview(authenticated_client, series_id)
    assert result.status_code == 200, result.text
    assert not result.json()["ready"]
    assert result.json()["targets"][0]["action"] == "blocked"
    assert result.json()["targets"][0]["reason"]
    assert (target.read_bytes() if target.exists() else None) == before


async def test_split_managed_folders_get_same_compiled_metadata(
    authenticated_client, sidecar_target, sec_db
):
    series_id, folder, root_path = sidecar_target
    split = root_path / "Split"
    split.mkdir()
    comic = split / "comic.cbz"
    comic.write_bytes(b"page bytes")
    async with sec_db.begin() as session:
        series = await session.get(Series, series_id)
        issue = Issue(series_id=series_id, issue_number=1)
        session.add(issue)
        await session.flush()
        session.add(
            LibraryFile(
                file_path=str(comic),
                file_name=comic.name,
                file_size=comic.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(UTC),
                library_root_id=series.library_root_id,
                issue_id=issue.id,
                storage_mode=LibraryFileStorageMode.MANAGED,
            )
        )
    result = await preview(authenticated_client, series_id)
    assert result.status_code == 200, result.text
    assert len(result.json()["targets"]) == 2
    result = await write(authenticated_client, series_id, result.json())
    assert result.status_code == 200, result.text
    assert result.json()["written"] == 2
    assert (
        json.loads((folder / "series.json").read_bytes())["pullbox"]
        == json.loads((split / "series.json").read_bytes())["pullbox"]
    )
    assert comic.read_bytes() == b"page bytes"


async def test_sidecar_requires_operator_csrf_and_never_accepts_paths(
    authenticated_client, unauthenticated_client, sec_api_key, sidecar_target
):
    series_id, _, _ = sidecar_target
    endpoint = url(series_id) + "/write"
    body = {"review_key": "a" * 64}
    assert (await authenticated_client.post(endpoint, json=body)).status_code == 403
    assert (await unauthenticated_client.post(endpoint, json=body)).status_code in {401, 403}
    assert (
        await unauthenticated_client.post(endpoint, json=body, headers={"X-API-Key": sec_api_key})
    ).status_code in {401, 403}
    assert (
        await authenticated_client.post(
            endpoint,
            json={**body, "path": "/outside/series.json"},
            headers=csrf(authenticated_client),
        )
    ).status_code == 422


async def test_publish_failure_preserves_previous_sidecar(
    authenticated_client, sidecar_target, monkeypatch
):
    from pullbox.services import series_sidecar

    series_id, folder, _ = sidecar_target
    target = folder / "series.json"
    target.write_text('{"custom":"old value"}')
    before = target.read_bytes()
    result = await preview(authenticated_client, series_id)
    assert result.status_code == 200, result.text

    def fail(*args):
        raise OSError("simulated full disk")

    monkeypatch.setattr(series_sidecar, "_stage", fail)
    result = await write(authenticated_client, series_id, result.json())
    assert result.status_code == 200, result.text
    assert result.json()["written"] == 0
    assert result.json()["targets"][0]["action"] == "blocked"
    assert target.read_bytes() == before
    assert not list(folder.glob(".pullbox-series-*.tmp"))


@pytest.mark.parametrize("kind", ["shared_series_path", "readonly_sidecar", "case_collision"])
async def test_shared_and_protected_sidecars_are_not_replaced(
    authenticated_client, sidecar_target, sec_db, kind
):
    series_id, folder, _ = sidecar_target
    if kind == "shared_series_path":
        async with sec_db.begin() as session:
            session.add(Series(title="Other series", sort_title="other", path=str(folder)))
    elif kind == "readonly_sidecar":
        (folder / "series.json").write_text('{"personal":"read only"}')
        (folder / "series.json").chmod(0o444)
    else:
        (folder / "Series.JSON").write_text('{"personal":"case collision"}')
    result = await preview(authenticated_client, series_id)
    assert result.status_code == 200, result.text
    assert not result.json()["ready"]
    assert result.json()["targets"][0]["action"] == "blocked"
    assert not list(folder.glob(".pullbox-series-*.tmp"))


async def test_existing_case_variants_and_custom_pullbox_keys_stay_coherent(
    authenticated_client, sidecar_target
):
    series_id, folder, _ = sidecar_target
    result = await preview(authenticated_client, series_id)
    assert result.status_code == 200, result.text
    assert (await write(authenticated_client, series_id, result.json())).status_code == 200
    path = folder / "series.json"
    data = json.loads(path.read_bytes())
    data["pullbox"]["custom_note"] = "Keep this as well"
    data["Metadata"] = {"ComicID": 42, "Name": "Old title", "issue_count": 4, "year": 1986}
    path.write_text(json.dumps(data))
    result = await preview(authenticated_client, series_id)
    assert result.status_code == 200, result.text
    result = await write(authenticated_client, series_id, result.json())
    assert result.status_code == 200, result.text
    stored = json.loads(path.read_bytes())
    assert stored["pullbox"]["custom_note"] == "Keep this as well"
    assert stored["Metadata"]["Name"] == "Test series"
    assert stored["Metadata"]["issue_count"] == 3
    assert stored["Metadata"]["year"] is None


async def test_new_unregistered_comic_during_staging_blocks_publication(
    authenticated_client, sidecar_target, monkeypatch
):
    from pullbox.services import series_sidecar

    series_id, folder, _ = sidecar_target
    original_stage = series_sidecar._stage

    def changed_folder(target):
        result = original_stage(target)
        (folder / "Other series 001.cbz").write_bytes(b"Do not label this folder")
        return result

    result = await preview(authenticated_client, series_id)
    assert result.status_code == 200, result.text
    monkeypatch.setattr(series_sidecar, "_stage", changed_folder)
    result = await write(authenticated_client, series_id, result.json())
    assert result.status_code == 200, result.text
    assert result.json()["written"] == 0
    assert not (folder / "series.json").exists()
    assert not list(folder.glob(".pullbox-series-*.tmp"))


async def test_other_series_beneath_target_folder_blocks_single_series_label(
    authenticated_client, sidecar_target, sec_db
):
    series_id, folder, _ = sidecar_target
    nested = folder / "Another series"
    nested.mkdir()
    async with sec_db.begin() as session:
        session.add(Series(title="Other series", sort_title="other", path=str(nested)))
    result = await preview(authenticated_client, series_id)
    assert result.status_code == 200, result.text
    assert not result.json()["ready"]
    assert result.json()["targets"][0]["action"] == "blocked"


async def test_private_saved_artwork_url_is_not_written_into_sidecar(
    authenticated_client, sidecar_target, sec_db
):
    series_id, folder, _ = sidecar_target
    async with sec_db.begin() as session:
        (
            await session.get(Series, series_id)
        ).cover_url = "https://provider.example/cover?api_key=private-example"
    result = await preview(authenticated_client, series_id)
    assert result.status_code == 409, result.text
    assert "private-example" not in result.text
    assert not (folder / "series.json").exists()
