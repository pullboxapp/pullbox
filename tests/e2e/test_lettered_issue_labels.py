"""Exact issue designations remain visible when navigating the library."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def lettered_series(seeded_server):
    from pullbox.database import get_session_factory
    from pullbox.models.issue import Issue, IssueStatus
    from pullbox.models.series import Series
    from tests.e2e.conftest import _run_async_blocking

    async def seed():
        async with get_session_factory()() as session:
            series = Series(title="Gen13 Lettered Fixture", sort_title="Gen13 Lettered Fixture")
            session.add(series)
            await session.flush()
            issues = [
                Issue(
                    series_id=series.id,
                    issue_number=13,
                    issue_number_text=number,
                    status=IssueStatus.WANTED,
                )
                for number in ["13A", "13B", "13C"]
            ]
            session.add_all(issues)
            await session.commit()
            return series.id, issues[0].id

    return _run_async_blocking(seed())


def test_lettered_issues_keep_exact_labels_between_library_and_detail(
    authed_page, seeded_server, lettered_series, browser_name
):
    series_id, issue_id = lettered_series
    page = authed_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{seeded_server}/series/{series_id}")
    table = page.get_by_test_id("series-detail-issues-table")
    for number in ["13A", "13B", "13C"]:
        expect(table).to_contain_text(f"#{number}")
    assert "13A" in page.locator(f"#issue-{issue_id}").get_by_test_id(
        "series-issue-manual-search"
    ).get_attribute("@click")
    output = Path(__file__).resolve().parents[2] / "test-results/lettered-issues"
    output.mkdir(parents=True, exist_ok=True)
    table.screenshot(path=str(output / f"{browser_name}-series.png"))
    page.goto(f"{seeded_server}/issues/{issue_id}")
    expect(page.get_by_test_id("issue-detail-page")).to_contain_text("#13A")
    assert errors == []
