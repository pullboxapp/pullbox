"""Legacy completed imports retain good files without overriding real conflicts."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from pullbox.core.exceptions import ValidationError
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.user import User
from pullbox.services.import_completed_cleanup import (
    CompletedImportCleanupAction,
    apply_completed_import_cleanup,
    preview_completed_import_cleanup,
)
from pullbox.services.import_known_series_recovery import load_known_series_recovery


async def seed_recovery(session, *, source_type=ImportSourceType.MYLAR3, method="mylar3_cv_id"):
    job = ImportJob(
        source_path="/imports", source_type=source_type, status=ImportJobStatus.COMPLETED
    )
    session.add(job)
    await session.flush()
    series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Batman",
        raw_year=1940,
        status=ImportSeriesStatus.NO_MATCH,
        diagnostics={
            "reason": "trusted_source_identity_conflict",
            "selected_candidate": {"cv_id": 796, "match_method": method, "title": "Batman"},
            "identity_conflicts": [{"field": "comicvine_issue_id", "first": 11, "conflicting": 12}],
        },
    )
    session.add(series)
    await session.flush()
    file = ImportedFile(
        import_job_id=job.id,
        import_series_id=series.id,
        file_path="/comics/Batman/001.cbz",
        file_name="Batman 001.cbz",
        file_size=1024,
        file_format="cbz",
        status=ImportedFileStatus.MATCHED,
        comicvine_issue_id=1001,
        parsed_issue_number=1,
        parsed_series="Batman",
        parsed_year=1940,
        matched_issue_cv_id=1001,
        match_method="comicvine_id",
        match_confidence="high",
        diagnostics={
            "comicvine_series_id": 796,
            "metadata_signals": {
                "comicvine_series_id": "mylar3" if method == "mylar3_cv_id" else "comicinfo",
                "comicvine_issue_id": "comicinfo",
                "series_name": "comicinfo",
            },
            "source_metadata": {"comicinfo": {"series": "Batman", "number": "1"}},
            "target_issue_summary": {
                "provider_id": "1001",
                "issue_number": 1,
                "issue_number_text": "1",
                "title": None,
                "release_date": None,
                "cover_url": None,
                "issue_type": "issue",
            },
        },
    )
    session.add(file)
    await session.commit()
    return job, series, file


@pytest.mark.parametrize(
    "source_type,method",
    [
        (ImportSourceType.MYLAR3, "mylar3_cv_id"),
        (ImportSourceType.FILESYSTEM, "comicinfo_cv_id"),
    ],
)
async def test_known_series_recovery_keeps_good_files_and_does_not_mutate_preview(
    db_session, source_type, method
):
    job, series, file = await seed_recovery(db_session, source_type=source_type, method=method)
    before = deepcopy(file.diagnostics)
    plans = await load_known_series_recovery(db_session, job.id)
    assert [plan.file_id for plan in plans] == [file.id]
    assert plans[0].cv_id == 796
    assert series.cv_id is None
    assert file.diagnostics == before
    assert not db_session.dirty


@pytest.mark.parametrize(
    "protection",
    ["skip", "imported", "safety", "identity", "manual", "wrong_series", "wrong_issue"],
)
async def test_known_series_recovery_preserves_protected_or_conflicting_files(
    db_session, protection
):
    job, series, file = await seed_recovery(db_session)
    if protection == "skip":
        file.status = ImportedFileStatus.SKIPPED
    elif protection == "imported":
        file.status = ImportedFileStatus.IMPORTED
    elif protection == "safety":
        file.diagnostics = {
            **file.diagnostics,
            "safety_block": {"category": "dangerous_path_or_payload"},
        }
    elif protection == "identity":
        file.diagnostics = {
            **file.diagnostics,
            "source_metadata": {
                "identity_conflicts": [
                    {"field": "comicvine_issue_id", "first": 1001, "conflicting": 1002}
                ]
            },
        }
    elif protection == "manual":
        series.user_selected_cv_id = 796
    elif protection == "wrong_series":
        file.diagnostics = {**file.diagnostics, "comicvine_series_id": 999}
    else:
        file.matched_issue_cv_id = 1002
    await db_session.commit()
    assert await load_known_series_recovery(db_session, job.id) == ()


async def test_known_series_recovery_rechecks_legacy_unmatched_files_with_exact_ids(db_session):
    job, series, file = await seed_recovery(db_session)
    series.diagnostics = {"reason": "trusted_source_identity_conflict"}
    file.status = ImportedFileStatus.NO_MATCH
    file.matched_issue_cv_id = None
    diagnostics = deepcopy(file.diagnostics)
    diagnostics.pop("target_issue_summary")
    diagnostics["kind"] = "series_no_match_file"
    file.diagnostics = diagnostics
    await db_session.commit()
    plans = await load_known_series_recovery(db_session, job.id)
    assert len(plans) == 1
    assert plans[0].summary["provider_id"] == "1001"


async def test_known_series_recovery_leaves_duplicate_candidates_for_review(db_session):
    job, series, file = await seed_recovery(db_session)
    other = ImportedFile(
        import_job_id=job.id,
        import_series_id=series.id,
        file_path="/comics/Batman/001-copy.cbz",
        file_name="Batman 001-copy.cbz",
        file_size=1024,
        file_format="cbz",
        status=file.status,
        comicvine_issue_id=file.comicvine_issue_id,
        parsed_issue_number=1,
        parsed_series="Batman",
        matched_issue_cv_id=1001,
        match_method="comicvine_id",
        diagnostics=deepcopy(file.diagnostics),
    )
    db_session.add(other)
    await db_session.commit()
    assert await load_known_series_recovery(db_session, job.id) == ()


@pytest.mark.parametrize(
    "source_type,method",
    [
        (ImportSourceType.MYLAR3, "mylar3_cv_id"),
        (ImportSourceType.FILESYSTEM, "comicinfo_cv_id"),
    ],
)
@pytest.mark.parametrize("other_issue_id", [1001, 1002])
@pytest.mark.parametrize("provider_id_type", [str, int])
async def test_known_series_recovery_checks_issue_identity_across_series(
    db_session, source_type, method, other_issue_id, provider_id_type
):
    job, series, file = await seed_recovery(db_session, source_type=source_type, method=method)
    _, other_series, other_file = await seed_recovery(
        db_session, source_type=source_type, method=method
    )
    other_series.import_job_id = job.id
    other_series.raw_series_name = "Superman"
    other_series.raw_year = 1939
    other_series.diagnostics = {
        **other_series.diagnostics,
        "selected_candidate": {"cv_id": 999, "match_method": method, "title": "Superman"},
    }
    other_file.import_job_id = job.id
    other_file.file_path = "/comics/Superman/001.cbz"
    other_file.file_name = "Superman 001.cbz"
    other_file.parsed_series = "Superman"
    other_file.parsed_year = 1939
    other_file.comicvine_issue_id = other_issue_id
    other_file.matched_issue_cv_id = other_issue_id
    other_file.diagnostics = {
        **other_file.diagnostics,
        "comicvine_series_id": 999,
        "source_metadata": {"comicinfo": {"series": "Superman", "number": "1"}},
        "target_issue_summary": {
            **other_file.diagnostics["target_issue_summary"],
            "provider_id": provider_id_type(other_issue_id),
        },
    }
    await db_session.commit()

    plans = await load_known_series_recovery(db_session, job.id)

    expected_ids = [] if other_issue_id == file.comicvine_issue_id else [file.id, other_file.id]
    assert [plan.file_id for plan in plans] == expected_ids
    assert series.status is ImportSeriesStatus.NO_MATCH
    assert other_series.status is ImportSeriesStatus.NO_MATCH
    assert file.status is ImportedFileStatus.MATCHED
    assert other_file.status is ImportedFileStatus.MATCHED
    assert not db_session.dirty


async def test_known_series_action_queues_only_previewed_files_and_preserves_conflicts(db_session):
    job, series, file = await seed_recovery(db_session)
    db_session.add(User(id=42, username="operator", password_hash="unused"))
    conflict = ImportedFile(
        import_job_id=job.id,
        import_series_id=series.id,
        file_path="/comics/Batman/002.cbz",
        file_name="Batman 002.cbz",
        file_size=1024,
        file_format="cbz",
        status=ImportedFileStatus.NO_MATCH,
        diagnostics={"kind": "metadata_conflict"},
    )
    db_session.add(conflict)
    await db_session.commit()
    action = CompletedImportCleanupAction.RECOVER_KNOWN_SERIES
    preview = await preview_completed_import_cleanup(db_session, job.id, action, actor_id=42)
    assert preview.affected_file_count == 1
    result = await apply_completed_import_cleanup(
        db_session,
        job.id,
        action,
        actor_id=42,
        preview_token=preview.preview_token,
    )
    assert result.requires_import_retry
    assert job.status is ImportJobStatus.IMPORTING
    assert series.cv_id == 796
    assert series.status is ImportSeriesStatus.CONFIRMED
    assert file.status is ImportedFileStatus.CONFIRMED
    assert conflict.status is ImportedFileStatus.NO_MATCH
    assert not conflict.include_in_import
    assert file.file_path == "/comics/Batman/001.cbz"
    assert series.diagnostics["identity_conflicts"]


async def test_known_series_action_is_visible_in_existing_follow_up_and_file_preview(db_session):
    from pullbox.services.import_completed_cleanup import list_completed_import_cleanup_files
    from pullbox.ui.import_results_context import _load_cleanup_action_summaries

    job, _series, file = await seed_recovery(db_session)
    summaries = await _load_cleanup_action_summaries(db_session, job.id)
    recovery = next(item for item in summaries if item["action"] == "recover_known_series")
    assert recovery["button_label"] == "Recover and retry"
    assert recovery["affected_file_count"] == 1
    page = await list_completed_import_cleanup_files(
        db_session,
        job.id,
        CompletedImportCleanupAction.RECOVER_KNOWN_SERIES,
    )
    assert [item.id for item in page.items] == [file.id]


async def test_known_series_recovery_survives_post_completion_job_failure(db_session):
    job, _series, file = await seed_recovery(db_session)
    db_session.add(User(id=42, username="operator", password_hash="unused"))
    job.status = ImportJobStatus.FAILED
    job.import_completed_at = datetime.now(UTC)
    job.error_message = "Optional follow-up failed after canonical import completed."
    await db_session.commit()

    plans = await load_known_series_recovery(db_session, job.id)
    preview = await preview_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.RECOVER_KNOWN_SERIES,
        actor_id=42,
    )

    assert [plan.file_id for plan in plans] == [file.id]
    assert preview.affected_file_count == 1


async def test_known_series_action_rejects_changed_parent_evidence(db_session):
    job, series, _file = await seed_recovery(db_session)
    action = CompletedImportCleanupAction.RECOVER_KNOWN_SERIES
    preview = await preview_completed_import_cleanup(db_session, job.id, action, actor_id=42)
    series.user_selected_cv_id = 999
    await db_session.commit()
    with pytest.raises(ValidationError, match="scope changed"):
        await apply_completed_import_cleanup(
            db_session,
            job.id,
            action,
            actor_id=42,
            preview_token=preview.preview_token,
        )
    assert job.status is ImportJobStatus.COMPLETED


async def test_known_series_recovery_never_implicitly_imports_unpreviewed_ready_sibling(db_session):
    job, series, file = await seed_recovery(db_session)
    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=series.id,
            file_path="/comics/Batman/unsafe-ready.cbz",
            file_name="unsafe-ready.cbz",
            file_size=1024,
            file_format="cbz",
            status=ImportedFileStatus.CONFIRMED,
            matched_issue_cv_id=999,
            diagnostics={"kind": "metadata_conflict"},
        )
    )
    await db_session.commit()
    assert await load_known_series_recovery(db_session, job.id) == ()
    assert file.status is ImportedFileStatus.MATCHED


@pytest.mark.parametrize("local_state", ["different_series", "owned", "different_number"])
async def test_known_series_recovery_respects_current_catalog_and_ownership(
    db_session, local_state
):
    from datetime import UTC, datetime

    from pullbox.models.issue import Issue
    from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot
    from pullbox.models.series import Series

    job, _item, _file = await seed_recovery(db_session)
    series = Series(
        title="Batman",
        sort_title="batman",
        comicvine_id=999 if local_state == "different_series" else 796,
    )
    db_session.add(series)
    await db_session.flush()
    issue = Issue(
        series_id=series.id,
        comicvine_id=1001,
        issue_number=2 if local_state == "different_number" else 1,
    )
    db_session.add(issue)
    await db_session.flush()
    if local_state == "owned":
        root = LibraryRoot(name="Library", path="/comics")
        db_session.add(root)
        await db_session.flush()
        db_session.add(
            LibraryFile(
                issue_id=issue.id,
                library_root_id=root.id,
                file_path="/comics/owned.cbz",
                file_name="owned.cbz",
                file_size=1024,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(UTC),
            )
        )
    await db_session.commit()
    assert await load_known_series_recovery(db_session, job.id) == ()
