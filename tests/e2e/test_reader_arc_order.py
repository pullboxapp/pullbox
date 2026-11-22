"""Read across series in the saved Story Arc order using real reader endpoints."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZipFile

import pytest
from playwright.sync_api import Page, expect
from sqlalchemy import delete

from pullbox.database import get_session_factory
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.story_arc import IssueStoryArc, StoryArc, StoryArcResolutionState
from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _TEST_COVER_PNG, _run_async_blocking
from tests.e2e.pages.issue_detail import IssueDetailPage

pytestmark = pytest.mark.e2e


@pytest.fixture
def arc_reader(seeded_server: str, tmp_path: Path) -> Iterator[int]:
    path = tmp_path / "Batman Beyond 091.cbz"
    with ZipFile(path, "w") as cbz:
        cbz.writestr("001.png", _TEST_COVER_PNG)
        cbz.writestr("002.png", _TEST_COVER_PNG)
        cbz.writestr("003.png", _TEST_COVER_PNG)

    async def seed() -> tuple[int, int, int, int]:
        async with get_session_factory().begin() as session:
            arc = StoryArc(name="Reader crossover")
            root = LibraryRoot(name="Reader crossover test", path=str(tmp_path))
            chapter = Issue(
                series_id=2, issue_number=91, title="Crossover finale", status=IssueStatus.OWNED
            )
            session.add_all([arc, root, chapter])
            await session.flush()
            book = LibraryFile(
                issue_id=chapter.id,
                library_root_id=root.id,
                file_name=path.name,
                file_path=str(path),
                file_size=path.stat().st_size,
                file_modified_at=datetime.now(UTC),
                file_format=FileFormat.CBZ,
                match_confidence=MatchConfidence.MANUAL,
            )
            session.add(book)
            for i, issue in enumerate((1, chapter.id, None), 1):
                session.add(
                    IssueStoryArc(
                        story_arc_id=arc.id,
                        sequence_number=i,
                        source_ordinal=i,
                        issue_id=issue,
                        source_issue_title="Lost chapter" if issue is None else None,
                        resolution_state=StoryArcResolutionState.RESOLVED
                        if issue
                        else StoryArcResolutionState.MISSING,
                    )
                )
            await session.flush()
            return arc.id, root.id, book.id, chapter.id

    arc_id, root_id, file_id, chapter_id = _run_async_blocking(seed())

    async def cleanup() -> None:
        async with get_session_factory().begin() as session:
            await session.execute(delete(StoryArc).where(StoryArc.id == arc_id))
            await session.execute(delete(LibraryFile).where(LibraryFile.id == file_id))
            await session.execute(delete(LibraryRoot).where(LibraryRoot.id == root_id))
            await session.execute(delete(Issue).where(Issue.id == chapter_id))

    try:
        yield arc_id
    finally:
        _run_async_blocking(cleanup())


def test_read_in_arc_order_then_open_independently(
    authed_page: Page, seeded_server: str, seeded_reader_state_guard: None, arc_reader: int
) -> None:
    page = authed_page
    page.goto(f"{seeded_server}/story-arcs/{arc_reader}?page=1&per_page=1")
    page.get_by_test_id("story-arc-open-issue-1").click()
    issue = IssueDetailPage(page, seeded_server)
    issue.open_reader()
    context = page.get_by_test_id("comic-reader-arc-position")
    expect(context).to_have_text("Reading: Reader crossover · 1 of 3")
    next_issue = page.get_by_test_id("comic-reader-next-issue")
    expect(next_issue).to_have_accessible_name("Next issue, Batman Beyond #91")
    next_issue.press("Enter")
    expect(context).to_have_text("Reading: Reader crossover · 2 of 3")
    expect(next_issue).to_be_disabled()
    expect(page.get_by_test_id("comic-reader-arc-gaps")).to_contain_text("Lost chapter")
    page.get_by_test_id("comic-reader-previous-issue").press("Enter")
    expect(context).to_have_text("Reading: Reader crossover · 1 of 3")
    issue.close_reader()
    expect(page.get_by_test_id("issue-detail-back-link")).to_have_attribute(
        "href", f"/story-arcs/{arc_reader}?page=1&per_page=1"
    )
    issue.goto(1)
    issue.open_reader()
    expect(context).not_to_be_visible()
    expect(next_issue).to_be_disabled()


@pytest.mark.parametrize("theme", ["light", "dark", "tron"])
def test_arc_action_resumes_bookmarked_chapter_and_shared_page(
    authed_page: Page,
    seeded_server: str,
    seeded_reader_state_guard: None,
    arc_reader: int,
    theme: str,
) -> None:
    page = authed_page
    page.add_init_script(f"localStorage.setItem('pullbox-theme', '{theme}')")
    if theme == "dark":
        page.set_viewport_size({"width": 390, "height": 844})
    url = f"{seeded_server}/story-arcs/{arc_reader}?page=1&per_page=1"
    page.goto(url)
    page.get_by_role("link", name="Read story arc", exact=True).click(timeout=5000)
    context = page.get_by_test_id("comic-reader-arc-position")
    expect(context).to_have_text("Reading: Reader crossover · 1 of 3")
    page.get_by_test_id("comic-reader-next-issue").click()
    expect(context).to_have_text("Reading: Reader crossover · 2 of 3")
    with page.expect_response(
        lambda r: (
            r.url.endswith("/progress")
            and r.request.method == "PUT"
            and r.request.post_data_json["page_index"] == 1
        )
    ) as saved:
        page.get_by_test_id("comic-reader-next").click()
    assert saved.value.ok
    page.goto(url)
    expect(page.locator("html")).to_have_attribute("data-theme", theme)
    resume = page.get_by_role("link", name="Continue story arc", exact=True)
    resume.focus()
    expect(resume).to_be_focused()
    assert_no_axe_violations(page, name=f"arc-resume-{theme}", include=['a[href*="read=1"]'])
    resume.press("Enter")
    expect(context).to_have_text("Reading: Reader crossover · 2 of 3")
    expect(page.get_by_test_id("comic-reader-page-status")).to_have_text("Page 2 of 3")
