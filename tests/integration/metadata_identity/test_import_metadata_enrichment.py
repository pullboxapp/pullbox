"""The normal import background entrypoint writes one reconciled XML pair."""

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock
from zipfile import ZipFile

import pytest
from defusedxml import ElementTree
from sqlalchemy import delete, select

from pullbox.core.library_file_ownership import build_managed_placement_signature
from pullbox.core.metadata_identity import MetadataEntityKind
from pullbox.core.metroninfo_schema import validate_metroninfo_xml
from pullbox.models import Issue, LibraryFile, LibraryRoot, Series
from pullbox.models.archive_metadata_publication import ArchiveMetadataPublication, PublicationState
from pullbox.models.import_job import (
    ImportedFile,
    ImportedSeries,
    ImportJobAction,
    ImportJobActionStatus,
    ImportSeriesStatus,
)
from pullbox.models.library import LibraryFileStorageMode
from pullbox.models.metadata_baseline import IssueMetadataBaseline
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.providers.base import IssueMetadata
from pullbox.schemas.metadata_snapshot import MetadataSnapshot, MetadataValues
from pullbox.services.import_service import ImportService
from tests.integration.metadata_identity.test_import_archive_publication import owned, rollback

pytestmark = pytest.mark.usefixtures("paired_import_writer_setting")


def import_service():
    service = ImportService(MagicMock(), MagicMock(), MagicMock())
    # Provider catalog hydration has already supplied the canonical DB values.
    service._metadata_service.prefetch_issue_metadata_batch = AsyncMock(return_value={})
    service._build_comicinfo_payload_for_issue = AsyncMock(
        return_value={"Series": "Example", "Number": "50-X"}
    )
    return service


async def add_comicvine_target(factory, plan, ids):
    async with factory.begin() as session:
        series = await session.get(Series, plan.target.binding.metadata.series.local_id)
        issue = await session.get(Issue, plan.target.binding.metadata.issues[0].local_id)
        series.comicvine_id, issue.comicvine_id = 123, 456
        session.add_all(
            [
                SeriesExternalIdentity(
                    series_id=series.id,
                    identity_namespace="comicvine",
                    external_id="123",
                    verification_state="verified",
                    evidence_kind="provider_result",
                ),
                IssueExternalIdentity(
                    issue_id=issue.id,
                    identity_namespace="comicvine",
                    external_id="456",
                    verification_state="verified",
                    evidence_kind="provider_result",
                ),
            ]
        )
        file = await session.get(ImportedFile, ids[1])
        file.diagnostics = {
            **file.diagnostics,
            "comicinfo_enrichment": {
                **file.diagnostics["comicinfo_enrichment"],
                "issue_cv_id": 456,
            },
        }


def full_issue(**changes):
    return IssueMetadata(
        **{
            "provider_id": "456",
            "series_provider_id": "123",
            "issue_number": 50,
            "issue_number_text": "50-X",
            "title": "Provider title",
            "description": "The missing provider summary",
            "release_date": "1996-05-01",
            "store_date": "1996-04-17",
            "cover_url": None,
            "page_count": 32,
            "comicvine_url": "https://comicvine.gamespot.com/issue/4000-456/",
            "creators": [{"name": "A Writer", "role": "writer"}],
            **changes,
        }
    )


async def test_real_job_uses_batch_fields_without_overwriting_local_metadata(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, source, plan, ids, _):
        await add_comicvine_target(factory, plan, ids)
        async with factory.begin() as session:
            issue = await session.get(Issue, plan.target.binding.metadata.issues[0].local_id)
            issue.title = "My local title"
        service = import_service()
        service._metadata_service.prefetch_issue_metadata_batch.return_value = {456: full_issue()}
        original = source.read_bytes()
        await service.recover_pending_comicinfo_enrichment(factory)
        with ZipFile(path) as archive:
            ci = ElementTree.fromstring(archive.read("ComicInfo.xml"))
            mi = ElementTree.fromstring(archive.read("MetronInfo.xml"))
            assert (
                ci.findtext("Summary") == mi.findtext("Summary") == "The missing provider summary"
            )
            assert ci.findtext("Title") == mi.findtext("Stories/Story") == "My local title"
            assert ci.findtext("Writer") == mi.findtext("Credits/Credit/Creator") == "A Writer"
            assert ci.findtext("PageCount") == mi.findtext("PageCount") == "32"
            assert archive.read("page.jpg") == b"page bytes"
        async with factory() as session:
            issue = await session.get(Issue, plan.target.binding.metadata.issues[0].local_id)
            assert issue.description == "The missing provider summary"
            assert issue.release_date == date(1996, 5, 1)
            assert issue.store_date == date(1996, 4, 17)
            baseline = await session.scalar(select(IssueMetadataBaseline))
            snapshot = MetadataSnapshot.model_validate_json(baseline.snapshot_json)
            origin = next(item for item in snapshot.origins if item.field == "description")
            assert origin.source.value == "comicvine_api"
        service._metadata_service.prefetch_issue_metadata_batch.assert_awaited_once_with([456])
        service._build_comicinfo_payload_for_issue.assert_not_awaited()
        assert source.read_bytes() == original


@pytest.mark.parametrize(
    "bad_field,bad_value",
    [
        ("provider_id", "789"),
        ("series_provider_id", "789"),
        ("issue_number_text", "50-O"),
    ],
)
async def test_wrong_batch_identity_never_enriches_or_rewrites(
    identity_probe_db, tmp_path, bad_field, bad_value
):
    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, _, plan, ids, _):
        await add_comicvine_target(factory, plan, ids)
        service = import_service()
        service._metadata_service.prefetch_issue_metadata_batch.return_value = {
            456: full_issue(**{bad_field: bad_value})
        }
        before = path.read_bytes()
        await service.recover_pending_comicinfo_enrichment(factory)
        assert path.read_bytes() == before
        async with factory() as session:
            file = await session.get(ImportedFile, ids[1])
            assert file.diagnostics["comicinfo_enrichment"]["status"] == "failed"
            issue = await session.get(Issue, plan.target.binding.metadata.issues[0].local_id)
            assert issue.description is None


@pytest.mark.parametrize("local", ["cleared", "embedded"])
async def test_batch_enrichment_preserves_local_summary_and_credit_clears(
    identity_probe_db, tmp_path, local
):
    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, _, plan, ids, _):
        await add_comicvine_target(factory, plan, ids)
        if local == "cleared":
            previous = MetadataSnapshot(
                entity_kind=MetadataEntityKind.ISSUE,
                identities=plan.issue.identities,
                values=MetadataValues(
                    description="Previously present",
                    issue_number_text="50-X",
                    credits=({"name": "Previous writer", "role": "writer"},),
                ),
            )
            async with factory.begin() as session:
                session.add(
                    IssueMetadataBaseline(
                        issue_id=plan.target.binding.metadata.issues[0].local_id,
                        revision=1,
                        snapshot_json=previous.model_dump_json(),
                    )
                )
        else:
            with ZipFile(path, "w") as archive:
                archive.writestr("page.jpg", b"page bytes")
                archive.writestr(
                    "ComicInfo.xml",
                    "<ComicInfo><Number>50-X</Number><Summary>Embedded summary</Summary>"
                    "<Writer>Local writer</Writer></ComicInfo>",
                )
            await capture_fixture_signature(factory, path, plan, ids)
        service = import_service()
        service._metadata_service.prefetch_issue_metadata_batch.return_value = {456: full_issue()}
        await service.recover_pending_comicinfo_enrichment(factory)
        with ZipFile(path) as archive:
            ci = ElementTree.fromstring(archive.read("ComicInfo.xml"))
            mi = ElementTree.fromstring(archive.read("MetronInfo.xml"))
            expected = None if local == "cleared" else "Embedded summary"
            assert ci.findtext("Summary") == mi.findtext("Summary") == expected
            if local == "cleared":
                # Empty containers distinguish a credit clear from missing metadata.
                assert ci.findtext("Writer") == ""
                assert mi.find("Credits") is not None
                assert mi.findall("Credits/Credit") == []
            else:
                assert (
                    ci.findtext("Writer") == mi.findtext("Credits/Credit/Creator") == "Local writer"
                )
            assert ci.findtext("Title") == "Provider title"
        async with factory() as session:
            file = await session.get(ImportedFile, ids[1])
            assert file.diagnostics["comicinfo_enrichment"]["status"] == "complete"
            baseline = await session.scalar(select(IssueMetadataBaseline))
            snapshot = MetadataSnapshot.model_validate_json(baseline.snapshot_json)
            for field in ("description", "credits"):
                origin = next(item for item in snapshot.origins if item.field == field)
                assert origin.user_override if local == "cleared" else origin.embedded_documents


async def test_disabled_source_is_not_prefetched_for_paired_enrichment(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, _, plan, ids, _):
        await add_comicvine_target(factory, plan, ids)
        async with factory.begin() as session:
            session.add(MetadataSourceConfig(source="comicvine_api", enabled=False, priority=20))
        service = import_service()
        service._metadata_service.prefetch_issue_metadata_batch.return_value = {456: full_issue()}
        await service.recover_pending_comicinfo_enrichment(factory)
        service._metadata_service.prefetch_issue_metadata_batch.assert_not_awaited()
        with ZipFile(path) as archive:
            assert "MetronInfo.xml" in archive.namelist()
            assert ElementTree.fromstring(archive.read("ComicInfo.xml")).findtext("Summary") is None


async def test_failed_batch_fetch_leaves_work_pending_without_rewriting(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, _, plan, ids, _):
        await add_comicvine_target(factory, plan, ids)
        service = import_service()
        service._metadata_service.prefetch_issue_metadata_batch.side_effect = TimeoutError()
        before = path.read_bytes()
        await service.recover_pending_comicinfo_enrichment(factory)
        assert path.read_bytes() == before
        async with factory() as session:
            file = await session.get(ImportedFile, ids[1])
            assert file.diagnostics["comicinfo_enrichment"]["status"] == "pending"
        service._metadata_service.prefetch_issue_metadata_batch.side_effect = None
        service._metadata_service.prefetch_issue_metadata_batch.return_value = {456: full_issue()}
        await service.recover_pending_comicinfo_enrichment(factory)
        async with factory() as session:
            file = await session.get(ImportedFile, ids[1])
            assert file.diagnostics["comicinfo_enrichment"]["status"] == "complete"
        with ZipFile(path) as archive:
            assert ElementTree.fromstring(archive.read("ComicInfo.xml")).findtext("Summary") == (
                "The missing provider summary"
            )


async def test_failed_archive_write_remains_visible_in_import_follow_up(
    identity_probe_db, tmp_path
):
    from pullbox.models.import_job import ImportJob
    from pullbox.ui.import_follow_up import count_import_follow_up_jobs
    from pullbox.ui.import_results_context import load_import_results_context

    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (_, _, plan, ids, _):
        async with factory.begin() as session:
            file = await session.get(ImportedFile, ids[1])
            imported_series = await session.get(ImportedSeries, file.import_series_id)
            imported_series.status = ImportSeriesStatus.IMPORTED
            root = await session.get(LibraryRoot, plan.target.binding.library_root_id)
            root.allow_managed_writes = False
        async with factory() as session:
            job = await session.get(ImportJob, ids[0])
            context = await load_import_results_context(session, job, include_clean_library=False)
            assert context["archive_metadata_pending_count"] == 1
            assert context["follow_up_group_count"] == 0
        await import_service().recover_pending_comicinfo_enrichment(factory)
        async with factory() as session:
            assert await count_import_follow_up_jobs(session) == 1
            job = await session.get(ImportJob, ids[0])
            context = await load_import_results_context(session, job, include_clean_library=False)
            assert context["archive_metadata_pending_count"] == 0
            assert context["archive_metadata_failed_count"] == 1
            assert context["follow_up_group_count"] == 1


@pytest.mark.parametrize("providers", ["metron", "comicvine", "both", "metron_first", "existing"])
async def test_normal_import_background_job_writes_both_xml_and_retains_rollback(
    identity_probe_db, tmp_path, providers
):
    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, source, plan, ids, signature):
        if providers != "metron":
            async with factory.begin() as session:
                if providers == "comicvine":
                    await session.execute(delete(IssueExternalIdentity))
                    await session.execute(delete(SeriesExternalIdentity))
                series = await session.get(Series, plan.target.binding.metadata.series.local_id)
                issue = await session.get(Issue, plan.target.binding.metadata.issues[0].local_id)
                series.comicvine_id, issue.comicvine_id = 123, 456
                session.add_all(
                    [
                        SeriesExternalIdentity(
                            series_id=series.id,
                            identity_namespace="comicvine",
                            external_id="123",
                            verification_state="verified",
                            evidence_kind="provider_result",
                        ),
                        IssueExternalIdentity(
                            issue_id=issue.id,
                            identity_namespace="comicvine",
                            external_id="456",
                            verification_state="verified",
                            evidence_kind="provider_result",
                        ),
                    ]
                )
                if providers == "metron_first":
                    session.add(
                        MetadataSourceConfig(
                            source="metron_api",
                            enabled=True,
                            priority=100,
                            domain_priorities={"core": 0},
                        )
                    )
            if providers == "existing":
                with ZipFile(path, "a") as archive:
                    archive.writestr(
                        "MetronInfo.xml",
                        '<MetronInfo><IDS><ID source="Metron" primary="true">42</ID></IDS>'
                        "<Series><Name>Example</Name></Series><Number>50-X</Number></MetronInfo>",
                    )
                signature = await capture_fixture_signature(factory, path, plan, ids)
        original_source = source.read_bytes()
        service = import_service()
        assert await service.recover_pending_comicinfo_enrichment(factory) == 1
        with ZipFile(path) as archive:
            assert "MetronInfo.xml" in archive.namelist(), "The real job must write both documents"
            assert "ComicInfo.xml" in archive.namelist()
            assert archive.read("page.jpg") == b"page bytes"
            ci = ElementTree.fromstring(archive.read("ComicInfo.xml"))
            mi = ElementTree.fromstring(archive.read("MetronInfo.xml"))
            validate_metroninfo_xml(archive.read("MetronInfo.xml"))
            assert ci.findtext("Series") == mi.findtext("Series/Name") == "Example"
            assert ci.findtext("Number") == mi.findtext("Number") == "50-X"
            assert len(mi.findall("IDS/ID")) == (1 if providers in {"comicvine", "metron"} else 2)
            primary = mi.find("IDS/ID[@primary='true']")
            assert primary is not None
            assert primary.attrib["source"] == (
                "Comic Vine" if providers in {"comicvine", "both"} else "Metron"
            )
        async with factory() as session:
            file = await session.get(ImportedFile, ids[1])
            assert file.diagnostics["comicinfo_enrichment"]["status"] == "complete"
            publication = await session.scalar(select(ArchiveMetadataPublication))
            assert publication is not None and publication.state is PublicationState.FINALIZED
            action = await session.get(ImportJobAction, ids[2])
            assert action.payload["destination_signature"] == signature
            assert action.payload["metadata_publication"] == publication.operation_id
        before = path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns
        assert await service.recover_pending_comicinfo_enrichment(factory) == 0
        assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == before
        assert source.read_bytes() == original_source
        assert await rollback(factory, ids[2]) is ImportJobActionStatus.ROLLED_BACK
        assert not path.exists() and source.read_bytes() == original_source


@pytest.mark.parametrize("protection", ["referenced", "read_only_root"])
async def test_normal_job_does_not_rewrite_a_protected_file(
    identity_probe_db, tmp_path, protection
):
    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, _, plan, ids, _):
        async with factory.begin() as session:
            if protection == "referenced":
                file = await session.get(LibraryFile, plan.target.binding.library_file_id)
                file.storage_mode = LibraryFileStorageMode.REFERENCED
            else:
                root = await session.get(LibraryRoot, plan.target.binding.library_root_id)
                root.allow_managed_writes = False
        before = path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns
        await import_service().recover_pending_comicinfo_enrichment(factory)
        assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == before
        async with factory() as session:
            file = await session.get(ImportedFile, ids[1])
            assert file.diagnostics["comicinfo_enrichment"]["status"] == "failed"


async def capture_fixture_signature(factory, path, plan, ids):
    """Model registration of the fixture bytes before its background job starts."""
    signature = build_managed_placement_signature(path)
    async with factory.begin() as session:
        file = await session.get(LibraryFile, plan.target.binding.library_file_id)
        file.file_size = path.stat().st_size
        file.file_modified_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        file.source_signature = signature
        action = await session.get(ImportJobAction, ids[2])
        action.payload = {**action.payload, "destination_signature": signature}
    return signature


@pytest.mark.parametrize("approved,dangerous", [(False, False), (True, False), (True, True)])
async def test_size_approval_never_overrides_dangerous_content(
    identity_probe_db, tmp_path, monkeypatch, approved, dangerous
):
    from pullbox.tasks import import_metadata_writing

    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, _, plan, ids, _):
        if dangerous:
            with ZipFile(path, "a") as archive:
                archive.writestr("payload.exe", b"not a comic page")
            await capture_fixture_signature(factory, path, plan, ids)
        if approved:
            async with factory.begin() as session:
                file = await session.get(ImportedFile, ids[1])
                file.diagnostics = {
                    **file.diagnostics,
                    "safety_exception": {
                        "allowed_once": True,
                        "previous_block": {"code": "archive_size_limit", "overrideable": True},
                    },
                }
        monkeypatch.setattr(
            import_metadata_writing, "get_archive_size_limit_bytes", AsyncMock(return_value=1)
        )
        before = path.read_bytes()
        await import_service().recover_pending_comicinfo_enrichment(factory)
        async with factory() as session:
            file = await session.get(ImportedFile, ids[1])
            assert file.diagnostics["comicinfo_enrichment"]["status"] == (
                "complete" if approved and not dangerous else "failed"
            )
        if not approved or dangerous:
            assert path.read_bytes() == before


async def test_normal_job_reports_identity_conflict_without_mutating_archive(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, _, plan, ids, _):
        with ZipFile(path, "w") as archive:
            archive.writestr("page.jpg", b"page bytes")
            archive.writestr(
                "ComicInfo.xml",
                "<ComicInfo><Number>50-X</Number><Notes>[cv_issue_id:999]</Notes></ComicInfo>",
            )
        signature = build_managed_placement_signature(path)
        async with factory.begin() as session:
            file = await session.get(LibraryFile, plan.target.binding.library_file_id)
            file.file_size = path.stat().st_size
            file.file_modified_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            file.source_signature = signature
            action = await session.get(ImportJobAction, ids[2])
            action.payload = {**action.payload, "destination_signature": signature}
        before = path.read_bytes()
        await import_service().recover_pending_comicinfo_enrichment(factory)
        async with factory() as session:
            file = await session.get(ImportedFile, ids[1])
            assert file.diagnostics["comicinfo_enrichment"]["status"] == "failed"
        assert path.read_bytes() == before


async def test_publication_failure_keeps_owner_pending_for_restart(
    identity_probe_db, tmp_path, monkeypatch
):
    from pullbox.services import archive_metadata_finalization
    from pullbox.tasks import import_metadata_writing

    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, source, _, ids, _):
        finish = archive_metadata_finalization.finalize_archive_publication
        monkeypatch.setattr(
            import_metadata_writing,
            "finalize_archive_publication",
            AsyncMock(side_effect=RuntimeError("interrupted finalization")),
        )
        service = import_service()
        await service.recover_pending_comicinfo_enrichment(factory)
        async with factory() as session:
            file = await session.get(ImportedFile, ids[1])
            assert file.diagnostics["comicinfo_enrichment"]["status"] == "pending"
        monkeypatch.setattr(import_metadata_writing, "finalize_archive_publication", finish)
        published = path.read_bytes(), path.stat().st_ino
        await service.recover_pending_comicinfo_enrichment(factory)
        async with factory() as session:
            file = await session.get(ImportedFile, ids[1])
            assert file.diagnostics["comicinfo_enrichment"]["status"] == "complete"
        assert (path.read_bytes(), path.stat().st_ino) == published
        assert source.exists()


async def test_changed_queued_issue_is_not_silently_rebound(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, _, _, ids, _):
        async with factory.begin() as session:
            file = await session.get(ImportedFile, ids[1])
            details = dict(file.diagnostics["comicinfo_enrichment"])
            details["issue_id"] = 123456
            file.diagnostics = {**file.diagnostics, "comicinfo_enrichment": details}
        before = path.read_bytes()
        await import_service().recover_pending_comicinfo_enrichment(factory)
        async with factory() as session:
            file = await session.get(ImportedFile, ids[1])
            assert file.diagnostics["comicinfo_enrichment"]["status"] == "failed"
        assert path.read_bytes() == before


async def test_verified_imported_issue_can_write_while_rest_of_catalog_hydrates(
    identity_probe_db, tmp_path
):
    from pullbox.models.series import IssueCatalogState

    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, _, plan, ids, _):
        async with factory.begin() as session:
            series = await session.get(Series, plan.target.binding.metadata.series.local_id)
            series.issue_catalog_state = IssueCatalogState.HYDRATING
        await import_service().recover_pending_comicinfo_enrichment(factory)
        async with factory() as session:
            file = await session.get(ImportedFile, ids[1])
            assert file.diagnostics["comicinfo_enrichment"]["status"] == "complete"
            series = await session.get(Series, plan.target.binding.metadata.series.local_id)
            assert series.issue_catalog_state is IssueCatalogState.HYDRATING
        with ZipFile(path) as archive:
            assert "MetronInfo.xml" in archive.namelist()


async def test_durable_cancel_during_staging_preserves_archive_and_pending_work(
    identity_probe_db, tmp_path, monkeypatch
):
    from pullbox.models.import_job import ImportControlRequest, ImportJob
    from pullbox.tasks import import_metadata_writing

    _, factory, _ = identity_probe_db
    real_stage = import_metadata_writing.stage_cbz_metadata_interruptible
    async with owned(factory, tmp_path) as (path, _, _, ids, _):

        @asynccontextmanager
        async def cancel_before_handoff(*args, **kwargs):
            async with real_stage(*args, **kwargs) as stage:
                async with factory.begin() as session:
                    job = await session.get(ImportJob, ids[0])
                    job.control_request = ImportControlRequest.CANCEL
                yield stage

        monkeypatch.setattr(
            import_metadata_writing, "stage_cbz_metadata_interruptible", cancel_before_handoff
        )
        before = path.read_bytes()
        await import_service().recover_pending_comicinfo_enrichment(factory)
        async with factory() as session:
            file = await session.get(ImportedFile, ids[1])
            assert file.diagnostics["comicinfo_enrichment"]["status"] == "pending"
        assert path.read_bytes() == before
