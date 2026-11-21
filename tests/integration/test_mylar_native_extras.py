"""Native files outside Mylar's issue records keep their own exact ownership."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.models.series import Series
from pullbox.schemas.import_job import ConfirmImportRequest, ImportJobCreate
from pullbox.services.import_metron_matching import (
    native_series_preview_identity,
    prepare_metron_series_recheck,
)
from pullbox.services.import_native_issue_choices import native_review_context
from scripts.mylar3_import_fixture import create_minimal_cbz, create_mylar3_db
from tests.integration.test_metron_folder_import import _archive, _service, _setup


async def seed_mylar_native_extra(
    session,
    tmp_path,
    namespace="metron",
    in_place=False,
    *,
    identity_base=0,
    admit=True,
    same_name=False,
):
    destination, native, native_issue = await _setup(
        session,
        tmp_path,
        namespace=IdentityNamespace(namespace),
        series_external_id=str(identity_base + 21),
        issue_external_id=str(identity_base + 101),
        default_destination=False,
    )
    native_issue.status = IssueStatus.SKIPPED
    recorded_series = Series(
        title="Old Harbor",
        sort_title="Old Harbor",
        year_start=2020,
        comicvine_id=identity_base + 900,
    )
    session.add(recorded_series)
    await session.flush()
    recorded_issue = Issue(
        series_id=recorded_series.id,
        comicvine_id=identity_base + 901,
        issue_number=1,
        status=IssueStatus.WANTED,
    )
    session.add(recorded_issue)
    source = tmp_path / "source"
    folder = source / "Old Harbor (2020)"
    tracked = folder / "Old Harbor 001.cbz"
    create_minimal_cbz(tracked)
    extra = folder / ("Old Harbor 003.cbz" if same_name else "Harbor Lights 003.cbz")
    _archive(
        extra,
        primary=namespace,
        series_id=str(identity_base + 21),
        issue_id=str(identity_base + 101),
    )
    unknown = folder / "Unidentified 099.cbz"
    create_minimal_cbz(unknown)
    if in_place:
        session.add(
            LibraryRoot(
                name="Mylar source",
                path=str(source),
                allow_managed_writes=False,
                allow_referenced_registrations=True,
                enabled=True,
            )
        )
    await session.commit()
    database = tmp_path / "mylar.db"
    create_mylar3_db(
        database,
        series=[
            {
                "ComicID": str(identity_base + 900),
                "ComicName": "Old Harbor",
                "ComicYear": "2020",
                "ComicLocation": str(folder),
                "Total": 1,
            }
        ],
        issues=[
            {
                "ComicID": str(identity_base + 900),
                "IssueID": str(identity_base + 901),
                "Issue_Number": "1",
                "Location": tracked.name,
            }
        ],
    )
    service = _service()
    request = ImportJobCreate(
        source_type="mylar3",
        source_path=str(database),
        mylar3_path_map_confirmed=True,
        file_handling_mode="in_place" if in_place else "managed_copy",
        target_library_root_id=None if in_place else destination.id,
    )
    if admit:
        job = await service.create_job(session, request)
    else:
        from pullbox.core.library_policy import load_library_ingest_policy
        from pullbox.models.import_job import ImportJob
        from pullbox.services.import_policy_snapshot import apply_ingest_policy_to_import_job

        job = ImportJob(**request.model_dump(exclude_unset=True))
        apply_ingest_policy_to_import_job(job, await load_library_ingest_policy(session))
        session.add(job)
        await session.commit()
    originals = {path: path.read_bytes() for path in (database, tracked, extra, unknown)}
    return job, service, native, native_issue, recorded_series, recorded_issue, originals, extra


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("in_place", [False, True])
@pytest.mark.parametrize("same_name", [False, True])
async def test_unrecorded_native_mylar_file_imports_without_claiming_recorded_neighbors(
    db_session, tmp_path, namespace, in_place, same_name
):
    (
        job,
        service,
        native,
        issue,
        recorded,
        recorded_issue,
        originals,
        extra,
    ) = await seed_mylar_native_extra(
        db_session, tmp_path, namespace, in_place, same_name=same_name
    )
    await service.start_scan(db_session, job.id)
    groups = (await db_session.scalars(select(ImportedSeries))).all()
    files = {
        Path(file.file_path): file
        for file in (await db_session.scalars(select(ImportedFile))).all()
    }
    assert len(groups) == job.series_found == 2
    assert len(files) == job.scan_total_files == 3
    file = files[extra]
    assert file.status == ImportedFileStatus.MATCHED
    assert file.matched_issue_id == issue.id
    assert file.match_method == f"{namespace}_id"
    parent = await db_session.get(ImportedSeries, file.import_series_id)
    assert parent.series_id == native.id and parent.cv_id is None
    tracked = files[extra.parent / "Old Harbor 001.cbz"]
    assert tracked.matched_issue_id == recorded_issue.id
    legacy = await db_session.get(ImportedSeries, tracked.import_series_id)
    assert legacy.series_id == recorded.id and legacy.cv_id == 900
    unknown = files[extra.parent / "Unidentified 099.cbz"]
    assert unknown.import_series_id == legacy.id
    assert unknown.status == ImportedFileStatus.NO_MATCH
    assert unknown.matched_issue_id is None
    assert not any(file.include_in_import for file in files.values())
    for selected in (file, tracked):
        await service.update_file_selection(db_session, job.id, selected.id, include_in_import=True)
    await service.confirm_import(
        db_session, job.id, ConfirmImportRequest(series_ids=[parent.id, legacy.id])
    )
    await service.run_import(db_session, job.id)
    await db_session.refresh(file)
    assert file.status == ImportedFileStatus.IMPORTED, file.error_message
    placed = (await db_session.scalars(select(LibraryFile))).all()
    assert {row.issue_id for row in placed} == {issue.id, recorded_issue.id}
    destinations = [Path(row.file_path) for row in placed]
    assert all(path.is_file() for path in destinations)
    assert await service.rollback_import(db_session, job.id)
    await db_session.refresh(issue)
    await db_session.refresh(recorded_issue)
    assert issue.status == IssueStatus.SKIPPED
    assert recorded_issue.status == IssueStatus.WANTED
    assert await db_session.scalar(select(LibraryFile.id)) is None
    assert all(path.exists() == in_place for path in destinations)
    assert all(path.read_bytes() == content for path, content in originals.items())
    for method in ("search_series", "get_series", "get_issues_for_series"):
        getattr(service._metadata_service._provider, method).assert_not_awaited()


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
async def test_mylar_native_extras_offer_identity_review_then_issue_recovery(
    db_session, tmp_path, namespace
):
    (
        job,
        service,
        native,
        issue,
        _recorded,
        _recorded_issue,
        originals,
        extra,
    ) = await seed_mylar_native_extra(db_session, tmp_path, namespace)
    link = await db_session.scalar(
        select(SeriesExternalIdentity).where(SeriesExternalIdentity.series_id == native.id)
    )
    link.verification_state = IdentityVerificationState.STALE
    await db_session.commit()
    await service.start_scan(db_session, job.id)
    file = await db_session.scalar(select(ImportedFile).where(ImportedFile.file_path == str(extra)))
    assert file.status == ImportedFileStatus.NO_MATCH
    _, _, provider, external_id = await native_series_preview_identity(
        db_session, job.id, file.import_series_id
    )
    assert (provider.value, external_id) == (namespace, "21")
    link.verification_state = IdentityVerificationState.VERIFIED
    issue_link = await db_session.scalar(
        select(IssueExternalIdentity).where(IssueExternalIdentity.issue_id == issue.id)
    )
    issue_link.verification_state = IdentityVerificationState.STALE
    await db_session.commit()
    await prepare_metron_series_recheck(db_session, job.id, file.import_series_id)
    await service.rematch_imported_series_files(db_session, job.id, file.import_series_id)
    await db_session.refresh(file)
    assert file.status == ImportedFileStatus.NO_MATCH
    issue_link.verification_state = IdentityVerificationState.VERIFIED
    await db_session.commit()
    _, _, _, resolved, provider = await native_review_context(db_session, job.id, file.id)
    assert resolved.id == native.id and provider.value == namespace
    assert all(path.read_bytes() == content for path, content in originals.items())


@pytest.mark.parametrize(
    "defect", ["unknown_issue", "number", "cross_identity", "sidecar", "owned"]
)
async def test_mylar_native_extras_do_not_bypass_identity_or_ownership_review(
    db_session, tmp_path, defect
):
    (
        job,
        service,
        _native,
        issue,
        _recorded,
        _recorded_issue,
        originals,
        extra,
    ) = await seed_mylar_native_extra(db_session, tmp_path)
    if defect == "unknown_issue":
        _archive(extra, issue_id="999")
    elif defect == "number":
        _archive(extra, number="4")
    elif defect == "cross_identity":
        _archive(extra, extra="<ComicInfo><Notes>[cv_vol_id:999]</Notes></ComicInfo>")
    elif defect == "sidecar":
        (extra.parent / "cvinfo").write_text("https://comicvine.gamespot.com/volume/4050-999/")
    else:
        owned = tmp_path / "library" / "owned.cbz"
        owned.write_bytes(extra.read_bytes())
        db_session.add(
            LibraryFile(
                issue_id=issue.id,
                file_path=str(owned),
                file_name=owned.name,
                file_size=owned.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.fromtimestamp(owned.stat().st_mtime, UTC),
                library_root_id=job.target_library_root_id,
            )
        )
        issue.status = IssueStatus.OWNED
        await db_session.commit()
    originals[extra] = extra.read_bytes()
    await service.start_scan(db_session, job.id)
    file = await db_session.scalar(select(ImportedFile).where(ImportedFile.file_path == str(extra)))
    if defect == "owned":
        assert file.status == ImportedFileStatus.ALREADY_OWNED
        assert owned.read_bytes() == originals[extra]
    else:
        assert file.status in {ImportedFileStatus.NO_MATCH, ImportedFileStatus.CONFLICT}
        assert file.matched_issue_id is None
    assert not file.include_in_import
    assert all(path.read_bytes() == content for path, content in originals.items())
