"""Step 3 never preselects an inherited issue number against a foreign file title."""

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from pullbox.database import get_session_factory
from pullbox.models.import_job import ImportedFile, ImportedFileStatus
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
