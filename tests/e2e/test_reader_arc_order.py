"""Read across series in the saved Story Arc order using real reader endpoints."""

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZipFile

import pytest
from playwright.sync_api import Page, Route, expect
from sqlalchemy import delete, select

from pullbox.database import get_session_factory
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcPlacement,
    StoryArcResolutionState,
)
from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _TEST_COVER_PNG, _run_async_blocking
from tests.e2e.pages.issue_detail import IssueDetailPage
from tests.e2e.pages.series_detail import SeriesDetailPage

pytestmark = pytest.mark.e2e


def test_switch_order_preserves_the_displayed_page_and_validates_before_switching(
    authed_page: Page, seeded_server: str, seeded_reader_state_guard: None, arc_reader: int
) -> None:
    page = authed_page
    issue = IssueDetailPage(page, seeded_server)
    issue.goto(1)
    issue.open_reader()
    expect(issue.reader_status).to_have_text("Page 2 of 3")
    issue.reader_page.evaluate("el => window.originalReaderImage = el")
    original_url = issue.reader_page.get_attribute("src")
    order = page.get_by_role("combobox", name="Reading order", exact=True)
    expect(order).to_have_value("series")
    next_issue = page.get_by_test_id("comic-reader-next-issue")
    expect(next_issue).to_be_disabled()
    page.get_by_test_id("comic-reader-fit-width").click()
    failed = "**/api/v1/reader/issues/1/manifest?story_arc_id=*"
    page.route(failed, lambda route: route.fulfill(status=409, json={"detail": "Arc changed."}))
    order.select_option(label="Reader crossover")
    expect(page.get_by_test_id("comic-reader-switch-error")).to_have_text("Arc changed.")
    expect(order).to_have_value("series")
    expect(next_issue).to_be_disabled()
    page.unroute(failed)

    def changed_archive(route: Route) -> None:
        manifest = route.fetch().json()
        route.fulfill(json={**manifest, "revision": "different-archive"})

    page.route(failed, changed_archive)
    order.select_option(label="Reader crossover")
    expect(page.get_by_test_id("comic-reader-switch-error")).to_contain_text("The comic changed.")
    expect(order).to_have_value("series")
    expect(issue.reader_page).to_have_attribute("src", original_url)
    page.unroute(failed)
    order.select_option(label="Reader crossover")
    expect(next_issue).to_have_accessible_name("Next issue, Batman Beyond #91")
    expect(issue.reader_status).to_have_text("Page 2 of 3")
    expect(issue.reader_page).to_have_attribute("src", original_url)
    expect(issue.reader_page).to_have_attribute("data-fit-mode", "width")
    assert issue.reader_page.evaluate("el => el === window.originalReaderImage")
    expect(order).to_have_value(str(arc_reader))
    page.reload()
    expect(page.get_by_test_id("comic-reader-arc-position")).to_have_text(
        "Reading: Reader crossover · 1 of 3"
    )
    page.reload()
    expect(order).to_have_value(str(arc_reader))
    expect(issue.reader_status).to_have_text("Page 2 of 3")
    order.select_option(value="series")
    expect(next_issue).to_be_disabled()
    expect(page.get_by_test_id("comic-reader-arc-position")).not_to_be_visible()
    issue.close_reader()
    expect(page.get_by_test_id("issue-detail-back-link")).to_have_text("Back to series")


def test_switch_saves_displayed_page_or_leaves_order_unchanged(
    authed_page: Page, seeded_server: str, seeded_reader_state_guard: None, arc_reader: int
) -> None:
    page = authed_page
    issue = IssueDetailPage(page, seeded_server)
    issue.goto(1)
    issue.open_reader()
    expect(issue.reader_status).to_have_text("Page 2 of 3")
    order = page.get_by_role("combobox", name="Reading order", exact=True)
    progress = "**/api/v1/reader/issues/1/progress"
    page.route(progress, lambda route: route.fulfill(status=503, body="Unavailable"))
    page.get_by_test_id("comic-reader-next").click()
    expect(issue.reader_status).to_have_text("Page 3 of 3")
    order.select_option(label="Reader crossover")
    expect(page.get_by_test_id("comic-reader-switch-error")).to_contain_text("not saved")
    expect(order).to_have_value("series")
    expect(issue.reader_status).to_have_text("Page 3 of 3")
    page.unroute(progress)
    order.select_option(label="Reader crossover")
    expect(order).to_have_value(str(arc_reader))
    expect(issue.reader_status).to_have_text("Page 3 of 3")
    assert (
        page.request.get(f"{seeded_server}/api/v1/reader/issues/1/manifest").json()["state"][
            "page_index"
        ]
        == 2
    )


def test_switched_order_follows_next_issue_and_returns_to_arc_on_mobile(
    authed_page: Page, seeded_server: str, seeded_reader_state_guard: None, arc_reader: int
) -> None:
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    issue = IssueDetailPage(page, seeded_server)
    issue.goto(1)
    issue.open_reader()
    order = page.get_by_role("combobox", name="Reading order", exact=True)
    order.focus()
    expect(order).to_be_focused()
    assert_no_axe_violations(
        page, name="reader-order-mobile", include=[".comic-reader__order-select"]
    )
    order.press("r")
    expect(order).to_have_value(str(arc_reader))
    page.get_by_test_id("comic-reader-next-issue").press("Enter")
    expect(page.get_by_test_id("comic-reader-arc-position")).to_contain_text("2 of 3")
    page.reload()
    expect(page.get_by_test_id("comic-reader-arc-position")).to_contain_text("2 of 3")
    issue.close_reader()
    back = page.get_by_test_id("issue-detail-back-link")
    expect(back).to_have_text("Back to story arc")
    back.click()
    expect(page).to_have_url(re.compile(rf"/story-arcs/{arc_reader}(?:\?|$)"))


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
                file_modified_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
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


@pytest.mark.parametrize("theme", ["light", "dark", "tron"])
def test_series_resume_is_independent_of_arc_position_and_list_filter(
    authed_page: Page,
    seeded_server: str,
    seeded_reader_state_guard: None,
    arc_reader: int,
    theme: str,
) -> None:
    async def move_second_chapter(series_id: int) -> int:
        async with get_session_factory().begin() as session:
            issue = (
                await session.scalars(
                    select(Issue)
                    .join(IssueStoryArc)
                    .where(
                        IssueStoryArc.story_arc_id == arc_reader, IssueStoryArc.sequence_number == 2
                    )
                )
            ).one()
            issue.series_id = series_id
            return issue.id

    second = _run_async_blocking(move_second_chapter(1))
    page = authed_page
    page.add_init_script(f"localStorage.setItem('pullbox-theme', '{theme}')")
    if theme == "dark":
        page.set_viewport_size({"width": 390, "height": 844})
    issue_page = IssueDetailPage(page, seeded_server)
    url = f"{seeded_server}/series/1?issue_status=wanted"
    page.goto(url)
    start = page.get_by_role("link", name="Read series", exact=True)
    expect(start).to_have_attribute("href", re.compile(r"^/issues/1\?read=1"))
    start.click()
    expect(issue_page.reader_status).to_have_text("Page 2 of 3")
    page.get_by_test_id("comic-reader-next-issue").click()
    with page.expect_response(
        lambda r: r.request.method == "PUT" and r.url.endswith(f"/{second}/progress")
    ):
        page.get_by_test_id("comic-reader-next").click()
        expect(issue_page.reader_status).to_have_text("Page 2 of 3")
    issue_page.close_reader()
    page.goto(f"{seeded_server}/story-arcs/{arc_reader}")
    with page.expect_response(
        lambda r: r.request.method == "PUT" and r.url.endswith("/1/progress")
    ):
        page.get_by_role("link", name="Read story arc", exact=True).click()
    page.goto(url)
    resume = page.get_by_role("link", name="Continue series", exact=True)
    expect(resume).to_have_attribute("href", re.compile(rf"^/issues/{second}\?read=1"))
    resume.focus()
    expect(resume).to_be_focused()
    expect(page.locator("html")).to_have_attribute("data-theme", theme)
    assert_no_axe_violations(
        page, name=f"series-resume-{theme}", include=["[data-testid='series-action-read']"]
    )
    resume.press("Enter")
    expect(issue_page.reader_status).to_have_text("Page 2 of 3")
    expect(page.get_by_test_id("comic-reader-arc-position")).not_to_be_visible()
    issue_page.close_reader()
    page.goto(url)
    expect(resume).to_be_visible()
    _run_async_blocking(move_second_chapter(2))
    resume.click()
    expect(page.get_by_text("Your bookmarked issue is unavailable.", exact=False)).to_be_visible()
    expect(resume).not_to_be_visible()
    expect(issue_page.reader_dialog).not_to_be_visible()


@pytest.mark.parametrize("theme", ["light", "dark", "tron"])
def test_reader_explicitly_continues_in_either_direction_across_a_gap(
    authed_page: Page,
    seeded_server: str,
    seeded_reader_state_guard: None,
    arc_reader: int,
    theme: str,
) -> None:
    async def separate_chapters() -> None:
        async with get_session_factory().begin() as session:
            second = (
                await session.scalars(
                    select(IssueStoryArc).where(
                        IssueStoryArc.story_arc_id == arc_reader, IssueStoryArc.sequence_number == 2
                    )
                )
            ).one()
            second.sequence_number = 4
            session.add(
                IssueStoryArc(
                    story_arc_id=arc_reader,
                    sequence_number=5,
                    source_ordinal=5,
                    source_issue_title="Missing finale",
                    resolution_state=StoryArcResolutionState.MISSING,
                )
            )

    _run_async_blocking(separate_chapters())
    page = authed_page
    page.add_init_script(f"localStorage.setItem('pullbox-theme', '{theme}')")
    if theme == "dark":
        page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{seeded_server}/story-arcs/{arc_reader}?page=1&per_page=1")
    page.get_by_role("link", name="Read story arc", exact=True).click()
    context = page.get_by_test_id("comic-reader-arc-position")
    expect(context).to_contain_text("1 of 4")
    expect(page.get_by_test_id("comic-reader-next-issue")).to_be_disabled()
    expect(page.get_by_test_id("comic-reader-arc-gaps")).to_contain_text("Lost chapter")
    forward = page.get_by_role("button", name=re.compile("^Continue to next available"))
    expect(forward).to_have_accessible_name(re.compile("Batman Beyond #91"))
    forward.focus()
    expect(forward).to_be_focused()
    assert_no_axe_violations(
        page, name=f"reader-arc-gap-{theme}", include=["[data-testid='comic-reader-gap-actions']"]
    )
    if theme == "light":
        progress = "**/api/v1/reader/issues/1/progress"
        page.route(progress, lambda route: route.fulfill(status=503, body="Unavailable"))
        page.get_by_test_id("comic-reader-previous").click()
        expect(page.get_by_test_id("comic-reader-page-status")).to_have_text("Page 1 of 3")
        forward.click()
        expect(page.get_by_test_id("comic-reader-switch-error")).to_contain_text("reading position")
        expect(context).to_contain_text("1 of 4")
        page.unroute(progress)
    forward.press("Enter")
    expect(context).to_contain_text("3 of 4")
    expect(page.get_by_test_id("comic-reader-previous-issue")).to_be_disabled()
    expect(forward).not_to_be_visible()
    expect(page.get_by_test_id("comic-reader-arc-gaps")).to_contain_text("Missing finale")
    expect(page.get_by_test_id("comic-reader-arc-gaps")).to_contain_text("No later readable entry")
    page.get_by_role("button", name=re.compile("^Return to previous available")).press("Enter")
    expect(context).to_contain_text("1 of 4")
    page.get_by_role("dialog").get_by_role("link", name="Back to story arc").click()
    expect(page).to_have_url(f"{seeded_server}/story-arcs/{arc_reader}?page=1&per_page=1")


def test_damaged_next_chapter_keeps_current_page_and_offers_return(
    authed_page: Page,
    seeded_server: str,
    seeded_reader_state_guard: None,
    arc_reader: int,
    tmp_path: Path,
) -> None:
    damaged = tmp_path / "Batman Beyond 091.cbz"
    damaged.write_bytes(b"not a readable comic archive")
    page = authed_page
    arc_url = f"{seeded_server}/story-arcs/{arc_reader}?page=1&per_page=1"
    page.goto(arc_url)
    page.get_by_role("link", name="Read story arc", exact=True).click()
    issue = IssueDetailPage(page, seeded_server)
    expect(issue.reader_status).to_have_text("Page 2 of 3")
    current = issue.reader_page.get_attribute("src")
    page.get_by_test_id("comic-reader-next-issue").click()
    expect(page.get_by_test_id("comic-reader-switch-error")).to_contain_text("Batman Beyond #91")
    expect(page.get_by_test_id("comic-reader-switch-error")).to_contain_text(
        "No later readable entry"
    )
    expect(issue.reader_page).to_have_attribute("src", current)
    expect(issue.reader_status).to_have_text("Page 2 of 3")
    page.get_by_role("dialog").get_by_role("link", name="Back to story arc").click()
    expect(page).to_have_url(arc_url)
    assert damaged.read_bytes() == b"not a readable comic archive"


@pytest.fixture
def later_reader_chapter(arc_reader: int, tmp_path: Path) -> Iterator[int]:
    path = tmp_path / "Batman Beyond 092.cbz"
    path.write_bytes((tmp_path / "Batman Beyond 091.cbz").read_bytes())

    async def seed() -> tuple[int, int, int]:
        async with get_session_factory().begin() as session:
            root = (
                await session.scalars(
                    select(LibraryRoot.id).where(LibraryRoot.path == str(tmp_path))
                )
            ).one()
            issue = Issue(series_id=2, issue_number=92, status=IssueStatus.OWNED)
            session.add(issue)
            await session.flush()
            book = LibraryFile(
                issue_id=issue.id,
                library_root_id=root,
                file_name=path.name,
                file_path=str(path),
                file_size=path.stat().st_size,
                file_modified_at=datetime.now(UTC),
                file_format=FileFormat.CBZ,
                match_confidence=MatchConfidence.MANUAL,
            )
            member = IssueStoryArc(
                story_arc_id=arc_reader,
                issue_id=issue.id,
                sequence_number=4,
                source_ordinal=4,
                resolution_state=StoryArcResolutionState.RESOLVED,
            )
            session.add_all([book, member])
            await session.flush()
            return issue.id, book.id, member.id

    issue_id, book_id, entry_id = _run_async_blocking(seed())

    async def cleanup() -> None:
        async with get_session_factory().begin() as session:
            await session.execute(delete(IssueStoryArc).where(IssueStoryArc.id == entry_id))
            await session.execute(delete(LibraryFile).where(LibraryFile.id == book_id))
            await session.execute(delete(Issue).where(Issue.id == issue_id))

    try:
        yield issue_id
    finally:
        _run_async_blocking(cleanup())


def test_reader_rechecks_damaged_chapter_before_an_explicit_jump(
    authed_page: Page,
    seeded_server: str,
    seeded_reader_state_guard: None,
    arc_reader: int,
    later_reader_chapter: int,
    tmp_path: Path,
) -> None:
    page = authed_page
    damaged = tmp_path / "Batman Beyond 091.cbz"
    original = damaged.read_bytes()
    with ZipFile(damaged, "w") as cbz:
        cbz.writestr("001.png", b"not an image")
    page.goto(f"{seeded_server}/story-arcs/{arc_reader}")
    page.get_by_role("link", name="Read story arc", exact=True).click()
    issue = IssueDetailPage(page, seeded_server)
    expect(issue.reader_status).to_have_text("Page 2 of 3")
    current = issue.reader_page.get_attribute("src")
    next_issue = page.get_by_test_id("comic-reader-next-issue")
    next_issue.click()
    jump = page.get_by_role("button", name="Continue to next available: Batman Beyond #92")
    expect(jump).to_be_visible()
    expect(issue.reader_page).to_have_attribute("src", current)

    damaged.write_bytes(original)
    jump.click()
    expect(page.get_by_test_id("comic-reader-switch-error")).to_contain_text("can now be opened")
    expect(jump).not_to_be_visible()
    expect(issue.reader_page).to_have_attribute("src", current)

    damaged.unlink()
    next_issue.click()
    expect(jump).to_be_visible()

    async def set_finale_resolved(resolved: bool) -> None:
        async with get_session_factory().begin() as session:
            finale = (
                await session.scalars(
                    select(IssueStoryArc).where(
                        IssueStoryArc.story_arc_id == arc_reader,
                        IssueStoryArc.sequence_number == 4,
                    )
                )
            ).one()
            finale.issue_id = later_reader_chapter if resolved else None
            finale.resolution_state = (
                StoryArcResolutionState.RESOLVED if resolved else StoryArcResolutionState.MISSING
            )

    _run_async_blocking(set_finale_resolved(False))
    jump.click()
    expect(page.get_by_test_id("comic-reader-switch-error")).to_contain_text(
        "reading order changed"
    )
    expect(jump).not_to_be_visible()
    expect(issue.reader_page).to_have_attribute("src", current)
    _run_async_blocking(set_finale_resolved(True))
    next_issue.click()
    jump.press("Enter")
    context = page.get_by_test_id("comic-reader-arc-position")
    expect(context).to_contain_text("4 of 4")
    back = page.get_by_role("button", name="Return to previous available: Batman Beyond #91")
    back.click()
    page.get_by_role("button", name="Return to previous available: Batman #1").press("Enter")
    expect(context).to_contain_text("1 of 4")
    expect(issue.reader_status).to_have_text("Page 2 of 3")
    assert not damaged.exists()


@pytest.mark.parametrize("theme", ["light", "dark", "tron"])
def test_review_arc_file_paths_before_deleting_or_cancelling(
    authed_page: Page, seeded_server: str, arc_reader: int, theme: str
) -> None:
    async def seed() -> None:
        async with get_session_factory().begin() as db:
            member = await db.scalar(
                select(IssueStoryArc.id)
                .where(IssueStoryArc.story_arc_id == arc_reader)
                .order_by(IssueStoryArc.id)
                .limit(1)
            )
            for n in range(21):
                db.add(
                    StoryArcPlacement(
                        issue_story_arc_id=member, placement_path=f"/arc-review/sample-{n:02d}.cbz"
                    )
                )

    _run_async_blocking(seed())
    page = authed_page
    page.add_init_script(f"localStorage.setItem('pullbox-theme', '{theme}')")
    if theme == "dark":
        page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{seeded_server}/story-arcs/{arc_reader}")
    opener = page.get_by_role("button", name="Delete Story Arc", exact=True)
    opener.click()
    dialog = page.get_by_role("dialog", name="Delete Reader crossover?")
    dialog.get_by_role("button", name="Review arc files").click(timeout=5000)
    expect(dialog.get_by_text("/arc-review/sample-00.cbz", exact=True)).to_be_visible()
    expect(page.locator("html")).to_have_attribute("data-theme", theme)
    expect(dialog.get_by_role("button", name="Cancel")).to_be_in_viewport(ratio=1)
    expect(dialog.get_by_role("button", name="Delete arc only")).to_be_in_viewport(ratio=1)
    assert_no_axe_violations(page, name=f"arc-delete-files-{theme}", include=["dialog[open]"])
    dialog.get_by_role("button", name="Next", exact=True).click()
    expect(dialog.get_by_text("/arc-review/sample-20.cbz", exact=True)).to_be_visible()
    expect(dialog.get_by_text("/arc-review/sample-00.cbz", exact=True)).to_have_count(0)
    dialog.get_by_role("button", name="Previous", exact=True).click()
    expect(dialog.get_by_text("/arc-review/sample-00.cbz", exact=True)).to_be_visible()
    dialog.get_by_role("button", name="Cancel", exact=True).click()
    expect(opener).to_be_focused()
    opener.click()
    expect(dialog.get_by_role("button", name="Review arc files")).to_have_attribute(
        "aria-expanded", "false"
    )
    dialog.press("Escape")
    expect(opener).to_be_focused()


@pytest.mark.parametrize("submit", [False, True], ids=["cancel", "confirm"])
def test_review_shared_member_comic_before_removing_it(
    authed_page: Page, seeded_server: str, arc_reader: int, tmp_path: Path, submit: bool
) -> None:
    path = tmp_path / "Batman Beyond 091.cbz"
    original = path.read_bytes()

    async def seed_other() -> int:
        async with get_session_factory().begin() as db:
            issue_id = await db.scalar(
                select(LibraryFile.issue_id).where(LibraryFile.file_path == str(path))
            )
            other = StoryArc(name="Another reading order")
            db.add(
                IssueStoryArc(
                    story_arc=other,
                    issue_id=issue_id,
                    sequence_number=1,
                    source_ordinal=1,
                    resolution_state=StoryArcResolutionState.RESOLVED,
                )
            )
            await db.flush()
            return other.id

    async def cleanup(other_id: int) -> None:
        async with get_session_factory().begin() as db:
            await db.execute(delete(StoryArc).where(StoryArc.id == other_id))

    other_id = _run_async_blocking(seed_other())
    page = authed_page
    try:
        page.goto(f"{seeded_server}/story-arcs/{arc_reader}")
        opener = page.get_by_role("button", name="Delete Story Arc", exact=True)
        opener.click()
        dialog = page.get_by_role("dialog", name="Delete Reader crossover?")
        if not submit:
            related = dialog.get_by_role("button", name="Review related series")
            related.click(timeout=2500)
            series_scope = dialog.get_by_role("region", name="Related series impact")
            row = series_scope.get_by_role("listitem").filter(
                has=page.get_by_text("Batman Beyond", exact=True)
            )
            expect(row.get_by_text("1 confirmed issue in this arc", exact=True)).to_be_visible()
            row.get_by_role("button", name="Review other reading orders").click()
            expect(row.get_by_text("Another reading order", exact=True)).to_be_visible()
            expect(dialog.get_by_role("button", name="Delete arc only")).to_be_enabled()
            related.click()
        page.route(
            f"**/story-arcs/{arc_reader}/delete/member-files?*",
            lambda r: r.fulfill(status=503),
            times=1,
        )
        dialog.get_by_role("button", name="Review library comics").click(timeout=5000)
        expect(dialog.get_by_text("Could not load library comics.")).to_be_visible()
        dialog.get_by_role("button", name="Retry", exact=True).click()
        book = dialog.get_by_role("listitem").filter(
            has=dialog.page.get_by_text(str(path), exact=True)
        )
        expect(book.get_by_text("Managed file", exact=True)).to_be_visible()
        book.get_by_role("button", name="Used by 1 other arc", exact=True).click()
        expect(book.get_by_text("Another reading order", exact=True)).to_be_visible()
        selection = book.get_by_role("checkbox", name="Remove library file " + str(path))
        expect(selection).not_to_be_checked()
        selection.check()
        dialog.get_by_role("button", name="Review selected files").click()
        confirm = dialog.get_by_role("button", name="Delete arc and selected files")
        expect(confirm).to_be_disabled()
        consent = dialog.get_by_role("checkbox", name="I understand the impact on these other arcs")
        expect(consent).not_to_be_checked()
        consent.check()
        expect(confirm).to_be_enabled()
        expect(confirm).to_have_css("opacity", "1")
        assert_no_axe_violations(page, name="arc-delete-library-review", include=["dialog[open]"])
        if submit:
            confirm.click()
            expect(page).to_have_url(re.compile(r"/utilities\?tab=queue$"))
            import time

            for _ in range(100):
                if not path.exists():
                    break
                time.sleep(0.05)
            assert not path.exists()
            for _ in range(100):
                if (
                    page.request.get(
                        f"{seeded_server}/story-arcs/{arc_reader}/delete/preview"
                    ).status
                    == 404
                ):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("The queued deletion did not remove the confirmed arc")
            assert page.request.get(f"{seeded_server}/story-arcs/{other_id}/delete/preview").ok
            return
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        expect(opener).to_be_focused()
        opener.click()
        expect(dialog.get_by_role("button", name="Review library comics")).to_have_attribute(
            "aria-expanded", "false"
        )
        assert path.read_bytes() == original
    finally:
        _run_async_blocking(cleanup(other_id))


@pytest.mark.parametrize("theme", ["light", "dark", "tron"])
def test_related_series_review_is_read_only(
    authed_page: Page, seeded_server: str, arc_reader: int, theme: str
) -> None:
    page = authed_page
    page.add_init_script(f"localStorage.setItem('pullbox-theme', '{theme}')")
    if theme == "dark":
        page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{seeded_server}/story-arcs/{arc_reader}")
    opener = page.get_by_role("button", name="Delete Story Arc", exact=True)
    opener.click()
    dialog = page.get_by_role("dialog", name="Delete Reader crossover?")
    review = dialog.get_by_role("region", name="Related series impact")
    baseline = page.request.get(f"{seeded_server}/story-arcs/{arc_reader}/delete/preview").json()
    page.route("**/related-series?*", lambda route: route.fulfill(status=503), times=1)
    review.get_by_role("button", name="Review related series").click()
    expect(review.get_by_role("alert")).to_have_text("Could not load related series.")
    review.get_by_role("button", name="Retry").click()
    row = review.get_by_role("listitem").filter(has=page.get_by_text("Batman Beyond", exact=True))
    row.get_by_role("button", name="Review outside issues").click()
    expect(
        row.get_by_role("list", name="Outside issues").get_by_role("listitem").first
    ).to_be_visible()
    choice = row.get_by_role("checkbox", name="Remove series record: Batman Beyond")
    expect(choice).not_to_be_checked()
    expect(dialog.get_by_role("button", name="Delete arc only")).to_be_enabled()
    choice.check()
    dialog.get_by_role("button", name="Review selected series").click()
    expect(dialog.get_by_text("All comic files will stay on disk.")).to_be_visible()
    expect(dialog.get_by_role("button", name="Delete arc and selected series")).to_be_visible()
    choice.uncheck()
    expect(dialog.get_by_role("button", name="Delete arc only")).to_be_enabled()
    assert_no_axe_violations(page, name=f"arc-series-review-{theme}", include=["dialog[open]"])
    dialog.press("Escape")
    expect(opener).to_be_focused()
    after = page.request.get(f"{seeded_server}/story-arcs/{arc_reader}/delete/preview").json()
    assert {k: v for k, v in after.items() if k != "token"} == {
        k: v for k, v in baseline.items() if k != "token"
    }


def test_deleting_a_parent_series_preserves_a_reviewable_chapter(
    authed_page: Page, seeded_server: str, arc_reader: int, tmp_path: Path
) -> None:
    async def seed() -> tuple[int, int]:
        async with get_session_factory().begin() as db:
            series = Series(
                title="Disposable crossover",
                sort_title="disposable crossover",
                path=str(tmp_path / "Disposable crossover"),
            )
            issue = Issue(
                series=series, issue_number=50, issue_number_text="50-X", title="Final chapter"
            )
            entry = IssueStoryArc(
                story_arc_id=arc_reader,
                issue=issue,
                sequence_number=4,
                source_ordinal=4,
                resolution_state=StoryArcResolutionState.RESOLVED,
            )
            db.add(entry)
            await db.flush()
            return series.id, entry.id

    async def cleanup(sid: int) -> None:
        async with get_session_factory().begin() as db:
            await db.execute(delete(Series).where(Series.id == sid))

    sid, mid = _run_async_blocking(seed())
    try:
        page = authed_page
        detail = SeriesDetailPage(page, seeded_server)
        detail.goto(sid)
        detail.open_delete_modal()
        detail.delete_modal.get_by_test_id("series-delete-submit").click()
        expect(page).to_have_url(re.compile(r"/series(?:\?.*)?$"))
        page.goto(f"{seeded_server}/story-arcs/{arc_reader}")
        row = page.locator(f"tbody[data-membership-id='{mid}']")
        expect(row).to_contain_text("Disposable crossover")
        expect(row).to_contain_text("#50-X")
        expect(row).to_contain_text("Final chapter")
        expect(row.get_by_test_id(f"story-arc-membership-status-{mid}")).to_contain_text("Missing")
    finally:
        _run_async_blocking(cleanup(sid))


def test_arc_delete_confirms_explicit_series_without_removing_comics(
    authed_page: Page, seeded_server: str, arc_reader: int, tmp_path: Path
) -> None:
    import asyncio

    async def setup() -> int:
        async with get_session_factory().begin() as db:
            parent = Series(title="Disposable records", sort_title="disposable records")
            db.add(
                IssueStoryArc(
                    story_arc_id=arc_reader,
                    issue=Issue(series=parent, issue_number=1),
                    sequence_number=4,
                    source_ordinal=4,
                    resolution_state=StoryArcResolutionState.RESOLVED,
                )
            )
            db.add(Issue(series=parent, issue_number=2))
            await db.flush()
            return parent.id

    async def await_removal(sid: int) -> None:
        for _ in range(100):
            async with get_session_factory()() as db:
                if await db.get(StoryArc, arc_reader) is None:
                    assert await db.get(Series, sid) is None
                    assert await db.get(Series, 2)
                    return
            await asyncio.sleep(0.05)
        pytest.fail("The confirmed records were not removed")

    async def cleanup(sid: int) -> None:
        async with get_session_factory().begin() as db:
            await db.execute(delete(Series).where(Series.id == sid))

    sid = _run_async_blocking(setup())
    canonical = tmp_path / "Batman Beyond 091.cbz"
    original = canonical.read_bytes()
    try:
        page = authed_page
        page.goto(f"{seeded_server}/story-arcs/{arc_reader}")
        page.get_by_role("button", name="Delete Story Arc", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("button", name="Review related series").click()
        dialog.get_by_role("checkbox", name="Remove series record: Disposable records").check()
        dialog.get_by_role("button", name="Review selected series").click()
        expect(dialog.get_by_role("status", name="Selected series records")).to_contain_text(
            "2 issue records, 1 outside this arc"
        )
        dialog.get_by_role("button", name="Delete arc and selected series").click()
        expect(page).to_have_url(re.compile(r"/utilities\?tab=queue$"))
        _run_async_blocking(await_removal(sid))
        assert canonical.read_bytes() == original
    finally:
        _run_async_blocking(cleanup(sid))


def test_delete_arc_with_an_explicitly_selected_copy(
    authed_page: Page,
    seeded_server: str,
    arc_reader: int,
    tmp_path: Path,
) -> None:
    from pullbox.services.story_arc_placement_integration import (
        StoryArcPlacementPolicyInput,
        StoryArcPlacementSyncService,
    )

    async def setup_copy() -> Path:
        dest = tmp_path / "Arc copies"
        dest.mkdir()
        async with get_session_factory()() as db:
            svc = StoryArcPlacementSyncService()
            arc = await db.get(StoryArc, arc_reader)
            root = await db.scalar(select(LibraryRoot).where(LibraryRoot.path == str(tmp_path)))
            member = await db.scalar(
                select(IssueStoryArc.id).where(
                    IssueStoryArc.story_arc_id == arc_reader, IssueStoryArc.sequence_number == 2
                )
            )
            await svc.update_policy(
                db,
                arc_reader,
                expected_revision=arc.revision,
                proposal=StoryArcPlacementPolicyInput(
                    mode="copy", target_library_root_id=root.id, destination_root=str(dest)
                ),
            )
            result = await svc.sync_membership(db, arc_reader, member)
            return Path(result.placement.placement_path)

    copy = _run_async_blocking(setup_copy())
    canonical = tmp_path / "Batman Beyond 091.cbz"
    original = canonical.read_bytes()
    page = authed_page
    page.goto(f"{seeded_server}/story-arcs/{arc_reader}")
    opener = page.get_by_role("button", name="Delete Story Arc", exact=True)
    opener.click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("button", name="Review arc files").click()
    box = dialog.get_by_role("checkbox", name=f"Remove {copy}", exact=True)
    expect(box).not_to_be_checked()
    box.check()
    dialog.get_by_role("button", name="Review selected files").click()
    confirm = dialog.get_by_role("button", name="Delete arc and selected files")
    expect(confirm).to_be_visible()
    expect(dialog.get_by_role("status").filter(has_text="Selected arc files")).to_contain_text(
        str(copy)
    )
    assert copy.read_bytes() == original
    box.uncheck()
    expect(confirm).to_have_count(0)
    box.check()
    dialog.get_by_role("button", name="Review selected files").click()
    confirm.click()
    expect(page).to_have_url(re.compile(r"/utilities\?tab=queue$"))
    import time

    for _ in range(100):
        if not copy.exists():
            break
        time.sleep(0.05)
    assert not copy.exists() and canonical.read_bytes() == original


@pytest.mark.parametrize("change", ["arc", "membership"])
def test_lost_arc_context_keeps_page_and_explicitly_recovers_series_order(
    authed_page: Page,
    seeded_server: str,
    seeded_reader_state_guard: None,
    arc_reader: int,
    change: str,
) -> None:
    page = authed_page
    page.goto(f"{seeded_server}/story-arcs/{arc_reader}")
    with page.expect_response(
        lambda r: r.url.endswith("/1/progress") and r.request.method == "PUT"
    ):
        page.get_by_role("link", name="Read story arc", exact=True).click()
    stale_launch = page.url + "&read=1"
    issue = IssueDetailPage(page, seeded_server)
    expect(issue.reader_status).to_have_text("Page 2 of 3")
    page.get_by_test_id("comic-reader-fit-width").click()

    async def remove() -> None:
        async with get_session_factory().begin() as session:
            await session.execute(
                delete(IssueStoryArc).where(
                    IssueStoryArc.story_arc_id == arc_reader, IssueStoryArc.issue_id == 1
                )
            )

    if change == "arc":
        detail = page.context.new_page()
        detail.goto(f"{seeded_server}/story-arcs/{arc_reader}")
        opener = detail.get_by_role("button", name="Delete Story Arc", exact=True)
        opener.click()
        dialog = detail.get_by_role("dialog", name="Delete Reader crossover?")
        expect(dialog).to_contain_text("3 reading-order entries")
        expect(dialog.get_by_role("button", name="Cancel")).to_be_focused()
        dialog.press("Escape")
        expect(opener).to_be_focused()
        opener.click()
        dialog.get_by_role("button", name="Delete arc only", exact=True).click()
        expect(
            detail.get_by_role("status").filter(has_text="No comic files were removed")
        ).to_be_visible()
        detail.close()
        with page.expect_response(
            lambda r: r.url.endswith("/1/progress") and r.request.method == "PUT"
        ) as saved:
            page.get_by_test_id("comic-reader-previous").click()
        assert saved.value.json()["arc_bookmark_saved"] is False
    else:
        _run_async_blocking(remove())
        page.get_by_test_id("comic-reader-next-issue").click()
    expected = "Page 1 of 3" if change == "arc" else "Page 2 of 3"
    current = issue.reader_page.get_attribute("src")
    recovery = page.get_by_role("button", name="Continue in series order", exact=True)
    expect(recovery).to_be_visible()
    expect(page.get_by_test_id("comic-reader-next-issue")).to_be_disabled()
    if change == "arc":
        expect(
            issue.reader_dialog.get_by_role("link", name="Back to story arc")
        ).not_to_be_visible()
    expect(issue.reader_status).to_have_text(expected)
    expect(page).to_have_url(re.compile(r"source=story-arc"))
    recovery.press("Enter")
    expect(recovery).not_to_be_visible()
    expect(page.get_by_test_id("comic-reader-arc-position")).not_to_be_visible()
    expect(issue.reader_page).to_have_attribute("src", current)
    expect(issue.reader_page).to_have_attribute("data-fit-mode", "width")
    expect(issue.reader_status).to_have_text(expected)
    expect(page.get_by_test_id("comic-reader-viewport")).to_be_focused()
    issue.close_reader()
    expect(page.get_by_test_id("issue-detail-back-link")).to_have_text("Back to series")

    page.goto(stale_launch)
    error = page.get_by_test_id("comic-reader-error")
    expect(error).to_be_visible()
    error.get_by_role("link", name="Read in series order").click()
    expect(issue.reader_status).to_have_text(expected)
    expect(page.get_by_test_id("comic-reader-arc-position")).not_to_be_visible()


def test_reader_does_not_return_to_an_arc_deleted_after_the_last_save(
    authed_page: Page,
    seeded_server: str,
    seeded_reader_state_guard: None,
    arc_reader: int,
) -> None:
    page = authed_page
    page.goto(f"{seeded_server}/story-arcs/{arc_reader}")
    page.get_by_role("link", name="Read story arc", exact=True).click()
    with page.expect_response(
        lambda r: (
            r.request.method == "PUT"
            and r.url.endswith("/progress")
            and not r.url.endswith("/1/progress")
        )
    ) as saved:
        page.get_by_test_id("comic-reader-next-issue").click()
    assert saved.value.json()["arc_bookmark_saved"] is True
    chapter_id = int(saved.value.url.split("/")[-2])
    back = page.get_by_role("dialog").get_by_role("link", name="Back to story arc")
    expect(back).to_be_visible()

    async def remove() -> None:
        async with get_session_factory().begin() as session:
            await session.execute(delete(StoryArc).where(StoryArc.id == arc_reader))

    _run_async_blocking(remove())
    arc_detail = f"**/story-arcs/{arc_reader}*"
    page.route(arc_detail, lambda route: route.fulfill(status=503))
    back.click()
    expect(page.get_by_test_id("comic-reader-switch-error")).to_contain_text("Could not check")
    expect(page.get_by_role("button", name="Continue in series order")).not_to_be_visible()
    expect(back).to_be_visible()
    page.unroute(arc_detail)
    back.click()
    expect(page.get_by_role("button", name="Continue in series order")).to_be_visible()
    expect(back).not_to_be_visible()
    expect(page.get_by_test_id("comic-reader-page-status")).to_have_text("Page 1 of 3")
    page.get_by_test_id("comic-reader-close").click()
    expect(page).to_have_url(f"{seeded_server}/issues/{chapter_id}")
    expect(page.get_by_test_id("issue-detail-back-link")).to_have_text("Back to series")


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
