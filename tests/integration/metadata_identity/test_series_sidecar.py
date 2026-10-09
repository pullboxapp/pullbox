"""Saved canonical series output uses the existing SQLite/PostgreSQL admission guards."""

import asyncio
import json
import threading
from datetime import UTC, datetime

import pytest

from pullbox.models import Issue, LibraryFile, LibraryRoot, Series
from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
from pullbox.models.library import FileFormat
from pullbox.models.metadata_identity import SeriesExternalIdentity
from pullbox.services import series_sidecar


@pytest.fixture
async def managed_series(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    folder = tmp_path / "Series"
    folder.mkdir()
    comic = folder / "Issue 001.cbz"
    comic.write_bytes(b"Archive must remain untouched")
    async with factory.begin() as session:
        root = LibraryRoot(name="Managed", path=str(tmp_path))
        session.add(root)
        await session.flush()
        series = Series(
            title="Verified multi-provider series",
            sort_title="verified",
            comicvine_id=42,
            path=str(folder),
            library_root_id=root.id,
        )
        session.add(series)
        await session.flush()
        for namespace, external in (("comicvine", "42"), ("metron", "12"), ("gcd", "30")):
            session.add(
                SeriesExternalIdentity(
                    series_id=series.id,
                    identity_namespace=namespace,
                    external_id=external,
                    verification_state="verified",
                    evidence_kind="user_selection",
                )
            )
        issue = Issue(series_id=series.id, issue_number=1)
        session.add(issue)
        await session.flush()
        session.add(
            LibraryFile(
                file_path=str(comic),
                file_name=comic.name,
                file_size=comic.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(UTC),
                library_root_id=root.id,
                issue_id=issue.id,
            )
        )
        series_id, root_id = series.id, root.id
    return factory, series_id, root_id, folder, comic


async def prepare(factory, series_id):
    async with factory() as session:
        return await series_sidecar.prepare_series_sidecar(session, series_id)


async def write(factory, series_id, key):
    async with factory() as session:
        return await series_sidecar.write_series_sidecar(session, series_id, key)


async def test_same_verified_provider_identities_and_no_archive_mutation(managed_series):
    factory, series_id, _, folder, comic = managed_series
    before = comic.read_bytes(), comic.stat()
    plan = await prepare(factory, series_id)
    result = await write(factory, series_id, plan.preview.review_key)
    assert result.written == 1
    data = json.loads((folder / "series.json").read_bytes())
    assert data["pullbox"]["snapshot"] == plan.preview.snapshot.model_dump(mode="json")
    assert {ref["namespace"] for ref in data["pullbox"]["snapshot"]["identities"]} == {
        "comicvine",
        "metron",
        "gcd",
    }
    assert set(data["pullbox"]["links"]) == {"comicvine", "metron", "gcd"}
    assert (comic.read_bytes(), comic.stat()) == before


async def test_passive_release_provenance_reuses_existing_sidecar_without_archive_mutation(
    managed_series,
):
    from pullbox.core.metadata_identity import ExternalIdentityRef, IdentityNamespace
    from pullbox.core.metadata_identity import MetadataEntityKind as Kind
    from pullbox.schemas.metadata_snapshot import (
        FieldOrigin,
        MetadataSnapshot,
        MetadataValues,
        PassiveReleaseOrigin,
    )
    from pullbox.schemas.metadata_sources import MetadataDomain
    from pullbox.services.metadata_baselines import MetadataBaselineWrite, save_metadata_baselines

    factory, series_id, _, folder, comic = managed_series
    before = comic.read_bytes(), comic.stat()
    origin = PassiveReleaseOrigin(
        locg_series_id="77", release_ids=("1001", "1002"), fetched_at=datetime.now(UTC)
    )
    async with factory.begin() as session:
        series = await session.get(Series, series_id)
        session.add(
            SeriesExternalIdentity(
                series_id=series_id,
                identity_namespace="locg",
                external_id="77",
                verification_state="verified",
                evidence_kind="user_selection",
            )
        )
        await session.flush()
        claims = (("comicvine", "42"), ("metron", "12"), ("gcd", "30"), ("locg", "77"))
        snapshot = MetadataSnapshot(
            entity_kind=Kind.SERIES,
            identities=tuple(
                ExternalIdentityRef(IdentityNamespace(namespace), Kind.SERIES, identifier)
                for namespace, identifier in claims
            ),
            values=MetadataValues(title=series.title, sort_title=series.sort_title, volume="2"),
            origins=(
                FieldOrigin(
                    field="volume",
                    domain=MetadataDomain.CORE,
                    observed_at=datetime.now(UTC),
                    passive_release=origin,
                ),
            ),
        )
        await save_metadata_baselines(session, [MetadataBaselineWrite(series_id, snapshot, 0)])
    plan = await prepare(factory, series_id)
    result = await write(factory, series_id, plan.preview.review_key)
    assert result.written == 1
    data = json.loads((folder / "series.json").read_bytes())
    saved = data["pullbox"]["snapshot"]
    assert saved["values"]["volume"] == "2"
    provenance = next(item for item in saved["origins"] if item["field"] == "volume")
    assert provenance["passive_release"] == origin.model_dump(mode="json")
    assert provenance["source"] is None
    assert (comic.read_bytes(), comic.stat()) == before


async def test_active_import_blocks_sidecar_and_cleans_staging(managed_series):
    factory, series_id, _, folder, comic = managed_series
    plan = await prepare(factory, series_id)
    async with factory.begin() as session:
        session.add(
            ImportJob(
                source_path=str(folder),
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.IMPORTING,
            )
        )
    result = await write(factory, series_id, plan.preview.review_key)
    assert result.written == 0
    assert result.targets[0].action == "blocked"
    assert "import" in result.targets[0].reason.lower()
    assert not (folder / "series.json").exists()
    assert not list(folder.glob(".pullbox-series-*.tmp"))
    assert comic.read_bytes() == b"Archive must remain untouched"


async def test_changed_root_policy_invalidates_approval(managed_series):
    factory, series_id, root_id, folder, _ = managed_series
    plan = await prepare(factory, series_id)
    async with factory.begin() as session:
        (await session.get(LibraryRoot, root_id)).allow_managed_writes = False
    with pytest.raises(ValueError, match="changed after preview"):
        await write(factory, series_id, plan.preview.review_key)
    assert not (folder / "series.json").exists()
    assert not list(folder.glob(".pullbox-series-*.tmp"))


async def test_cancel_during_stage_joins_short_write_and_cleans_staging(
    managed_series, monkeypatch
):
    factory, series_id, _, folder, comic = managed_series
    plan = await prepare(factory, series_id)
    entered, release = threading.Event(), threading.Event()
    original_stage = series_sidecar._stage

    def blocked_stage(target):
        entered.set()
        assert release.wait(10), "Test must release sidecar worker"
        return original_stage(target)

    monkeypatch.setattr(series_sidecar, "_stage", blocked_stage)
    task = asyncio.create_task(write(factory, series_id, plan.preview.review_key))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(folder.glob(".pullbox-series-*.tmp"))
    assert json.loads((folder / "series.json").read_bytes())["comicid"] == 42
    assert comic.read_bytes() == b"Archive must remain untouched"
