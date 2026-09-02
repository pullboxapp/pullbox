"""Offline saved-review rechecks must preserve decisions and source boundaries."""

from __future__ import annotations

import zipfile

import pytest

from pullbox.core.exceptions import ValidationError
from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.services.import_review_recheck import prepare_review_recheck


async def _fixture(session, tmp_path, source_type):
    folder = tmp_path / "Firefly (2018)"
    folder.mkdir()
    (folder / "series.json").write_text(
        '{"metadata":{"comicid":115251,"name":"Firefly","year":2018}}'
    )
    (folder / "cvinfo").write_text(
        "https://comicvine.gamespot.com/firefly/4050-115251/\nseries_id: 7921"
    )
    job = ImportJob(
        source_path=str(tmp_path), source_type=source_type, status=ImportJobStatus.REVIEW
    )
    session.add(job)
    await session.flush()
    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Firefly",
        raw_year=2018,
        source_folder=str(folder),
        status=ImportSeriesStatus.NO_MATCH,
        diagnostics={"reason": "trusted_source_identity_conflict"},
    )
    session.add(item)
    await session.flush()
    files = []
    for number, pages in [(7, 0), (8, 2), (9, 1)]:
        path = folder / f"Firefly {number:03}.cbz"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "ComicInfo.xml",
                f"<ComicInfo><Series>Firefly</Series><Number>{number}</Number><PageCount>27</PageCount></ComicInfo>",
            )
            for page in range(pages):
                archive.writestr(f"{page}.jpg", b"image")
        file = ImportedFile(
            import_job_id=job.id,
            import_series_id=item.id,
            file_path=str(path),
            file_name=path.name,
            file_size=path.stat().st_size,
            file_format="cbz",
            status=ImportedFileStatus.NO_MATCH,
            source_signature=build_file_identity_signature(path),
            parsed_series="Firefly",
            parsed_issue_number=float(number),
            comicvine_issue_id=100000 + number if source_type is ImportSourceType.MYLAR3 else None,
            diagnostics={
                "comicvine_series_id": 115251 if source_type is ImportSourceType.MYLAR3 else 7921,
                "metadata_signals": {
                    "comicvine_series_id": "mylar3"
                    if source_type is ImportSourceType.MYLAR3
                    else "sidecar",
                    "comicvine_issue_id": "mylar3",
                },
                "source_metadata": {
                    "identity_conflicts": [
                        {"field": "comicvine_series_id", "mylar3": 115251, "sidecar": 7921}
                    ],
                    "mylar3_issue": {"title": "Issue title"},
                },
            },
        )
        session.add(file)
        files.append(file)
    await session.flush()
    return job, item, files


@pytest.mark.parametrize("source_type", list(ImportSourceType))
async def test_recheck_preserves_sources_and_dry_run_then_prepares_only_saved_review(
    db_session, tmp_path, source_type
):
    job, item, files = await _fixture(db_session, tmp_path, source_type)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    report = await prepare_review_recheck(db_session, job.id, source_roots=[tmp_path], apply=False)
    assert report["files_checked"] == 3
    assert report["blocked_files"] == 2
    assert item.status is ImportSeriesStatus.NO_MATCH
    assert all(file.status is ImportedFileStatus.NO_MATCH for file in files)
    assert files[0].diagnostics["source_metadata"]["identity_conflicts"]

    await prepare_review_recheck(db_session, job.id, source_roots=[tmp_path], apply=True)
    assert job.status is ImportJobStatus.MATCHING
    assert item.status is ImportSeriesStatus.PENDING
    assert files[0].diagnostics["safety_block"]["code"] == "archive_no_pages"
    assert files[1].status is ImportedFileStatus.PENDING
    assert files[2].diagnostics["safety_block"]["code"] == "single_page_comic"
    assert all(file.diagnostics["comicvine_series_id"] == 115251 for file in files)
    assert all(not file.diagnostics["source_metadata"].get("identity_conflicts") for file in files)
    assert all(not file.include_in_import for file in files)
    if source_type is ImportSourceType.MYLAR3:
        assert files[1].comicvine_issue_id == 100008
        assert files[1].diagnostics["source_metadata"]["mylar3_issue"]["title"] == "Issue title"
    assert before == {path: path.read_bytes() for path in before}


@pytest.mark.parametrize("choice", ["series", "file", "skip", "selection", "allow_once"])
async def test_recheck_leaves_entire_manually_reviewed_series_untouched(
    db_session, tmp_path, choice
):
    job, item, files = await _fixture(db_session, tmp_path, ImportSourceType.MYLAR3)
    if choice == "series":
        item.user_selected_cv_id = 42
    elif choice == "file":
        files[1].match_method = "manual_override"
    elif choice == "selection":
        item.selected_for_import = True
    elif choice == "allow_once":
        files[1].diagnostics = {
            **files[1].diagnostics,
            "safety_exception": {"allowed_once": True},
        }
    else:
        files[1].status = ImportedFileStatus.SKIPPED
    await db_session.flush()
    report = await prepare_review_recheck(db_session, job.id, source_roots=[tmp_path], apply=True)
    assert report["files_checked"] == 0
    assert report["skipped_series"] == 1
    assert job.status is ImportJobStatus.REVIEW
    assert item.status is ImportSeriesStatus.NO_MATCH


async def test_recheck_keeps_genuine_identity_conflicts(db_session, tmp_path):
    job, _item, files = await _fixture(db_session, tmp_path, ImportSourceType.MYLAR3)
    (tmp_path / "Firefly (2018)" / "cvinfo").write_text(
        "https://comicvine.gamespot.com/other/4050-123456/"
    )
    await prepare_review_recheck(db_session, job.id, source_roots=[tmp_path], apply=True)
    assert files[1].diagnostics["source_metadata"]["identity_conflicts"]


async def test_recheck_does_not_read_outside_explicit_source_roots(db_session, tmp_path):
    job, _item, files = await _fixture(db_session, tmp_path, ImportSourceType.MYLAR3)
    allowed = tmp_path / "empty"
    allowed.mkdir()
    report = await prepare_review_recheck(db_session, job.id, source_roots=[allowed], apply=True)
    assert report["blocked_files"] == 3
    assert all(file.diagnostics["safety_block"]["code"] == "source_outside_root" for file in files)


async def test_recheck_rejects_running_job(db_session, tmp_path):
    job, _, _ = await _fixture(db_session, tmp_path, ImportSourceType.MYLAR3)
    job.status = ImportJobStatus.IMPORTING
    with pytest.raises(ValidationError, match="REVIEW"):
        await prepare_review_recheck(db_session, job.id, source_roots=[tmp_path], apply=True)


@pytest.mark.parametrize("accept_replacement", [False, True])
async def test_recheck_requires_explicit_consent_for_replaced_files(
    db_session, tmp_path, accept_replacement
):
    job, _item, files = await _fixture(db_session, tmp_path, ImportSourceType.MYLAR3)
    from pathlib import Path

    path = Path(files[0].file_path)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("1.jpg", b"replacement")
        archive.writestr("2.jpg", b"replacement")
    await prepare_review_recheck(
        db_session,
        job.id,
        source_roots=[tmp_path],
        apply=True,
        accept_replaced_files=accept_replacement,
    )
    assert files[0].status is (
        ImportedFileStatus.PENDING if accept_replacement else ImportedFileStatus.SAFETY_BLOCKED
    )
    if not accept_replacement:
        assert files[0].diagnostics["safety_block"]["code"] == "source_changed"


async def test_recheck_refuses_symlink_escape(db_session, tmp_path):
    from pathlib import Path

    job, _item, files = await _fixture(db_session, tmp_path, ImportSourceType.MYLAR3)
    outside = tmp_path / "outside.cbz"
    outside.write_bytes(b"must not be opened as a comic")
    path = Path(files[0].file_path)
    path.unlink()
    path.symlink_to(outside)
    await prepare_review_recheck(
        db_session, job.id, source_roots=[path.parent], apply=True, accept_replaced_files=True
    )
    assert files[0].diagnostics["safety_block"]["code"] == "source_outside_root"
