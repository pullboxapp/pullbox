"""Managed Mylar and folder imports reach the paired writer from a real scan."""

import asyncio
import io
import shutil
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from zipfile import ZipFile

import pytest
from defusedxml import ElementTree
from PIL import Image
from py7zr import SevenZipFile
from sqlalchemy import delete, select, update

from pullbox.core.events import EventBus
from pullbox.core.metroninfo_schema import validate_metroninfo_xml
from pullbox.models import Issue, LibraryFile, LibraryRoot, Series, SystemConfig
from pullbox.models.archive_metadata_publication import ArchiveMetadataPublication, PublicationState
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobAction,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.library import LibraryFileStorageMode
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.models.series import IssueCatalogState
from pullbox.schemas.import_job import ConfirmImportRequest, ImportJobCreate
from pullbox.services.import_catalog_hydration import run_pending_catalog_hydration
from pullbox.services.import_comicinfo_enrichment import comicinfo_enrichment_tasks
from pullbox.services.import_job_execution import _load_duplicate_import_series
from pullbox.services.import_service import ImportService
from pullbox.services.metadata_service import MetadataService
from pullbox.services.series_service import SeriesService
from scripts.mylar3_import_fixture import create_mylar3_db
from tests.integration.metadata_identity.test_import_archive_publication import owned
from tests.integration.metadata_identity.test_import_metadata_enrichment import (
    full_issue,
    import_service,
)
from tests.integration.test_import_file_lifecycle import (
    _cv_search_result,
    _cv_series_metadata,
    _issue_summary,
    _mock_cv_provider,
)
from tests.unit.test_nonzip_metadata_writing import nonzip_archive
from tests.unit.test_pdf_metadata_writing import native_pdf, pdf_source

pytestmark = pytest.mark.usefixtures("paired_import_writer_setting")


@pytest.mark.parametrize(
    "problem", ["no_comicvine_catalog", "conflicted_parent", "stale_issue", "failed_catalog"]
)
async def test_only_unclaimed_comicvine_issues_wait_for_a_loading_catalog(
    identity_probe_db, tmp_path, problem
):
    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, source, plan, ids, _):
        before, source_before = path.read_bytes(), source.read_bytes()
        async with factory.begin() as session:
            series = await session.get(Series, plan.target.binding.metadata.series.local_id)
            issue = await session.get(Issue, plan.target.binding.metadata.issues[0].local_id)
            series.issue_catalog_state = (
                IssueCatalogState.FAILED
                if problem == "failed_catalog"
                else IssueCatalogState.HYDRATING
            )
            if problem == "stale_issue":
                await session.execute(
                    update(IssueExternalIdentity)
                    .where(IssueExternalIdentity.issue_id == issue.id)
                    .values(verification_state="stale")
                )
            else:
                await session.execute(
                    delete(IssueExternalIdentity).where(IssueExternalIdentity.issue_id == issue.id)
                )
            if problem == "conflicted_parent":
                await session.execute(
                    update(SeriesExternalIdentity)
                    .where(SeriesExternalIdentity.series_id == series.id)
                    .values(verification_state="conflicted")
                )
        service = import_service()
        await service.recover_pending_comicinfo_enrichment(factory)
        assert path.read_bytes() == before
        assert source.read_bytes() == source_before
        service._build_comicinfo_payload_for_issue.assert_not_awaited()
        async with factory() as session:
            file = await session.get(ImportedFile, ids[1])
            assert file.diagnostics["comicinfo_enrichment"]["status"] == "failed"


async def test_duplicate_selection_deduplicates_ids_without_comparing_json(identity_probe_db):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        local = Series(title="Existing", sort_title="existing", comicvine_id=123)
        job = ImportJob(source_path="fixture", source_type=ImportSourceType.FILESYSTEM)
        session.add_all([local, job])
        await session.flush()
        duplicate = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Existing",
            series_id=local.id,
            status=ImportSeriesStatus.DUPLICATE,
            diagnostics={"preserved": ["json evidence"]},
        )
        excluded = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Excluded",
            series_id=local.id,
            status=ImportSeriesStatus.DUPLICATE,
        )
        session.add_all([duplicate, excluded])
        await session.flush()
        for index, parent, include in (
            (1, duplicate, True),
            (2, duplicate, True),
            (3, excluded, False),
        ):
            session.add(
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=parent.id,
                    file_path=f"fixture/{index}.cbz",
                    file_name=f"{index}.cbz",
                    file_format="cbz",
                    status=ImportedFileStatus.MATCHED,
                    include_in_import=include,
                )
            )
    async with factory() as session:
        selected = await _load_duplicate_import_series(session, job.id, confirmed_ids=set())
        assert [row.id for row in selected] == [duplicate.id]
        assert selected[0].diagnostics == {"preserved": ["json evidence"]}
        assert (
            await _load_duplicate_import_series(session, job.id, confirmed_ids={duplicate.id}) == []
        )


@pytest.mark.parametrize(
    "source_type,in_place,catalog_failure",
    [
        (source_type, in_place, False)
        for source_type in ImportSourceType
        for in_place in (False, True)
    ]
    + [pytest.param(ImportSourceType.FILESYSTEM, False, True, id="failed-catalog")],
)
@pytest.mark.parametrize(
    "source_format",
    [
        "cbz",
        "cb7",
        pytest.param(
            "cbr",
            marks=pytest.mark.skipif(
                not shutil.which("unrar"),
                reason="Native UnRAR contract; required in Docker runtime qualification",
            ),
        ),
        pytest.param("pdf", marks=native_pdf),
    ],
)
async def test_scan_confirm_import_enrichment_and_rollback_preserve_sources(
    identity_probe_db,
    tmp_path,
    monkeypatch,
    source_type,
    in_place,
    catalog_failure,
    source_format,
    size_approval=False,
    changed_source=False,
):
    _, factory, _ = identity_probe_db
    source_root = tmp_path / "source"
    source_folder = source_root / "Example (2024) [123]"
    source_folder.mkdir(parents=True)
    (source_folder / "series.json").write_text('{"comicid": 123}')
    originals = {}
    for number in (1, 2):
        path = source_folder / f"Example {number:03d} (2024).{source_format}"
        members = {
            "001.jpg": b"first page" * (220000 if size_approval else 1),
            "002.jpg": b"second page",
            "ComicInfo.xml": (
                f"<ComicInfo><Series>Example</Series><Number>{number}</Number>"
                f"<Title>My local title {number}</Title></ComicInfo>"
            ).encode(),
        }
        if source_format == "pdf":
            pdf_source(path)
        elif source_format == "cbr":
            nonzip_archive(path, list(members.items()))
        elif source_format == "cbz":
            with ZipFile(path, "w") as archive:
                for name, payload in members.items():
                    archive.writestr(name, payload)
        else:
            with SevenZipFile(path, "w") as archive:
                for name, payload in members.items():
                    archive.writestr(payload, name)
        originals[path] = path.read_bytes()
    source = source_root
    if source_type is ImportSourceType.MYLAR3:
        source = tmp_path / "mylar.db"
        create_mylar3_db(
            source,
            series=[
                {
                    "ComicID": "123",
                    "ComicName": "Example",
                    "ComicYear": "2024",
                    "ComicPublisher": "Fixture Comics",
                    "ComicLocation": str(source_folder),
                    "Total": 2,
                }
            ],
            issues=[
                {
                    "IssueID": str(456 + number),
                    "ComicID": "123",
                    "Issue_Number": str(number),
                    "Location": path.name,
                }
                for number, path in enumerate(originals, start=1)
            ],
        )
    destination = tmp_path / "library"
    destination.mkdir()
    async with factory.begin() as session:
        if size_approval:
            session.add(SystemConfig(key="archive_size_limit_mb", value="1", value_type="int"))
        root = LibraryRoot(
            name="Managed destination",
            path=str(destination),
            enabled=True,
            allow_managed_writes=True,
            is_default_managed_destination=True,
        )
        session.add_all(
            [
                root,
                LibraryRoot(
                    name="Read-only source",
                    path=str(source_root),
                    enabled=True,
                    allow_referenced_registrations=True,
                    allow_managed_writes=False,
                ),
                SystemConfig(
                    key="convert_to_preferred_format_on_import",
                    value="true",
                    value_type="bool",
                ),
                SystemConfig(
                    key="update_embedded_comicinfo_from_match_on_import",
                    value="true",
                    value_type="bool",
                ),
            ]
        )
    provider = _mock_cv_provider(
        search_map={
            "Example": [
                _cv_search_result(provider_id="123", title="Example", year=2024, issue_count=2)
            ]
        },
        get_map={
            "123": replace(
                _cv_series_metadata(provider_id="123", title="Example", year=2024),
                status="continuing",
                issue_count=2,
            )
        },
        issues_map={
            "123": [
                _issue_summary(provider_id=str(456 + number), issue_number=number)
                for number in (1, 2)
            ]
        },
    )
    metadata = MetadataService(provider, tmp_path / "covers")
    metadata.prefetch_issue_metadata_batch = AsyncMock(
        return_value={
            456 + number: full_issue(
                provider_id=str(456 + number),
                issue_number=number,
                issue_number_text=str(number),
            )
            for number in (1, 2)
        }
    )
    events = EventBus()
    service = ImportService(SeriesService(metadata, events), metadata, events)
    # The independently tested catalog scheduler is not running in this test process.
    hydration = Mock()
    monkeypatch.setattr(
        "pullbox.services.import_job_execution._schedule_catalog_hydration", hydration
    )
    monkeypatch.setattr(
        "pullbox.composition.services.build_import_service", AsyncMock(return_value=service)
    )
    async with factory() as session:
        job = await service.create_job(
            session,
            ImportJobCreate(
                source_path=str(source),
                source_type=source_type,
                file_handling_mode=(
                    ImportFileHandlingMode.IN_PLACE
                    if in_place
                    else ImportFileHandlingMode.MANAGED_COPY
                ),
                target_library_root_id=None if in_place else root.id,
                mylar3_path_map_confirmed=source_type is ImportSourceType.MYLAR3,
            ),
        )
        job_id = job.id
        await session.commit()
        await service.start_scan(session, job_id)
        await session.commit()
    async with factory() as session:
        job = await session.get(ImportJob, job_id)
        assert job.status is ImportJobStatus.REVIEW
        if size_approval:
            files = list(await session.scalars(select(ImportedFile)))
            assert len(files) == 2
            assert all(file.status is ImportedFileStatus.SAFETY_BLOCKED for file in files)
            for file in files:
                assert file.diagnostics["safety_block"]["code"] == "archive_decompressed_size_limit"
                await service.allow_safety_blocked_file_once(session, job_id, file.id)
            for series_id in {file.import_series_id for file in files}:
                await service.rematch_imported_series_files(session, job_id, series_id)
            assert all(file.status is ImportedFileStatus.MATCHED for file in files)
            assert all(file.diagnostics["safety_exception"]["allowed_once"] for file in files)
        assert job.total_files_found == job.total_files_matched == 2
        items = list(await session.scalars(select(ImportedSeries)))
        await service.confirm_import(
            session, job_id, ConfirmImportRequest(series_ids=[item.id for item in items])
        )
        await session.commit()
        if changed_source:
            for path, data in originals.items():
                originals[path] = data + b"changed after approval"
                path.write_bytes(originals[path])
        result = await service.run_import(session, job_id)
        await session.commit()
        if changed_source:
            files = list(
                await session.scalars(
                    select(ImportedFile).execution_options(populate_existing=True)
                )
            )
            assert all(file.status is ImportedFileStatus.FAILED for file in files)
            assert all(
                file.diagnostics["source_revalidation"]["code"] == "source_changed"
                for file in files
            )
            assert await session.scalar(select(LibraryFile)) is None
            assert await session.scalar(select(ArchiveMetadataPublication)) is None
            assert not list(destination.rglob("*.cbz"))
            assert all(path.read_bytes() == data for path, data in originals.items())
            assert (await session.get(SystemConfig, "archive_size_limit_mb")).value == "1"
            return
        assert result.schedule_comicinfo_enrichment
    async with factory() as session:
        job = await session.get(ImportJob, job_id)
        assert job.status is ImportJobStatus.COMPLETED
        files = list(await session.scalars(select(ImportedFile).order_by(ImportedFile.id)))
        assert len(files) == 2
        assert all(file.status is ImportedFileStatus.IMPORTED for file in files)
        libraries = list(await session.scalars(select(LibraryFile).order_by(LibraryFile.id)))
        assert len(libraries) == 2
        assert await session.scalar(select(SeriesExternalIdentity)) is not None
        if in_place:
            assert all(file.storage_mode is LibraryFileStorageMode.REFERENCED for file in libraries)
            assert all(file.file_format.value == source_format for file in libraries)
            assert all("comicinfo_enrichment" not in file.diagnostics for file in files)
        else:
            assert all(file.file_format.value == "cbz" for file in libraries)
            assert all(
                file.diagnostics["comicinfo_enrichment"]["status"] == "pending" for file in files
            )
            assert all(Path(file.file_path).is_relative_to(destination) for file in libraries)
    assert await service.recover_pending_comicinfo_enrichment(factory) == (0 if in_place else 1)
    if not in_place and source_type is ImportSourceType.FILESYSTEM:
        async with factory() as session:
            files = list(await session.scalars(select(ImportedFile)))
            assert all(
                file.diagnostics["comicinfo_enrichment"]["status"] == "pending" for file in files
            ), "A loading catalog must not turn a provisional issue into a failed XML write"
        assert all(path.read_bytes() == data for path, data in originals.items())
    if catalog_failure:
        provider.get_series.side_effect = ValueError("Fixture catalog failed")
    hydrated = await run_pending_catalog_hydration(factory, series_service=service._series_service)
    async with factory() as session:
        state = await session.execute(
            select(Series.issue_catalog_state, Series.issue_catalog_error)
        )
        assert hydrated == (0 if catalog_failure else 1), list(state.all())
    if comicinfo_enrichment_tasks:
        await asyncio.gather(*tuple(comicinfo_enrichment_tasks))
    async with factory() as session:
        claims = list(await session.scalars(select(IssueExternalIdentity)))
        assert len(claims) == (0 if catalog_failure else 2)
        assert all(claim.verification_state == "verified" for claim in claims)
        if catalog_failure:
            files = list(await session.scalars(select(ImportedFile)))
            assert all(
                file.diagnostics["comicinfo_enrichment"]["status"] == "failed" for file in files
            )
            assert await session.scalar(select(ArchiveMetadataPublication)) is None
    if not in_place and not catalog_failure:
        async with factory() as session:
            files = list(await session.scalars(select(ImportedFile)))
            assert all(
                file.diagnostics["comicinfo_enrichment"]["status"] == "complete" for file in files
            )
            publications = list(await session.scalars(select(ArchiveMetadataPublication)))
            assert len(publications) == 2
            assert all(row.state is PublicationState.FINALIZED for row in publications)
            for file in libraries:
                with ZipFile(file.file_path) as archive:
                    ci = ElementTree.fromstring(archive.read("ComicInfo.xml"))
                    mi_bytes = archive.read("MetronInfo.xml")
                    validate_metroninfo_xml(mi_bytes)
                    mi = ElementTree.fromstring(mi_bytes)
                    assert ci.findtext("Number") == mi.findtext("Number")
                    assert ci.findtext("Title") == mi.findtext("Stories/Story")
                    if source_format == "pdf":
                        assert ci.findtext("Title") == "Provider title"
                    else:
                        assert ci.findtext("Title").startswith("My local title")
                    assert (
                        ci.findtext("Summary")
                        == mi.findtext("Summary")
                        == "The missing provider summary"
                    )
                    if source_format == "pdf":
                        pages = [name for name in archive.namelist() if name.endswith(".jpg")]
                        assert len(pages) == 3
                        for name, channel in zip(pages, (0, 1, 2), strict=True):
                            with Image.open(io.BytesIO(archive.read(name))) as page:
                                pixel = page.convert("RGB").getpixel((100, 100))
                                assert pixel[channel] > max(
                                    pixel[(channel + 1) % 3], pixel[(channel + 2) % 3]
                                )
                    else:
                        assert archive.read("001.jpg") == b"first page" * (
                            220000 if size_approval else 1
                        )
            actions = list(
                await session.scalars(
                    select(ImportJobAction).where(
                        ImportJobAction.action_type == "library_file_registered"
                    )
                )
            )
            assert all(action.payload.get("metadata_publication") for action in actions)
    snapshots = {Path(file.file_path): Path(file.file_path).read_bytes() for file in libraries}
    assert await service.recover_pending_comicinfo_enrichment(factory) == 0
    assert all(path.read_bytes() == data for path, data in snapshots.items())
    assert all(path.read_bytes() == data for path, data in originals.items())
    async with factory() as session:
        assert await service.rollback_import(session, job_id)
        await session.commit()
    assert all(path.read_bytes() == data for path, data in originals.items())
    if not in_place:
        assert all(not path.exists() for path in snapshots)
    async with factory() as session:
        assert await session.scalar(select(LibraryFile)) is None
        assert await session.scalar(select(Series)) is None
        assert await session.scalar(select(Issue)) is None
        if size_approval:
            assert (await session.get(SystemConfig, "archive_size_limit_mb")).value == "1"


@pytest.mark.parametrize("source_type", list(ImportSourceType))
@pytest.mark.parametrize(
    "source_format",
    [
        "cbz",
        "cb7",
        pytest.param(
            "cbr", marks=pytest.mark.skipif(not shutil.which("unrar"), reason="Native UnRAR")
        ),
    ],
)
@pytest.mark.parametrize("changed_source", [False, True])
async def test_reviewed_large_import_writes_paired_metadata_and_preserves_rollback(
    identity_probe_db, tmp_path, monkeypatch, source_type, source_format, changed_source
):
    await test_scan_confirm_import_enrichment_and_rollback_preserve_sources(
        identity_probe_db,
        tmp_path,
        monkeypatch,
        source_type,
        False,
        False,
        source_format,
        size_approval=True,
        changed_source=changed_source,
    )
