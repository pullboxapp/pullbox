"""Step 3 never preselects an inherited issue number against a foreign file title."""

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from pullbox.database import get_session_factory
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from tests.e2e.conftest import _run_async_blocking
from tests.e2e.pages.import_page import ImportPage
from tests.unit.test_import_service import TestImportReconciliation as _Reconciliation

pytestmark = pytest.mark.e2e


async def _seed(number, base):
    async with get_session_factory()() as session:
        job, item, file = await _Reconciliation._seed_reconcile_row(session)
        series = Series(title="Fritzi Ritz", sort_title="Fritzi Ritz", comicvine_id=base)
        session.add(series)
        await session.flush()
        session.add(Issue(series_id=series.id, issue_number=21, comicvine_id=base + 1))
        item.raw_series_name = item.cv_title = file.parsed_series = series.title
        item.series_id = series.id
        item.cv_id = item.user_selected_cv_id = base
        item.cv_match_method = "user_override"
        file.file_name = f"042 - Thunderbolts {number:03d} (converted).cbz"
        file.parsed_issue_number = number
        await session.commit()
        return job.id, item.id, file.id


@pytest.mark.parametrize("number", [21, 105])
def test_foreign_file_can_be_skipped_without_suggesting_a_folder_issue(
    authed_page, seeded_server, browser_name, number
):
    base = 91000000 + 10 * number + (10000 if browser_name == "firefox" else 0)
    job, item, file = _run_async_blocking(_seed(number, base))
    page = authed_page
    page.goto(f"{seeded_server}/import?tab=collection&resume_job_id={job}&resume_step=3")
    review = ImportPage(page, seeded_server)
    review.click_review_control(page.get_by_test_id("import-review-lane-decide"))
    row = page.locator(f'[data-import-review-series-row="{item}"]')
    row.get_by_test_id("import-review-primary-action").click()
    expect(row.get_by_test_id("import-review-reassign-file")).to_be_visible()
    row.get_by_test_id("import-review-reconcile-action").click()
    modal = page.get_by_test_id("import-reconcile-modal")
    expect(modal.get_by_text("This file names a different series.", exact=False)).to_be_visible()
    expect(modal.get_by_test_id(f"import-reconcile-issue-input-{file}")).to_have_value("")
    expect(modal.get_by_test_id(f"import-reconcile-provisional-{file}")).to_have_count(0)
    modal.get_by_test_id(f"import-reconcile-skip-{file}").click()
    with page.expect_response(
        lambda response: (
            response.url.endswith(f"/series/{item}/reconcile") and response.request.method == "POST"
        )
    ) as result:
        modal.get_by_test_id("import-reconcile-save").click()
    assert result.value.ok

    async def saved():
        async with get_session_factory()() as session:
            return (
                await session.execute(
                    select(ImportedFile.status, ImportedFile.matched_issue_id).where(
                        ImportedFile.id == file
                    )
                )
            ).one()

    assert _run_async_blocking(saved()) == (ImportedFileStatus.SKIPPED, None)


@pytest.mark.parametrize("conflicting_ids", [False, True])
def test_retry_failed_shows_the_mixed_file_for_a_decision_without_reloading(
    authed_page, seeded_server, browser_name, conflicting_ids
):
    base = 91400000 + int(conflicting_ids) * 10 + (10000 if browser_name == "firefox" else 0)
    job, group, file = _run_async_blocking(_seed(21, base))

    async def mark_failed():
        async with get_session_factory().begin() as session:
            record = await session.get(ImportJob, job)
            item = await session.get(ImportedSeries, group)
            source = await session.get(ImportedFile, file)
            record.status, record.source_type = ImportJobStatus.COMPLETED, ImportSourceType.MYLAR3
            item.status, item.cv_match_method = ImportSeriesStatus.IMPORTED, "mylar3_cv_id"
            item.files_no_match = 0
            source.status = ImportedFileStatus.FAILED
            source.error_message = "Could not resolve to a library issue"
            if conflicting_ids:
                source.matched_issue_cv_id, source.comicvine_issue_id = base + 1, base + 2
                session.add(Issue(series_id=item.series_id, issue_number=22, comicvine_id=base + 2))

    _run_async_blocking(mark_failed())
    page = authed_page
    page.goto(f"{seeded_server}/import?tab=follow-up&job_id={job}")
    recover = page.get_by_test_id(f"import-orphaned-recover-{group}")
    expect(recover).to_have_count(0)
    with page.expect_response(f"**/api/v1/import/{job}/retry-failed") as response:
        page.get_by_test_id("import-results-retry-action").click()
    assert response.value.ok
    assert response.value.json()["retrying_count"] == 0
    expect(recover).to_be_visible()
    recover.click()
    row = page.get_by_test_id(f"orphaned-recovery-file-{file}")
    expect(row.get_by_test_id(f"orphaned-recovery-issue-input-{file}")).to_have_value("")
    if conflicting_ids:

        async def stored_evidence():
            async with get_session_factory()() as session:
                source = await session.get(ImportedFile, file)
                return source.matched_issue_cv_id, source.comicvine_issue_id

        assert _run_async_blocking(stored_evidence()) == (base + 1, base + 2)
    row.get_by_test_id(f"orphaned-recovery-skip-{file}").click()
    page.get_by_test_id("orphaned-recovery-save").click()
    expect(recover).to_have_count(0)
    page.reload()
    expect(page.get_by_test_id("import-follow-up-job-panel")).to_be_visible()
    expect(recover).to_have_count(0)

    async def saved():
        async with get_session_factory()() as session:
            source = await session.get(ImportedFile, file)
            return source.status, source.matched_issue_id, source.library_file_id

    assert _run_async_blocking(saved()) == (ImportedFileStatus.SKIPPED, None, None)


def test_follow_up_picker_shows_both_lettered_issues(authed_page, seeded_server, browser_name):
    base = 91300000 + (10000 if browser_name == "firefox" else 0)

    async def seed():
        async with get_session_factory().begin() as session:
            job, group, file = await _Reconciliation._seed_reconcile_row(session)
            job.status = ImportJobStatus.COMPLETED
            series = Series(title="X-O Manowar", sort_title="X-O Manowar", comicvine_id=base)
            session.add(series)
            await session.flush()
            group.status = ImportSeriesStatus.RECOVERY_PENDING
            group.series_id, group.cv_id, group.cv_title = series.id, base, series.title
            for index, exact in enumerate(("50-X", "50-O"), start=1):
                session.add(
                    Issue(
                        series_id=series.id,
                        issue_number=50,
                        issue_number_text=exact,
                        comicvine_id=base + index,
                    )
                )
            file.file_name = "X-O Manowar 50-o.cbz"
            file.issue_number_raw, file.parsed_issue_number = "50-o", 50
            return job.id, group.id, file.id

    job, group, file = _run_async_blocking(seed())
    authed_page.goto(f"{seeded_server}/import?tab=follow-up&job_id={job}")
    authed_page.get_by_test_id(f"import-orphaned-recover-{group}").click()
    row = authed_page.get_by_test_id(f"orphaned-recovery-file-{file}")
    expect(row.get_by_text("Parsed #50-O", exact=True)).to_be_visible()
    expect(row.get_by_text("Suggested #50-O", exact=True)).to_be_visible()
    expect(row.get_by_test_id(f"orphaned-recovery-issue-input-{file}")).to_have_value(str(base + 2))
    row.get_by_test_id(f"orphaned-recovery-issue-trigger-{file}").click()
    for exact in ("50-X", "50-O"):
        expect(authed_page.get_by_role("option", name=f"#{exact}", exact=True)).to_be_visible()
    authed_page.get_by_role("option", name="#50-X", exact=True).click()
    expect(row.get_by_test_id(f"orphaned-recovery-issue-input-{file}")).to_have_value(str(base + 1))
    authed_page.get_by_role("button", name="Close recovery dialog", exact=True).click()
    authed_page.get_by_test_id(f"import-orphaned-recover-{group}").click()
    expect(row.get_by_test_id(f"orphaned-recovery-issue-input-{file}")).to_have_value(str(base + 2))
