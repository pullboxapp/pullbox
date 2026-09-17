"""Legacy completed imports retain good files without overriding real conflicts."""

from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from pullbox.core.exceptions import ValidationError
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
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
from pullbox.services.import_workflow_state import deferred_recovery_scope


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
    [
        "skip",
        "imported",
        "safety",
        "safety_review",
        "excluded",
        "identity",
        "manual",
        "wrong_series",
        "wrong_issue",
    ],
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
    elif protection == "safety_review":
        file.diagnostics = {**file.diagnostics, "safety_review": {"action": "allow_once"}}
    elif protection == "excluded":
        file.diagnostics = {**file.diagnostics, "review_selection": False}
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
    target = await db_session.get(ImportedSeries, file.import_series_id)
    assert target.id != series.id
    assert target.cv_id == 796
    assert target.status is ImportSeriesStatus.CONFIRMED
    assert deferred_recovery_scope(job) == (target.id,)
    assert series.cv_id is None
    assert series.status is ImportSeriesStatus.NO_MATCH
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
    plans = await load_known_series_recovery(db_session, job.id)
    assert [plan.file_id for plan in plans] == [file.id]
    assert file.status is ImportedFileStatus.MATCHED


@pytest.mark.parametrize(
    "source_type,method",
    [(ImportSourceType.MYLAR3, "mylar3_cv_id"), (ImportSourceType.FILESYSTEM, "comicinfo_cv_id")],
)
async def test_known_series_recovery_isolates_proven_files_from_unresolved_siblings(
    db_session, source_type, method
):
    job, parent, good = await seed_recovery(db_session, source_type=source_type, method=method)
    db_session.add(User(id=42, username="operator", password_hash="unused"))
    job.file_handling_mode = ImportFileHandlingMode.IN_PLACE
    job.move_to_library = False
    job.source_preserved = True
    parent.source_folder = "/comics/Batman"
    unresolved = ImportedFile(
        import_job_id=job.id,
        import_series_id=parent.id,
        file_path="/comics/Batman/002.cbz",
        file_name="Batman 002.cbz",
        file_size=1024,
        file_format="cbz",
        status=ImportedFileStatus.CONFIRMED,
        include_in_import=True,
        matched_issue_cv_id=999,
        match_method="manual",
        diagnostics={"kind": "metadata_conflict"},
    )
    unrelated = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Unrelated ready series",
        status=ImportSeriesStatus.CONFIRMED,
        selected_for_import=True,
        cv_id=999,
    )
    db_session.add_all([unresolved, unrelated])
    await db_session.commit()
    before = deepcopy(unresolved.diagnostics)
    action = CompletedImportCleanupAction.RECOVER_KNOWN_SERIES

    plans = await load_known_series_recovery(db_session, job.id)
    assert [plan.file_id for plan in plans] == [good.id]
    assert good.import_series_id == parent.id
    assert not db_session.dirty
    preview = await preview_completed_import_cleanup(db_session, job.id, action, actor_id=42)
    await apply_completed_import_cleanup(
        db_session,
        job.id,
        action,
        actor_id=42,
        preview_token=preview.preview_token,
    )

    assert good.import_series_id != parent.id
    target = await db_session.get(ImportedSeries, good.import_series_id)
    assert target.cv_id == 796
    assert target.status is ImportSeriesStatus.CONFIRMED
    assert target.selected_for_import
    assert target.files_total == 1
    assert target.source_folder == parent.source_folder
    assert target.diagnostics["source_import_series_id"] == parent.id
    assert good.diagnostics["completed_import_cleanup"]["source_import_series_id"] == parent.id
    assert good.file_path == "/comics/Batman/001.cbz"
    assert parent.status is ImportSeriesStatus.NO_MATCH
    assert parent.cv_id is None
    assert parent.files_total == 1
    assert unresolved.import_series_id == parent.id
    assert unresolved.status is ImportedFileStatus.CONFIRMED
    assert unresolved.include_in_import
    assert unresolved.diagnostics == before
    assert deferred_recovery_scope(job) == (target.id,)
    assert unrelated.id not in deferred_recovery_scope(job)
    assert job.file_handling_mode is ImportFileHandlingMode.IN_PLACE
    assert not job.move_to_library
    assert job.source_preserved


@pytest.mark.parametrize("claim", ["source", "target", "local"])
async def test_known_series_recovery_does_not_compete_with_a_manual_keeper(db_session, claim):
    job, parent, good = await seed_recovery(db_session)
    keeper = ImportedFile(
        import_job_id=job.id,
        import_series_id=parent.id,
        file_path="/comics/Batman/manual-keeper.cbz",
        file_name="manual-keeper.cbz",
        file_size=1024,
        file_format="cbz",
        status=ImportedFileStatus.CONFIRMED,
        include_in_import=True,
        matched_issue_cv_id=good.matched_issue_cv_id,
        match_method="manual",
        diagnostics={"review_selection": True},
    )
    if claim == "source":
        keeper.comicvine_issue_id = good.matched_issue_cv_id
        keeper.matched_issue_cv_id = None
    elif claim == "local":
        from pullbox.models.issue import Issue
        from pullbox.models.series import Series

        catalog = Series(title="Batman", sort_title="batman", comicvine_id=796)
        db_session.add(catalog)
        await db_session.flush()
        issue = Issue(series_id=catalog.id, comicvine_id=1001, issue_number=1)
        db_session.add(issue)
        await db_session.flush()
        keeper.matched_issue_id = issue.id
        keeper.matched_issue_cv_id = None
    db_session.add(keeper)
    await db_session.commit()

    assert await load_known_series_recovery(db_session, job.id) == ()


async def test_known_series_recovery_revalidates_new_manual_claim_after_preview(db_session):
    job, parent, good = await seed_recovery(db_session)
    action = CompletedImportCleanupAction.RECOVER_KNOWN_SERIES
    preview = await preview_completed_import_cleanup(db_session, job.id, action, actor_id=42)
    other = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Another review group",
        status=ImportSeriesStatus.CONFIRMED,
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=other.id,
            file_path="/comics/manual-keeper.cbz",
            file_name="manual-keeper.cbz",
            file_format="cbz",
            status=ImportedFileStatus.CONFIRMED,
            matched_issue_cv_id=good.matched_issue_cv_id,
            match_method="manual",
            include_in_import=True,
        )
    )
    await db_session.commit()

    with pytest.raises(ValidationError, match="scope changed"):
        await apply_completed_import_cleanup(
            db_session,
            job.id,
            action,
            actor_id=42,
            preview_token=preview.preview_token,
        )
    assert good.import_series_id == parent.id
    assert job.status is ImportJobStatus.COMPLETED


async def test_known_series_recovery_keeps_one_group_across_apply_batches(db_session):
    from sqlalchemy import select

    job, parent, first = await seed_recovery(db_session)
    db_session.add(User(id=42, username="operator", password_hash="unused"))
    for number in range(2, 403):
        diagnostics = deepcopy(first.diagnostics)
        diagnostics["source_metadata"]["comicinfo"]["number"] = str(number)
        diagnostics["target_issue_summary"].update(
            provider_id=str(1000 + number),
            issue_number=number,
            issue_number_text=str(number),
        )
        db_session.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=parent.id,
                file_path=f"/comics/Batman/{number}.cbz",
                file_name=f"Batman {number}.cbz",
                file_format="cbz",
                file_size=1024,
                status=ImportedFileStatus.MATCHED,
                comicvine_issue_id=1000 + number,
                matched_issue_cv_id=1000 + number,
                parsed_series="Batman",
                parsed_issue_number=number,
                diagnostics=diagnostics,
            )
        )
    await db_session.commit()
    action = CompletedImportCleanupAction.RECOVER_KNOWN_SERIES
    preview = await preview_completed_import_cleanup(db_session, job.id, action, actor_id=42)
    assert preview.affected_file_count == 402

    await apply_completed_import_cleanup(
        db_session,
        job.id,
        action,
        actor_id=42,
        preview_token=preview.preview_token,
    )

    scope = deferred_recovery_scope(job)
    assert scope is not None and len(scope) == 1
    target = await db_session.get(ImportedSeries, scope[0])
    assert target.files_total == 402
    assert target.files_matched == 402
    assert parent.status is ImportSeriesStatus.SKIPPED
    assert parent.files_total == 0
    assert set(await db_session.scalars(select(ImportedFile.import_series_id))) == {target.id}


@pytest.mark.parametrize("changed_source", [False, True])
async def test_known_series_recovery_runs_scoped_import_and_checks_source_signature(
    db_session, tmp_path, monkeypatch, changed_source
):
    from pullbox.core.library_file_ownership import build_file_identity_signature
    from pullbox.models.issue import Issue
    from pullbox.models.library import LibraryFileStorageMode, LibraryRoot
    from pullbox.models.series import Series
    from pullbox.services import import_job_execution as execution
    from pullbox.services.import_referenced_sources import MYLAR_REFERENCE_ROOT_ID_SIGNATURE_KEY
    from pullbox.services.import_service import ImportService
    from scripts.mylar3_import_fixture import create_minimal_cbz
    from tests.unit.test_import_file_execution import _mock_register_library_file

    job, parent, good = await seed_recovery(db_session)
    root = LibraryRoot(name="Original library", path=str(tmp_path), enabled=True)
    catalog = Series(title="Batman", sort_title="batman", year_start=1940, comicvine_id=796)
    db_session.add_all([root, catalog, User(id=42, username="operator", password_hash="unused")])
    await db_session.flush()
    db_session.add(Issue(series_id=catalog.id, comicvine_id=1001, issue_number=1))
    comic = tmp_path / "Batman" / "Batman 001.cbz"
    create_minimal_cbz(comic)
    signature = build_file_identity_signature(comic)
    signature[MYLAR_REFERENCE_ROOT_ID_SIGNATURE_KEY] = root.id
    good.file_path, good.file_size, good.source_signature = (
        str(comic),
        comic.stat().st_size,
        signature,
    )
    parent.source_folder = str(comic.parent)
    job.file_handling_mode = ImportFileHandlingMode.IN_PLACE
    job.move_to_library = False
    job.source_preserved = True
    job.effective_transfer_method = "leave_in_place"
    unrelated = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Unrelated",
        cv_id=999,
        status=ImportSeriesStatus.CONFIRMED,
        selected_for_import=True,
    )
    db_session.add(unrelated)
    await db_session.commit()
    action = CompletedImportCleanupAction.RECOVER_KNOWN_SERIES
    preview = await preview_completed_import_cleanup(db_session, job.id, action, actor_id=42)
    await apply_completed_import_cleanup(
        db_session,
        job.id,
        action,
        actor_id=42,
        preview_token=preview.preview_token,
    )
    await db_session.commit()
    if changed_source:
        comic.write_bytes(b"changed after the recovery preview")
    before = comic.read_bytes(), comic.stat().st_mtime_ns
    register = _mock_register_library_file()
    monkeypatch.setattr("pullbox.services.import_service.register_library_file", register)
    arcs = AsyncMock(side_effect=AssertionError("Unrelated Story Arcs must not execute"))
    monkeypatch.setattr(execution, "_execute_story_arc_materialization", arcs)
    series_service = AsyncMock()
    series_service.add_from_comicvine.return_value = catalog
    service = ImportService(
        series_service=series_service, metadata_service=AsyncMock(), event_bus=AsyncMock()
    )

    await service.run_import(db_session, job.id)

    await db_session.refresh(good)
    await db_session.refresh(unrelated)
    assert job.status is ImportJobStatus.COMPLETED
    assert job.progress_snapshot["deferred_recovery"]["state"] == "completed"
    assert unrelated.status is ImportSeriesStatus.CONFIRMED
    arcs.assert_not_awaited()
    assert (comic.read_bytes(), comic.stat().st_mtime_ns) == before
    if changed_source:
        register.assert_not_awaited()
        assert good.status is ImportedFileStatus.FAILED
        assert good.diagnostics["source_revalidation"]["code"] == "source_changed"
    else:
        register.assert_awaited_once()
        assert good.status is ImportedFileStatus.IMPORTED
        assert register.await_args.kwargs["storage_mode"] is LibraryFileStorageMode.REFERENCED
        assert register.await_args.kwargs["move_to_library"] is False
        assert register.await_args.kwargs["library_root_id"] == root.id
        assert register.await_args.kwargs["expected_source_signature"] == signature


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
