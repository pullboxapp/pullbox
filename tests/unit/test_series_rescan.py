"""Series rescans must reconcile ownership without changing source artifacts."""

from __future__ import annotations

import os
import time
from pathlib import Path
from zipfile import ZipFile

import pytest

from pullbox.services.series_rescan import plan_series_rescan
from pullbox.services.series_rescan_registration import apply_rescan_match


def comic(folder: Path, name: str, *, pages: int = 2, xml: str = "") -> Path:
    path = folder / name
    with ZipFile(path, "w") as archive:
        for page in range(pages):
            archive.writestr(f"{page:03}.jpg", b"test page")
        if xml:
            archive.writestr("ComicInfo.xml", xml)
    os.utime(path, (time.time() - 120, time.time() - 120))
    return path


@pytest.fixture
def context(tmp_path):
    folder = tmp_path / "Swamp Thing (1986)"
    folder.mkdir()
    return {
        "folder": str(folder),
        "roots": [{"id": 1, "path": str(tmp_path)}],
        "series": {"id": 1, "title": "Swamp Thing", "year_start": 1986, "comicvine_id": 123},
        "issues": [
            {"id": 10, "number": 1.0, "text": "1", "cv_id": 1001, "type": "issue"},
            {"id": 11, "number": 13.0, "text": "13A", "cv_id": 1002, "type": "issue"},
        ],
        "files": [],
        "block_dangerous": True,
        "max_archive_size": 1024 * 1024,
    }


def test_new_exact_file_is_registered_without_modifying_source(context):
    path = comic(Path(context["folder"]), "Swamp Thing 001 (1986).cbz")
    before = path.read_bytes(), path.stat().st_mtime_ns
    plan = plan_series_rescan(context)
    assert len(plan) == 1
    assert plan[0]["outcome"] == "register"
    assert plan[0]["issue_id"] == 10
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_missing_folder_stays_disabled_after_alpine_initializes():
    from jinja2 import Environment, FileSystemLoader

    templates = Path(__file__).parents[2] / "src/pullbox/ui/templates"
    environment = Environment(loader=FileSystemLoader(templates), autoescape=True)
    html = environment.get_template("partials/series_rescan.html").render(
        series={"id": 1, "path": None}
    )
    assert ':disabled="true || busy' in html


def test_duplicate_candidates_are_not_arbitrarily_registered(context):
    folder = Path(context["folder"])
    comic(folder, "Swamp Thing 001 (1986).cbz")
    comic(folder, "Swamp Thing 001 (1986) (variant).cbz")
    plan = plan_series_rescan(context)
    assert len(plan) == 2
    assert {item["outcome"] for item in plan} == {"review"}
    assert all("multiple" in item["reason"].lower() for item in plan)


def test_letter_suffix_does_not_fall_back_to_plain_number(context):
    folder = Path(context["folder"])
    comic(folder, "Swamp Thing 013a (1986).cbz")
    comic(folder, "Swamp Thing 013b (1986).cbz")
    plan = {Path(item["file_path"]).name: item for item in plan_series_rescan(context)}
    assert plan["Swamp Thing 013a (1986).cbz"]["issue_id"] == 11
    assert plan["Swamp Thing 013a (1986).cbz"]["outcome"] == "register"
    assert plan["Swamp Thing 013b (1986).cbz"]["outcome"] == "review"


def test_single_page_and_foreign_series_require_review(context):
    folder = Path(context["folder"])
    comic(folder, "Swamp Thing 001 (1986).cbz", pages=1)
    comic(folder, "Batman 001 (1986).cbz")
    assert {item["outcome"] for item in plan_series_rescan(context)} == {"review"}


def test_unavailable_folder_is_not_an_empty_success(context):
    Path(context["folder"]).rmdir()
    with pytest.raises(ValueError, match="unavailable"):
        plan_series_rescan(context)


def test_obsolete_file_link_that_is_now_a_directory_does_not_abort_rescan(context):
    folder = Path(context["folder"])
    comic(folder, "Swamp Thing 001 (1986).cbz")
    obsolete = folder / "old.cbz"
    obsolete.mkdir()
    context["files"] = [{"path": str(obsolete), "issue_id": 11}]
    plan = plan_series_rescan(context)
    assert sorted(item["outcome"] for item in plan) == ["register", "review"]


async def seed_catalog(db_session, context):
    from pullbox.models.issue import Issue, IssueStatus
    from pullbox.models.library import LibraryRoot
    from pullbox.models.series import Series

    root = LibraryRoot(
        id=1, name="Read-only source", path=context["roots"][0]["path"], allow_managed_writes=False
    )
    series = Series(
        id=1,
        title="Swamp Thing",
        sort_title="swamp thing",
        year_start=1986,
        path=context["folder"],
        comicvine_id=123,
        library_root_id=1,
    )
    issue = Issue(id=10, series_id=1, issue_number=1, comicvine_id=1001, status=IssueStatus.WANTED)
    db_session.add_all([root, series, issue])
    await db_session.commit()
    return issue


async def test_registration_is_referenced_owned_and_idempotent(db_session, context):
    from sqlalchemy import select

    from pullbox.models.issue import IssueStatus
    from pullbox.models.library import LibraryFile, LibraryFileStorageMode

    issue = await seed_catalog(db_session, context)
    path = comic(Path(context["folder"]), "Swamp Thing 001 (1986).cbz")
    before = path.read_bytes(), path.stat().st_mtime_ns
    item = plan_series_rescan(context)[0]
    assert (await apply_rescan_match(db_session, 1, item))[0] == "added"
    await db_session.commit()
    assert issue.status == IssueStatus.OWNED
    files = list((await db_session.scalars(select(LibraryFile))).all())
    assert len(files) == 1
    assert files[0].storage_mode == LibraryFileStorageMode.REFERENCED
    assert (await apply_rescan_match(db_session, 1, item))[0] == "unchanged"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


async def test_changed_file_is_not_registered(db_session, context):
    from pullbox.models.issue import IssueStatus

    issue = await seed_catalog(db_session, context)
    path = comic(Path(context["folder"]), "Swamp Thing 001 (1986).cbz")
    item = plan_series_rescan(context)[0]
    path.write_bytes(b"different file")
    outcome, reason = await apply_rescan_match(db_session, 1, item)
    assert outcome == "review"
    assert "changed" in reason.lower()
    assert issue.status == IssueStatus.WANTED


async def test_missing_link_is_repaired_without_deleting_the_record(db_session, context):
    from sqlalchemy import select

    from pullbox.models.library import LibraryFile, LibraryFileStorageMode

    await seed_catalog(db_session, context)
    old = comic(Path(context["folder"]), "Swamp Thing 001 (1986).cbz")
    await apply_rescan_match(db_session, 1, plan_series_rescan(context)[0])
    await db_session.commit()
    record = await db_session.scalar(select(LibraryFile))
    original_id = record.id
    old.rename(old.with_name("Swamp Thing 001 (1986) (digital).cbz"))
    context["files"] = [{"path": str(old), "issue_id": 10}]
    plan = plan_series_rescan(context)
    assert len(plan) == 1
    new_item = plan[0]
    assert (await apply_rescan_match(db_session, 1, new_item))[0] == "repaired"
    assert record.id == original_id
    assert record.file_path == new_item["file_path"]
    assert record.storage_mode == LibraryFileStorageMode.REFERENCED


async def test_existing_owned_copy_is_never_replaced(db_session, context):
    from sqlalchemy import select

    from pullbox.models.library import LibraryFile

    await seed_catalog(db_session, context)
    folder = Path(context["folder"])
    original = comic(folder, "Swamp Thing 001 (1986).cbz")
    await apply_rescan_match(db_session, 1, plan_series_rescan(context)[0])
    await db_session.commit()
    record = await db_session.scalar(select(LibraryFile))
    context["files"] = [{"path": str(original), "issue_id": 10}]
    comic(folder, "Swamp Thing 001 (1986) (digital).cbz")
    plan = plan_series_rescan(context)
    existing = next(item for item in plan if item["file_path"] == str(original))
    duplicate = next(item for item in plan if item["file_path"] != str(original))
    assert (await apply_rescan_match(db_session, 1, existing))[0] == "unchanged"
    assert (await apply_rescan_match(db_session, 1, duplicate))[0] == "review"
    assert record.file_path == str(original)
    assert original.exists()


async def test_wanted_with_a_valid_registered_file_is_repaired(db_session, context):
    from pullbox.models.issue import IssueStatus

    issue = await seed_catalog(db_session, context)
    comic(Path(context["folder"]), "Swamp Thing 001 (1986).cbz")
    item = plan_series_rescan(context)[0]
    await apply_rescan_match(db_session, 1, item)
    await db_session.commit()
    issue.status = IssueStatus.WANTED
    await db_session.commit()
    assert (await apply_rescan_match(db_session, 1, item))[0] == "repaired"
    assert issue.status == IssueStatus.OWNED


async def test_downloading_issue_is_left_alone(db_session, context):
    from pullbox.models.issue import IssueStatus

    issue = await seed_catalog(db_session, context)
    issue.status = IssueStatus.DOWNLOADING
    await db_session.commit()
    comic(Path(context["folder"]), "Swamp Thing 001 (1986).cbz")
    assert (await apply_rescan_match(db_session, 1, plan_series_rescan(context)[0]))[0] == "review"
    assert issue.status == IssueStatus.DOWNLOADING


async def test_unassigned_registered_file_gets_its_proven_issue(db_session, context):
    from sqlalchemy import select

    from pullbox.models.issue import IssueStatus
    from pullbox.models.library import LibraryFile

    issue = await seed_catalog(db_session, context)
    comic(Path(context["folder"]), "Swamp Thing 001 (1986).cbz")
    item = plan_series_rescan(context)[0]
    await apply_rescan_match(db_session, 1, item)
    await db_session.commit()
    record = await db_session.scalar(select(LibraryFile))
    original_id = record.id
    record.issue_id = None
    issue.status = IssueStatus.WANTED
    await db_session.commit()
    assert (await apply_rescan_match(db_session, 1, item))[0] == "repaired"
    assert record.issue_id == issue.id
    assert record.id == original_id
    assert issue.status == IssueStatus.OWNED


def test_conflicting_embedded_identity_requires_review(context):
    comic(
        Path(context["folder"]),
        "Batman 001 (1986).cbz",
        xml=(
            "<ComicInfo><Series>Batman</Series><Number>1</Number><Year>1986</Year>"
            "<Web>https://comicvine.gamespot.com/issue/4000-1001/</Web></ComicInfo>"
        ),
    )
    assert plan_series_rescan(context)[0]["outcome"] == "review"


def test_resource_safety_limit_is_not_bypassed(context):
    comic(Path(context["folder"]), "Swamp Thing 001 (1986).cbz")
    context["max_archive_size"] = 1
    result = plan_series_rescan(context)[0]
    assert result["outcome"] == "review"
    assert "exceeds" in result["reason"]


def test_missing_link_is_a_warning_not_an_ownership_downgrade(context):
    context["files"] = [{"path": str(Path(context["folder"]) / "gone.cbz"), "issue_id": 10}]
    plan = plan_series_rescan(context)
    assert len(plan) == 1
    assert plan[0]["outcome"] == "review"


def test_linked_file_outside_series_is_checked_without_scanning_its_folder(context, tmp_path):
    other = tmp_path / "Mixed folder"
    other.mkdir()
    linked = comic(other, "Swamp Thing 001 (1986).cbz")
    comic(other, "Swamp Thing 013a (1986).cbz")
    context["files"] = [{"path": str(linked), "issue_id": 10}]
    plan = plan_series_rescan(context)
    assert [item["file_path"] for item in plan] == [str(linked)]


def test_outside_root_and_library_root_itself_are_not_scanned(context, tmp_path):
    context["folder"] = str(tmp_path)
    with pytest.raises(ValueError, match="entire library root"):
        plan_series_rescan(context)
    context["roots"] = []
    with pytest.raises(ValueError, match="outside enabled roots"):
        plan_series_rescan(context)


def test_unsafe_archive_and_symlink_are_not_registered(context, tmp_path):
    folder = Path(context["folder"])
    unsafe = comic(folder, "Swamp Thing 001 (1986).cbz")
    with ZipFile(unsafe, "a") as archive:
        archive.writestr("../escape.txt", b"unsafe")
    os.utime(unsafe, (time.time() - 120, time.time() - 120))
    other = comic(tmp_path, "outside.cbz")
    (folder / "Swamp Thing 013a (1986).cbz").symlink_to(other)
    assert {item["outcome"] for item in plan_series_rescan(context)} == {"review"}


async def test_background_queue_registers_and_saves_report(db_session, async_engine, context):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from pullbox.api.v1.series_rescan import series_rescan_report
    from pullbox.ui.utilities_routes import load_utility_history_context
    from pullbox.utilities.executors.series_rescan import SeriesRescanExecutor
    from pullbox.utilities.job_queue import JobQueueManager
    from pullbox.utilities.models import JobState, JobType, UtilityJob

    await seed_catalog(db_session, context)
    comic(Path(context["folder"]), "Swamp Thing 001 (1986).cbz")
    comic(Path(context["folder"]), "Batman 001 (1986).cbz")
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    manager = JobQueueManager(factory)
    manager.register_executor(JobType.SERIES_RESCAN, SeriesRescanExecutor)
    job = await manager.create_job(db_session, JobType.SERIES_RESCAN, "Rescan", {"series_id": 1})
    await db_session.commit()
    await manager.dispatch_next()
    await db_session.refresh(job)
    assert job.state == JobState.COMPLETED
    report = await series_rescan_report(1, db_session, None, page=1)
    assert job.warning_count == 1, report
    assert report["counts"] == {"added": 1, "review": 1}
    assert report["job"]["percent"] == 100
    assert len(report["items"]) == 1
    assert "Batman" in report["items"][0]["path"]
    assert await db_session.get(UtilityJob, job.id) is not None
    history = await load_utility_history_context(db_session)
    assert history["history_jobs"][0]["can_rollback"] is False
    with pytest.raises(ValueError, match="do not support rollback"):
        await manager.queue_rollback_job(db_session, job.id)


def test_rescan_activity_links_to_series_and_finishes():
    from pullbox.services.utility_operation_progress import build_utility_operation_update
    from pullbox.utilities.models import JobState, JobType, UtilityJob

    job = UtilityJob(
        id="scan1",
        job_type=JobType.SERIES_RESCAN,
        display_name="Rescan",
        state=JobState.COMPLETED,
        config='{"series_id":1}',
        total_items=2,
        completed_items=2,
        warning_count=1,
    )
    update = build_utility_operation_update(job)
    assert update.detail_url == "/series/1?rescan=scan1"
    assert update.overall.percent == 100
    assert "1 files need review" in update.message


async def test_repeated_start_reuses_active_job(db_session, async_engine, context, monkeypatch):
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from pullbox.api.v1 import series_rescan
    from pullbox.utilities.job_queue import JobQueueManager

    await seed_catalog(db_session, context)
    manager = JobQueueManager(async_sessionmaker(async_engine, expire_on_commit=False))
    monkeypatch.setattr(series_rescan, "_get_manager", lambda: manager)
    monkeypatch.setattr(series_rescan, "_schedule_dispatch", lambda _: None)
    first = await series_rescan.start_series_rescan(
        1, db_session, SimpleNamespace(username="tester")
    )
    second = await series_rescan.start_series_rescan(
        1, db_session, SimpleNamespace(username="tester")
    )
    assert first["job_id"] == second["job_id"]


async def test_rescan_queue_failure_does_not_downgrade_owned_issues(
    db_session, async_engine, context
):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from pullbox.models.issue import IssueStatus
    from pullbox.models.operation_progress import OperationProgress, OperationProgressState
    from pullbox.utilities.executors.series_rescan import SeriesRescanExecutor
    from pullbox.utilities.job_queue import JobQueueManager
    from pullbox.utilities.models import JobState, JobType

    issue = await seed_catalog(db_session, context)
    issue.status = IssueStatus.OWNED
    await db_session.commit()
    Path(context["folder"]).rmdir()
    manager = JobQueueManager(async_sessionmaker(async_engine, expire_on_commit=False))
    manager.register_executor(JobType.SERIES_RESCAN, SeriesRescanExecutor)
    job = await manager.create_job(db_session, JobType.SERIES_RESCAN, "Rescan", {"series_id": 1})
    await db_session.commit()
    await manager.dispatch_next()
    await db_session.refresh(job)
    await db_session.refresh(issue)
    assert job.state == JobState.FAILED
    assert "unavailable" in job.error_message
    assert issue.status == IssueStatus.OWNED
    activity = await db_session.scalar(
        select(OperationProgress).where(OperationProgress.operation_key == job.id)
    )
    assert activity.state == OperationProgressState.FAILED
