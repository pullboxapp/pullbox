"""Confirmed release issue state survives navigation, themes and real state updates."""
# ruff: noqa: F811 - reuse the owning discovery/browser fixture.

from datetime import UTC, date, datetime

import pytest
from playwright.sync_api import expect
from sqlalchemy import delete, select

from pullbox.core.metadata_identity import IdentityEvidenceKind, IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.database import get_session_factory
from pullbox.models import (
    DownloadHistory,
    DownloadState,
    Issue,
    IssueStatus,
    LibraryFile,
    LibraryRoot,
    Series,
)
from pullbox.models.download import DownloadClientType
from pullbox.models.library import FileFormat
from pullbox.models.metadata_identity import SeriesExternalIdentity
from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _run_async_blocking
from tests.e2e.test_whats_new_find_add import release_page  # noqa: F401

pytestmark = pytest.mark.e2e


@pytest.fixture
def issue_state_page(release_page, tmp_path):
    async def prepare():
        async with get_session_factory().begin() as session:
            root = await session.scalar(select(LibraryRoot).order_by(LibraryRoot.id))
            ids = []
            issue_ids = []
            for number, (title, locg_id) in enumerate(
                [
                    ("Atlas Deluxe", 960001),
                    ("Budget Hero", 960002),
                    ("Midnight Flight", 960003),
                    ("Omega Patrol", 960004),
                ]
            ):
                series = Series(title=title, sort_title=title)
                session.add(series)
                await session.flush()
                ids.append(series.id)
                session.add(
                    SeriesExternalIdentity(
                        series_id=series.id,
                        identity_namespace=IdentityNamespace.LOCG,
                        external_id=str(locg_id),
                        verification_state=IdentityVerificationState.VERIFIED,
                        evidence_kind=IdentityEvidenceKind.USER_SELECTION,
                    )
                )
                if number == 3:
                    continue
                issue = Issue(
                    series_id=series.id,
                    issue_number=1,
                    store_date=date(2026, 5, 20),
                    status=IssueStatus.OWNED if number == 1 else IssueStatus.WANTED,
                )
                session.add(issue)
                await session.flush()
                issue_ids.append(issue.id)
                if number == 1:
                    session.add(
                        LibraryFile(
                            issue_id=issue.id,
                            library_root_id=root.id,
                            file_path=str(tmp_path / "owned.cbz"),
                            file_name="owned.cbz",
                            file_size=10,
                            file_format=FileFormat.CBZ,
                            file_modified_at=datetime.now(UTC),
                        )
                    )
                elif number == 2:
                    session.add(
                        DownloadHistory(
                            issue_id=issue.id,
                            title="Midnight Flight #1",
                            download_url="https://example.test/release",
                            download_client=DownloadClientType.DIRECT,
                            state=DownloadState.DOWNLOADING,
                        )
                    )
            return ids, issue_ids

    series_ids, issue_ids = _run_async_blocking(prepare())
    yield release_page, issue_ids

    async def cleanup():
        async with get_session_factory().begin() as session:
            await session.execute(delete(LibraryFile).where(LibraryFile.issue_id.in_(issue_ids)))
            for series_id in series_ids:
                await session.delete(await session.get(Series, series_id))

    _run_async_blocking(cleanup())


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 390), ("monitor", 320)])
def test_real_release_state_links_reflow_and_accessibility(
    issue_state_page, seeded_server, theme, width, browser_name
):
    page, issue_ids = issue_state_page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    page.goto(f"{seeded_server}/whats-new")
    page.evaluate("theme => applyTheme(theme)", theme)
    rows = page.get_by_test_id("whats-new-release-row")
    for title, label in [
        ("Atlas Deluxe", "Missing"),
        ("Budget Hero", "Owned"),
        ("Midnight Flight", "Downloading"),
        ("Omega Patrol", "Issue not linked"),
    ]:
        row = rows.filter(has_text=title)
        expect(row.get_by_test_id("whats-new-issue-state")).to_have_text(label)
        expect(row.get_by_test_id("whats-new-local-series")).to_be_visible()
    unresolved = rows.filter(has_text="Omega Patrol")
    expect(unresolved.get_by_test_id("whats-new-local-issue")).to_have_count(0)
    linked = rows.filter(has_text="Atlas Deluxe")
    expect(linked.get_by_test_id("whats-new-local-issue")).to_have_attribute(
        "href", f"/issues/{issue_ids[0]}"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    assert_no_axe_violations(page, name=f"release-issue-state-{theme}-{width}")
    page.screenshot(
        path=f"output/playwright/release-issue-state-{browser_name}-{theme}-{width}.png",
        animations="disabled",
    )
    linked.get_by_test_id("whats-new-local-issue").focus()
    page.keyboard.press("Enter")
    expect(page).to_have_url(f"{seeded_server}/issues/{issue_ids[0]}")


def test_release_state_reads_actual_changes_on_reload(issue_state_page, seeded_server):
    page, issue_ids = issue_state_page
    page.goto(f"{seeded_server}/whats-new")
    row = page.get_by_test_id("whats-new-release-row").filter(has_text="Atlas Deluxe")
    expect(row.get_by_test_id("whats-new-issue-state")).to_have_text("Missing")
    response = page.request.put(
        f"{seeded_server}/api/v1/issues/{issue_ids[0]}",
        data={"status": "skipped"},
        headers={"X-CSRF-Token": page.evaluate("readCsrfTokenFromBody()")},
    )
    assert response.status == 200, response.text()
    page.reload()
    expect(row.get_by_test_id("whats-new-issue-state")).to_have_text("Skipped")
    expect(row.get_by_test_id("whats-new-local-issue")).to_have_attribute(
        "href", f"/issues/{issue_ids[0]}"
    )
