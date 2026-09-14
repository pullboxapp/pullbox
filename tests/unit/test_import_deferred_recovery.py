"""Deferred recovery respects physical identity, source evidence, and operator decisions."""

from datetime import UTC, date, datetime

import pytest

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueType
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot
from pullbox.models.series import IssueCatalogState, Series
from pullbox.services.import_deferred_recovery import plan_deferred_recovery


async def seed(session, *, source_type=ImportSourceType.MYLAR3):
    root = LibraryRoot(name="Comics", path="/comics")
    job = ImportJob(
        source_path="/imports", source_type=source_type, status=ImportJobStatus.COMPLETED
    )
    session.add_all([root, job])
    await session.flush()
    series = Series(
        title="Batman",
        sort_title="batman",
        year_start=2016,
        comicvine_id=100,
        library_root_id=root.id,
        issue_catalog_state=IssueCatalogState.COMPLETE,
    )
    session.add(series)
    await session.flush()
    issue = Issue(
        series_id=series.id,
        comicvine_id=1001,
        issue_number=104,
        issue_number_text="104",
        issue_type=IssueType.ISSUE,
        release_date=date(2021, 1, 1),
    )
    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Batman",
        raw_year=2016,
        cv_id=100,
        series_id=series.id,
        status=ImportSeriesStatus.IMPORTED,
    )
    session.add_all([issue, item])
    await session.flush()
    return job, item, series, issue, root


async def add_file(session, job, item, **overrides):
    values = dict(
        import_job_id=job.id,
        import_series_id=item.id,
        file_path="/comics/Batman/Batman 104 (2021).cbz",
        file_name="Batman 104 (2021).cbz",
        file_size=1024,
        file_format="cbz",
        parsed_series="Batman",
        parsed_issue_number=104,
        parsed_year=2021,
        status=ImportedFileStatus.NO_MATCH,
        comicvine_issue_id=1001,
        diagnostics={"source_issue_type": "issue"},
        source_signature={"size_bytes": 1024, "mtime_ns": 123456},
    )
    values.update(overrides)
    result = ImportedFile(**values)
    session.add(result)
    await session.flush()
    return result


async def register(session, file, issue, root):
    library = LibraryFile(
        file_path=file.file_path,
        file_name=file.file_name,
        file_size=file.file_size,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime.now(UTC),
        issue_id=issue.id,
        library_root_id=root.id,
        source_signature=file.source_signature,
    )
    session.add(library)
    await session.flush()
    return library


async def test_conflicting_saved_content_digest_never_collapses_files(db_session):
    job, item, _, issue, root = await seed(db_session)
    file = await add_file(db_session, job, item)
    file.source_signature = {**file.source_signature, "content_digest": "one"}
    registered = await register(db_session, file, issue, root)
    registered.source_signature = {**registered.source_signature, "content_digest": "two"}
    assert await plan_deferred_recovery(db_session, job.id) == ()


@pytest.mark.parametrize("source_type", list(ImportSourceType))
async def test_exact_registered_path_is_handled_without_reimport(db_session, source_type):
    job, item, _, issue, root = await seed(db_session, source_type=source_type)
    file = await add_file(db_session, job, item)
    await register(db_session, file, issue, root)
    await db_session.commit()

    plans = await plan_deferred_recovery(db_session, job.id)

    assert [(p.file_id, p.action, p.issue_id) for p in plans] == [
        (file.id, "already_registered", issue.id)
    ]
    assert file.status is ImportedFileStatus.NO_MATCH
    assert not db_session.dirty


async def test_one_physical_file_with_different_mylar_parents_has_one_decision(db_session):
    job, item, _, issue, _ = await seed(db_session)
    wrong = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Superman",
        cv_id=200,
        status=ImportSeriesStatus.NO_MATCH,
    )
    db_session.add(wrong)
    await db_session.flush()
    file = await add_file(db_session, job, item)
    twin = await add_file(
        db_session,
        job,
        wrong,
        diagnostics={
            "comicvine_series_id": 200,
            "metadata_signals": {"comicvine_series_id": "mylar3"},
        },
    )
    plans = await plan_deferred_recovery(db_session, job.id)
    assert [(p.file_id, p.action) for p in plans] == [
        (file.id, "exact_target"),
        (twin.id, "duplicate_reference"),
    ]
    assert plans[1].canonical_file_id == file.id
    assert plans[0].issue_id == issue.id


async def test_distinct_variant_paths_are_left_for_one_duplicate_decision(db_session):
    job, item, _, issue, root = await seed(db_session)
    owned = await add_file(db_session, job, item, status=ImportedFileStatus.IMPORTED)
    await register(db_session, owned, issue, root)
    variant = await add_file(db_session, job, item, file_path="/comics/Batman/104 variant.cbz")
    plans = await plan_deferred_recovery(db_session, job.id)
    assert [(p.file_id, p.action) for p in plans] == [(variant.id, "owned_variant")]


@pytest.mark.parametrize(
    "protection", ["safety", "manual", "skip", "conflicting_id", "wrong_title", "changed"]
)
async def test_recovery_preserves_protected_or_contradictory_evidence(db_session, protection):
    job, item, _, issue, root = await seed(db_session)
    file = await add_file(db_session, job, item)
    if protection == "safety":
        file.diagnostics = {"safety_block": {"category": "dangerous_path_or_payload"}}
    elif protection == "manual":
        file.match_method = "manual_issue"
    elif protection == "skip":
        file.status = ImportedFileStatus.SKIPPED
    elif protection == "conflicting_id":
        file.matched_issue_cv_id = 9999
    elif protection == "wrong_title":
        file.parsed_series = "Superman"
    else:
        await register(db_session, file, issue, root)
        file.source_signature = {"size_bytes": 1024, "mtime_ns": 9999}
    assert await plan_deferred_recovery(db_session, job.id) == ()


@pytest.mark.parametrize(
    "name,number",
    [
        ("batman.104. (2021).cbz", None),
        ("Batman 104 (2021) (converted).cbz", None),
        ("Batman 104 (2021).cbz", 104),
    ],
)
async def test_contextual_reparse_requires_exact_series_type_number_and_date(
    db_session, name, number
):
    job, item, _, issue, _ = await seed(db_session)
    file = await add_file(
        db_session, job, item, comicvine_issue_id=None, file_name=name, parsed_issue_number=number
    )
    plans = await plan_deferred_recovery(db_session, job.id)
    assert [(p.file_id, p.action, p.issue_id) for p in plans] == [
        (file.id, "exact_target", issue.id)
    ]


@pytest.mark.parametrize(
    "name,year,issue_type",
    [
        ("Batman Annual 104 (2021).cbz", 2021, "annual"),
        ("Batman 104 (1990).cbz", 1990, "issue"),
        ("Batman 104-105 (2021).cbz", 2021, "issue"),
        ("Batman Vol. 104 (2021).cbz", 2021, "tpb"),
        ("Absolute Batman 104 (2021).cbz", 2021, "issue"),
    ],
)
async def test_number_only_recovery_does_not_cross_type_title_pack_or_year(
    db_session, name, year, issue_type
):
    job, item, _, _, _ = await seed(db_session)
    await add_file(
        db_session,
        job,
        item,
        comicvine_issue_id=None,
        file_name=name,
        parsed_series=None,
        parsed_year=year,
        diagnostics={"source_issue_type": issue_type},
    )
    assert await plan_deferred_recovery(db_session, job.id) == ()


async def test_identical_unresolved_path_groups_once_without_inventing_a_target(db_session):
    job, item, _, _, _ = await seed(db_session)
    file = await add_file(
        db_session,
        job,
        item,
        comicvine_issue_id=None,
        parsed_series="Unknown",
        file_name="Unknown.cbz",
        parsed_issue_number=None,
    )
    twin = await add_file(
        db_session,
        job,
        item,
        comicvine_issue_id=None,
        parsed_series="Unknown",
        file_name="Unknown.cbz",
        parsed_issue_number=None,
    )
    plans = await plan_deferred_recovery(db_session, job.id)
    assert [(p.file_id, p.action, p.canonical_file_id) for p in plans] == [
        (twin.id, "duplicate_reference", file.id)
    ]


async def test_registered_identity_survives_container_device_number_change(db_session):
    job, item, _, issue, root = await seed(db_session)
    file = await add_file(
        db_session,
        job,
        item,
        source_signature={
            "size": 1024,
            "mtime_ns": 123456,
            "device": 98,
            "inode": 123,
        },
    )
    library = await register(db_session, file, issue, root)
    library.source_signature = {**file.source_signature, "device": 99}
    plans = await plan_deferred_recovery(db_session, job.id)
    assert plans[0].action == "already_registered"


async def test_stale_mylar_id_can_be_reconciled_by_embedded_id_and_registered_path(db_session):
    job, item, _, issue, root = await seed(db_session)
    file = await add_file(
        db_session,
        job,
        item,
        comicvine_issue_id=999,
        diagnostics={
            "metadata_signals": {"comicvine_issue_id": "mylar3", "series_name": "comicinfo"},
            "source_metadata": {
                "archive_metadata_loaded": True,
                "comicinfo": {
                    "series": "Batman",
                    "number": "104",
                    "web": "https://comicvine.gamespot.com/batman/4000-1001/",
                },
                "identity_conflicts": [
                    {"field": "comicvine_issue_id", "first": 999, "conflicting": 1001}
                ],
            },
        },
    )
    await register(db_session, file, issue, root)
    plans = await plan_deferred_recovery(db_session, job.id)
    assert [(p.action, p.issue_id) for p in plans] == [("already_registered", issue.id)]


async def test_conflicting_embedded_ids_remain_reviewable(db_session):
    job, item, _, issue, root = await seed(db_session)
    file = await add_file(
        db_session,
        job,
        item,
        diagnostics={
            "source_metadata": {
                "comicinfo": {
                    "series": "Batman",
                    "number": "104",
                    "web": "https://comicvine.gamespot.com/batman/4000-1001/",
                    "notes": "[cv_issue_id:999]",
                }
            },
        },
    )
    await register(db_session, file, issue, root)
    assert await plan_deferred_recovery(db_session, job.id) == ()
