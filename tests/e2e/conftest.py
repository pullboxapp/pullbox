"""
E2E test fixtures — server lifecycle, database seeding, and Playwright helpers.

Provides session-scoped fixtures that start a live uvicorn server with
migrations applied and test data seeded, plus function-scoped browser
pages for isolated E2E testing.

Run:
    pytest tests/e2e/ -v --browser chromium
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import pytest
import uvicorn
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

if TYPE_CHECKING:
    from collections.abc import Generator

    from playwright.sync_api import Browser, Page


_TRACE_ENABLED = os.environ.get("PULLBOX_E2E_TRACE", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call):  # type: ignore[no-untyped-def]
    """Expose per-phase results to fixtures that capture failure artifacts."""
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


# ── Playwright session fixture override ──────────────────────────────


@pytest.fixture(scope="session")
def browser(launch_browser):  # type: ignore[no-untyped-def]
    """Override pytest-playwright's browser fixture for safe teardown.

    The default fixture's ``browser.close()`` can fail during session
    teardown when the live server thread has poisoned the event loop.
    We catch and suppress that error since the browser process is
    terminated by the OS at process exit anyway.
    """
    browser: Browser = launch_browser()
    yield browser
    with contextlib.suppress(Exception):
        browser.close()


@pytest.fixture(autouse=True)
def capture_playwright_artifacts(
    request: pytest.FixtureRequest, page: Page
) -> Generator[None, None, None]:
    """Capture trace + screenshot on E2E failure for easier visual debugging."""
    artifacts_dir = Path(__file__).resolve().parents[2] / "test-results" / "playwright"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    if _TRACE_ENABLED:
        with contextlib.suppress(Exception):
            page.context.tracing.start(screenshots=True, snapshots=True)

    yield

    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", request.node.nodeid)
    failed = getattr(request.node, "rep_call", None)

    if failed and failed.failed:
        with contextlib.suppress(Exception):
            page.screenshot(path=str(artifacts_dir / f"{safe_name}.png"), full_page=True)
        if _TRACE_ENABLED:
            with contextlib.suppress(Exception):
                page.context.tracing.stop(path=str(artifacts_dir / f"{safe_name}.zip"))
        return

    if _TRACE_ENABLED:
        with contextlib.suppress(Exception):
            page.context.tracing.stop()


# ── Helpers ────────────────────────────────────────────────────────────


def _find_free_port() -> int:
    """Find an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(base_url: str, *, timeout: float = 15.0) -> None:
    """Poll GET /ping until it returns 200, or raise after timeout."""
    import httpx

    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/ping", timeout=2.0)
            if resp.status_code == 200:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(0.3)
    msg = f"Server at {base_url} did not become ready within {timeout}s"
    if last_error:
        msg += f" (last error: {last_error!r})"
    raise TimeoutError(msg)


def wait_for_htmx(page: Page, *, timeout: int = 5000) -> None:
    """Wait for HTMX to finish all pending requests and settle.

    Evaluates in-browser: if htmx is loaded and has pending requests,
    waits for the htmx:afterSettle event. If htmx isn't loaded or has
    no pending work, returns immediately.
    """
    deadline = time.monotonic() + (timeout / 1000)
    attempts = 0
    while True:
        attempts += 1
        try:
            _evaluate_htmx_idle(page, timeout=timeout)
            return
        except PlaywrightError as exc:
            if "Execution context was destroyed" not in str(exc) or attempts >= 3:
                raise

            remaining_ms = max(250, int((deadline - time.monotonic()) * 1000))
            with contextlib.suppress(PlaywrightError, PlaywrightTimeoutError):
                page.wait_for_load_state("domcontentloaded", timeout=remaining_ms)


def _evaluate_htmx_idle(page: Page, *, timeout: int) -> None:
    """Evaluate the HTMX idle promise in the current browser context."""
    page.evaluate(
        """(timeout) => {
            return new Promise((resolve) => {
                if (typeof htmx === 'undefined') { resolve(); return; }
                // If no pending requests, resolve immediately
                const pending = document.querySelectorAll('.htmx-request');
                if (pending.length === 0) { resolve(); return; }
                const timer = setTimeout(resolve, timeout);
                document.addEventListener('htmx:afterSettle', function handler() {
                    clearTimeout(timer);
                    document.removeEventListener('htmx:afterSettle', handler);
                    resolve();
                }, { once: true });
            });
        }""",
        timeout,
    )


def run_htmx_ajax_and_wait(
    page: Page,
    url: str,
    target_selector: str,
    *,
    swap: str = "outerHTML",
    timeout: int = 5000,
) -> None:
    """Issue an HTMX ajax request and wait for the target request to finish.

    This is more precise than ``wait_for_htmx`` for one-off refresh checks,
    because it watches the specific target's HTMX lifecycle instead of any
    pending HTMX activity on the page. It resolves on request completion even
    when the swap is suppressed because the returned HTML is unchanged.
    """
    page.evaluate(
        """async ({ url, targetSelector, swap, timeout }) => {
            if (typeof htmx === "undefined") {
                throw new Error("htmx is not available on the page");
            }

            const target = document.querySelector(targetSelector);
            if (!target) {
                throw new Error(`HTMX target not found: ${targetSelector}`);
            }

            function matchesTarget(node) {
                if (!node || !node.matches) {
                    return false;
                }
                return node.matches(targetSelector) || Boolean(node.closest(targetSelector));
            }

            await new Promise((resolve, reject) => {
                let settled = false;
                const timer = window.setTimeout(() => {
                    cleanup();
                    reject(new Error(`Timed out waiting for HTMX settle on ${targetSelector}`));
                }, timeout);

                function cleanup() {
                    window.clearTimeout(timer);
                    document.removeEventListener("htmx:afterRequest", onAfterRequest);
                    document.removeEventListener("htmx:afterSettle", onAfterSettle);
                    document.removeEventListener("htmx:responseError", onResponseError);
                }

                function finish(callback, value) {
                    if (settled) {
                        return;
                    }
                    settled = true;
                    cleanup();
                    callback(value);
                }

                function onAfterRequest(event) {
                    const detail = event.detail || {};
                    const elt = detail.elt || event.target;
                    const target = detail.target || null;
                    if (matchesTarget(target) || matchesTarget(elt)) {
                        finish(resolve);
                    }
                }

                function onAfterSettle(event) {
                    const elt = (event.detail && event.detail.elt) || event.target;
                    if (matchesTarget(elt)) {
                        finish(resolve);
                    }
                }

                function onResponseError(event) {
                    const elt = (event.detail && event.detail.elt) || event.target;
                    if (matchesTarget(elt)) {
                        finish(reject, new Error(`HTMX response error for ${url}`));
                    }
                }

                document.addEventListener("htmx:afterRequest", onAfterRequest);
                document.addEventListener("htmx:afterSettle", onAfterSettle);
                document.addEventListener("htmx:responseError", onResponseError);

                htmx.ajax("GET", url, {
                    target: targetSelector,
                    swap,
                });
            });
        }""",
        {
            "url": url,
            "targetSelector": target_selector,
            "swap": swap,
            "timeout": timeout,
        },
    )


def assert_toast(page: Page, message: str, *, timeout: int = 3000) -> None:
    """Assert that a toast notification with the given message appears."""
    toast = page.locator(f"text={message}").first
    toast.wait_for(state="visible", timeout=timeout)


def _run_async_blocking(coro):  # type: ignore[no-untyped-def]
    """Run a coroutine from sync test code, even if a loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, object] = {}
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - bubbles to caller
            error["exc"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()

    if "exc" in error:
        raise error["exc"]
    return result.get("value")


_TEST_COVER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAMCAIAAADQ/GvKAAAAEklEQVR42mMwqfiGFTGMSqAjAJZBnMFc9NzZAAAAAElFTkSuQmCC"
)


async def _seed_series_library_data() -> None:
    """Seed a small /series dataset so E2E tests exercise real list states."""
    from datetime import UTC, date, datetime

    from sqlalchemy import select

    from pullbox.database import get_session_factory
    from pullbox.models.blocklist import BlocklistEntry, BlocklistReason, normalize_release_title
    from pullbox.models.config import SystemConfig
    from pullbox.models.creator import Creator, IssueCreator
    from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
    from pullbox.models.import_job import (
        ImportedFile,
        ImportedFileStatus,
        ImportedSeries,
        ImportJob,
        ImportJobLog,
        ImportJobStatus,
        ImportSeriesStatus,
        ImportSourceType,
    )
    from pullbox.models.issue import Issue, IssueStatus
    from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
    from pullbox.models.matching_suggestion import MatchingSuggestion, SuggestionStatus
    from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
    from pullbox.models.publisher import Publisher
    from pullbox.models.reader import IssueReaderState
    from pullbox.models.search_log import SearchLog, SearchType
    from pullbox.models.series import Series, SeriesStatus
    from pullbox.models.user import User

    factory = get_session_factory()
    async with factory() as session:
        existing_series = await session.scalar(select(Series.id).limit(1))
        if existing_series is not None:
            return

        reader_user = await session.scalar(select(User).where(User.username == "admin"))
        if reader_user is None:
            raise RuntimeError("E2E reader state requires the seeded admin user.")

        test_cover_url = (
            "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 120 180'%3E"
            "%3Crect width='120' height='180' fill='%23273343'/%3E"
            "%3Ctext x='50%25' y='50%25' dominant-baseline='middle' text-anchor='middle' "
            "fill='%23f4f4f5' font-family='sans-serif' font-size='16'%3EBatman%3C/text%3E%3C/svg%3E"
        )

        primary_publisher = Publisher(name="DC Comics")
        secondary_publisher = Publisher(name="Image Comics")
        library_root = LibraryRoot(
            name="E2E Library",
            path="/tmp/pullbox-e2e-library",
            enabled=True,
        )
        session.add_all([primary_publisher, secondary_publisher, library_root])
        await session.flush()

        comics_directory = await session.get(SystemConfig, "comics_directory")
        e2e_library_path = str(Path("/tmp/pullbox-e2e-library").resolve())
        if comics_directory is None:
            session.add(
                SystemConfig(
                    key="comics_directory",
                    value=e2e_library_path,
                    value_type="string",
                )
            )
        else:
            comics_directory.value = e2e_library_path

        seeded_series = [
            ("Batman", 2016, SeriesStatus.CONTINUING, True, primary_publisher),
            ("Batman Beyond", 2015, SeriesStatus.ENDED, False, primary_publisher),
            ("Saga", 2012, SeriesStatus.CONTINUING, True, secondary_publisher),
            ("Superman", 2018, SeriesStatus.CONTINUING, True, primary_publisher),
            ("Wonder Woman", 2019, SeriesStatus.ENDED, False, primary_publisher),
            ("Planetary", 1999, SeriesStatus.ENDED, False, secondary_publisher),
        ]

        for idx, (title, year, status, monitored, publisher) in enumerate(seeded_series, start=1):
            series_path = Path(
                f"/tmp/pullbox-e2e-library/{idx:02d}-{title.lower().replace(' ', '-')}"
            )
            series_path.mkdir(parents=True, exist_ok=True)
            series = Series(
                title=title,
                sort_title=title,
                year_start=year,
                status=status,
                monitored=monitored,
                cover_url=test_cover_url if title == "Batman" else None,
                issue_count=3,
                comicvine_id=12345 if title == "Batman" else None,
                comicvine_url=(
                    "https://comicvine.gamespot.com/batman/4050-12345/"
                    if title == "Batman"
                    else None
                ),
                publisher_id=publisher.id,
                library_root_id=library_root.id,
                path=str(series_path),
            )
            session.add(series)
            await session.flush()
            if title == "Batman":
                (series_path / "cover.png").write_bytes(_TEST_COVER_PNG)
                (series_path / "issue_001.png").write_bytes(_TEST_COVER_PNG)
                with zipfile.ZipFile(
                    series_path / "Batman 001 (2016).cbz", "w", zipfile.ZIP_DEFLATED
                ) as comic:
                    comic.writestr("001.png", _TEST_COVER_PNG)
                    comic.writestr("002.png", _TEST_COVER_PNG)
                    comic.writestr("003.png", _TEST_COVER_PNG)
                (series_path / "library-context-test.cbr").write_bytes(b"rar-ish")
                series.cover_path = f"/api/v1/series/{series.id}/cover"

            owned_issue = Issue(
                series_id=series.id,
                issue_number=1.0,
                title=f"{title} #1",
                release_date=date(2024, 1, idx),
                status=IssueStatus.OWNED,
            )
            wanted_issue = Issue(
                series_id=series.id,
                issue_number=2.0,
                title=f"{title} #2",
                release_date=date(2024, 2, idx),
                status=IssueStatus.OWNED if title == "Planetary" else IssueStatus.WANTED,
            )
            skipped_issue = Issue(
                series_id=series.id,
                issue_number=3.0,
                title=f"{title} #3",
                release_date=date(2024, 3, idx),
                status=IssueStatus.OWNED if title == "Planetary" else IssueStatus.SKIPPED,
            )
            if title == "Batman":
                owned_issue.description = "<p>Batman faces a new threat over Gotham.</p>"
                owned_issue.store_date = date(2024, 1, idx)
                owned_issue.page_count = 32
                owned_issue.comicvine_id = 50001
                owned_issue.comicvine_url = "https://comicvine.gamespot.com/batman-1/4000-50001/"
                owned_issue.cover_path = f"/api/v1/issues/{owned_issue.id}/cover"
            session.add_all([owned_issue, wanted_issue, skipped_issue])
            await session.flush()

            if title == "Batman":
                owned_issue.cover_path = f"/api/v1/issues/{owned_issue.id}/cover"
                writer = Creator(name="Tom King")
                artist = Creator(name="David Finch")
                session.add_all([writer, artist])
                await session.flush()
                session.add_all(
                    [
                        IssueCreator(issue_id=owned_issue.id, creator_id=writer.id, role="Writer"),
                        IssueCreator(issue_id=owned_issue.id, creator_id=artist.id, role="Artist"),
                    ]
                )
                session.add(
                    LibraryFile(
                        issue_id=owned_issue.id,
                        library_root_id=library_root.id,
                        file_path=f"{series_path}/Batman 001 (2016).cbz",
                        file_name="Batman 001 (2016).cbz",
                        file_size=52_428_800,
                        file_format=FileFormat.CBZ,
                        file_modified_at=datetime(2024, 1, 1, tzinfo=UTC),
                        match_confidence=MatchConfidence.MANUAL,
                    )
                )
                session.add(
                    IssueReaderState(
                        user_id=reader_user.id,
                        issue_id=owned_issue.id,
                        last_page_index=1,
                        content_revision="e2e-reading-revision",
                        page_count=3,
                        progress_updated_at=datetime.now(tz=UTC),
                        last_opened_at=datetime.now(tz=UTC),
                    )
                )
                session.add(
                    LibraryFile(
                        library_root_id=library_root.id,
                        file_path=f"{series_path}/library-context-test.cbr",
                        file_name="library-context-test.cbr",
                        file_size=7,
                        file_format=FileFormat.CBR,
                        file_modified_at=datetime(2024, 1, 2, tzinfo=UTC),
                        match_confidence=MatchConfidence.MANUAL,
                    )
                )

                session.add_all(
                    [
                        DownloadHistory(
                            issue_id=wanted_issue.id,
                            title="Batman 002 (2016) [Digital].cbz",
                            download_url="https://example.com/downloads/batman-002",
                            download_client=DownloadClientType.SABNZBD,
                            external_id="e2e-active-download",
                            state=DownloadState.DOWNLOADING,
                            file_size=104_857_600,
                        ),
                        DownloadHistory(
                            issue_id=owned_issue.id,
                            title="Batman 001 (2016) [Digital].cbz",
                            download_url="https://example.com/downloads/batman-001",
                            download_client=DownloadClientType.SABNZBD,
                            external_id="e2e-complete-download",
                            state=DownloadState.COMPLETED,
                            file_size=52_428_800,
                            downloaded_path=f"{series_path}/Batman 001 (2016).cbz",
                            final_path=f"{series_path}/Batman 001 (2016).cbz",
                            completed_at=datetime.now(tz=UTC),
                            imported_at=datetime.now(tz=UTC),
                        ),
                        DownloadHistory(
                            issue_id=owned_issue.id,
                            title="Batman 001 (2016) [Download Only].cbz",
                            download_url="https://example.com/downloads/batman-001-download",
                            download_client=DownloadClientType.SABNZBD,
                            external_id="e2e-complete-download-history",
                            state=DownloadState.COMPLETED,
                            file_size=52_428_800,
                            completed_at=datetime.now(tz=UTC),
                        ),
                        DownloadHistory(
                            issue_id=wanted_issue.id,
                            title="Batman 003 (2016) [Failed].cbz",
                            download_url="https://example.com/downloads/batman-003-failed",
                            download_client=DownloadClientType.SABNZBD,
                            external_id="e2e-failed-download-history",
                            state=DownloadState.FAILED,
                            file_size=73_400_320,
                            error_message="Connection refused while sending to client.",
                            completed_at=datetime.now(tz=UTC),
                        ),
                        PendingMatch(
                            issue_id=wanted_issue.id,
                            release_title="Batman 002 (2016) [Digital] (Alt Source).cbz",
                            download_url="https://indexer.example.com/dl/batman-002-alt",
                            is_torrent=False,
                            file_size=98_765_432,
                            confidence="medium",
                            match_details={
                                "parsed_series": "Batman",
                                "parsed_issue": 2.0,
                                "parsed_year": 2016,
                                "series_similarity": 0.94,
                                "issue_match": True,
                                "year_match": True,
                                "type_match": True,
                                "indexer_name": "NZBGeek",
                            },
                            status=PendingMatchStatus.PENDING,
                        ),
                        PendingMatch(
                            issue_id=owned_issue.id,
                            release_title="Batman 001 (2016) [Digital] Approved.cbz",
                            download_url="https://indexer.example.com/dl/batman-001-approved",
                            is_torrent=False,
                            file_size=52_428_800,
                            confidence="high",
                            match_details={
                                "parsed_series": "Batman",
                                "parsed_issue": 1.0,
                                "parsed_year": 2016,
                                "series_similarity": 1.0,
                                "issue_match": True,
                                "year_match": True,
                                "type_match": True,
                                "indexer_name": "NZBGeek",
                            },
                            status=PendingMatchStatus.APPROVED,
                            resolved_at=datetime.now(tz=UTC),
                            resolved_by="e2e",
                        ),
                    ]
                )

                session.add_all(
                    [
                        SearchLog(
                            issue_id=wanted_issue.id,
                            series_title=title,
                            issue_number=2.0,
                            search_type=SearchType.MANUAL,
                            results_found=12,
                            results_grabbed=1,
                            results_queued=2,
                            results_rejected=4,
                            best_confidence="high",
                            details={
                                "best_match": {
                                    "title": "Batman 002 (2016) [Digital].cbz",
                                    "indexer": "NZBGeek",
                                    "confidence": "high",
                                    "series_similarity": 0.97,
                                    "parsed_series": "Batman",
                                    "parsed_issue": 2.0,
                                    "parsed_year": 2016,
                                },
                                "search_passes": 2,
                                "search_time_ms": 184,
                                "rejected_count": 4,
                            },
                        ),
                        SearchLog(
                            issue_id=owned_issue.id,
                            series_title=title,
                            issue_number=1.0,
                            search_type=SearchType.AUTOMATED,
                            results_found=5,
                            results_grabbed=0,
                            results_queued=0,
                            results_rejected=1,
                            best_confidence="medium",
                            details={
                                "best_match": {
                                    "title": "Batman 001 (2016) [Digital].cbz",
                                    "indexer": "NZBFinder",
                                    "confidence": "medium",
                                    "series_similarity": 0.9,
                                }
                            },
                        ),
                        SearchLog(
                            issue_id=skipped_issue.id,
                            series_title=title,
                            issue_number=3.0,
                            search_type=SearchType.BULK,
                            results_found=0,
                            results_grabbed=0,
                            results_queued=0,
                            results_rejected=0,
                            best_confidence=None,
                            details={},
                        ),
                    ]
                )

                unmatched_file = LibraryFile(
                    library_root_id=library_root.id,
                    file_path=f"{series_path}/Batman Annual 001 (2016).cbz",
                    file_name="Batman Annual 001 (2016).cbz",
                    file_size=41_943_040,
                    file_format=FileFormat.CBZ,
                    file_modified_at=datetime(2024, 1, 3, tzinfo=UTC),
                    match_confidence=MatchConfidence.UNMATCHED,
                    parsed_series="Batman",
                    parsed_issue_number=1.0,
                    parsed_year=2016,
                    parsed_publisher="DC Comics",
                )
                session.add(unmatched_file)
                await session.flush()

                session.add(
                    MatchingSuggestion(
                        library_file_id=unmatched_file.id,
                        parent_series_id=series.id,
                        suggested_title="Batman Annual",
                        suggested_year=2016,
                        suggested_publisher="DC Comics",
                        suggested_series_type="annual",
                        detection_source="filename",
                        confidence_score=0.8,
                        reason="File appears to be an annual of Batman but no matching series exists.",
                        status=SuggestionStatus.PENDING,
                    )
                )

                session.add_all(
                    [
                        BlocklistEntry(
                            release_title="Batman 999 (2016) [Digital] Team-DCP",
                            release_title_normalized=normalize_release_title(
                                "Batman 999 (2016) [Digital] Team-DCP"
                            ),
                            series_id=series.id,
                            issue_id=wanted_issue.id,
                            reason=BlocklistReason.FAILED,
                            error_message="Post-processing failed after repeated retries.",
                            release_group="Team-DCP",
                        ),
                        BlocklistEntry(
                            release_title="Batman Annual 001 (2016) [Rejected] Empire",
                            release_title_normalized=normalize_release_title(
                                "Batman Annual 001 (2016) [Rejected] Empire"
                            ),
                            series_id=series.id,
                            issue_id=owned_issue.id,
                            reason=BlocklistReason.REJECTED,
                            release_group="Empire",
                        ),
                    ]
                )

            if title == "Saga":
                session.add(
                    BlocklistEntry(
                        release_title="Saga 073 (2024) [Manual] Minutemen",
                        release_title_normalized=normalize_release_title(
                            "Saga 073 (2024) [Manual] Minutemen"
                        ),
                        series_id=series.id,
                        issue_id=wanted_issue.id,
                        reason=BlocklistReason.MANUAL,
                        release_group="Minutemen",
                    )
                )

        misc_folder = Path("/tmp/pullbox-e2e-library/99-misc-folder")
        misc_folder.mkdir(parents=True, exist_ok=True)
        (misc_folder / "notes.txt").write_text("not tracked", encoding="utf-8")
        (misc_folder / "tracked-note.cbz").write_bytes(b"cbz")
        session.add(
            LibraryFile(
                library_root_id=library_root.id,
                file_path=f"{misc_folder}/tracked-note.cbz",
                file_name="tracked-note.cbz",
                file_size=3,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime(2024, 1, 4, tzinfo=UTC),
                match_confidence=MatchConfidence.MANUAL,
            )
        )

        for index in range(90):
            scroll_folder = Path(f"/tmp/pullbox-e2e-library/zz-scroll-{index:03d}")
            scroll_folder.mkdir(parents=True, exist_ok=True)
            scroll_file = scroll_folder / "tracked.cbz"
            scroll_file.write_bytes(b"cbz")
            session.add(
                LibraryFile(
                    library_root_id=library_root.id,
                    file_path=str(scroll_file),
                    file_name=scroll_file.name,
                    file_size=3,
                    file_format=FileFormat.CBZ,
                    file_modified_at=datetime(2024, 1, 5, tzinfo=UTC),
                    match_confidence=MatchConfidence.MANUAL,
                )
            )

        completed_job = ImportJob(
            source_path="/tmp/imports/batman-batch",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.COMPLETED,
            scan_total_files=24,
            scan_total_dirs=5,
            series_found=3,
            series_matched=3,
            series_imported=2,
            series_failed=1,
            total_files_found=24,
            total_files_matched=20,
            total_files_imported=18,
            total_files_failed=2,
            scan_started_at=datetime(2024, 2, 1, 9, 0, tzinfo=UTC),
            scan_completed_at=datetime(2024, 2, 1, 9, 3, tzinfo=UTC),
            match_started_at=datetime(2024, 2, 1, 9, 3, tzinfo=UTC),
            match_completed_at=datetime(2024, 2, 1, 9, 5, tzinfo=UTC),
            import_started_at=datetime(2024, 2, 1, 9, 5, tzinfo=UTC),
            import_completed_at=datetime(2024, 2, 1, 9, 12, tzinfo=UTC),
        )
        review_job = ImportJob(
            source_path="/tmp/imports/review-queue",
            source_type=ImportSourceType.MYLAR3,
            status=ImportJobStatus.REVIEW,
            scan_total_files=17,
            scan_total_dirs=4,
            series_found=2,
            series_matched=1,
            series_no_match=1,
            total_files_found=17,
            total_files_matched=11,
            total_files_no_match=6,
            scan_started_at=datetime(2024, 2, 2, 10, 0, tzinfo=UTC),
            scan_completed_at=datetime(2024, 2, 2, 10, 4, tzinfo=UTC),
            match_started_at=datetime(2024, 2, 2, 10, 5, tzinfo=UTC),
        )
        matching_job = ImportJob(
            source_path="/tmp/imports/active-matching",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.MATCHING,
            scan_total_files=42,
            scan_total_dirs=7,
            series_found=5,
            series_duplicate=1,
            series_matched=2,
            series_no_match=1,
            total_files_found=42,
            scan_started_at=datetime(2024, 2, 2, 12, 0, tzinfo=UTC),
            scan_completed_at=datetime(2024, 2, 2, 12, 2, tzinfo=UTC),
            match_started_at=datetime(2024, 2, 2, 12, 3, tzinfo=UTC),
        )
        failed_job = ImportJob(
            source_path="/tmp/imports/failed-run",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FAILED,
            scan_total_files=8,
            scan_total_dirs=2,
            series_found=1,
            series_failed=1,
            total_files_found=8,
            total_files_failed=8,
            scan_started_at=datetime(2024, 2, 3, 11, 0, tzinfo=UTC),
            scan_completed_at=datetime(2024, 2, 3, 11, 1, tzinfo=UTC),
            import_started_at=datetime(2024, 2, 3, 11, 2, tzinfo=UTC),
            import_completed_at=datetime(2024, 2, 3, 11, 3, tzinfo=UTC),
            error_message="Archive extraction failed during import.",
        )
        session.add_all([completed_job, review_job, matching_job, failed_job])
        await session.flush()

        session.add_all(
            [
                ImportJobLog(
                    import_job_id=completed_job.id,
                    level="INFO",
                    event="scan_started",
                    logged_at=datetime(2024, 2, 1, 9, 0, tzinfo=UTC),
                    data={"source": completed_job.source_path},
                ),
                ImportJobLog(
                    import_job_id=completed_job.id,
                    level="INFO",
                    event="series_imported",
                    logged_at=datetime(2024, 2, 1, 9, 10, tzinfo=UTC),
                    data={"series": "Batman", "count": 2},
                ),
                ImportJobLog(
                    import_job_id=completed_job.id,
                    level="WARNING",
                    event="series_failed",
                    logged_at=datetime(2024, 2, 1, 9, 11, tzinfo=UTC),
                    data={"series": "Batman Beyond", "reason": "metadata timeout"},
                ),
                ImportJobLog(
                    import_job_id=review_job.id,
                    level="INFO",
                    event="review_required",
                    logged_at=datetime(2024, 2, 2, 10, 6, tzinfo=UTC),
                    data={"unmatched_series": 1},
                ),
                ImportJobLog(
                    import_job_id=matching_job.id,
                    level="INFO",
                    event="scan_completed",
                    message="Scanned 42 files across 7 folders.",
                    logged_at=datetime(2024, 2, 2, 12, 2, tzinfo=UTC),
                    data={"files": 42, "dirs": 7},
                ),
                ImportJobLog(
                    import_job_id=matching_job.id,
                    level="INFO",
                    event="matching_progress",
                    message="Matching 4 of 5 series against ComicVine.",
                    logged_at=datetime(2024, 2, 2, 12, 4, tzinfo=UTC),
                    data={"raw_series_name": "Saga", "matched": 2, "remaining": 2},
                ),
                ImportJobLog(
                    import_job_id=failed_job.id,
                    level="ERROR",
                    event="import_failed",
                    logged_at=datetime(2024, 2, 3, 11, 3, tzinfo=UTC),
                    data={"message": failed_job.error_message},
                ),
            ]
        )

        session.add_all(
            [
                ImportedSeries(
                    import_job_id=completed_job.id,
                    raw_series_name="Batman and Robin Eternal",
                    raw_year=2015,
                    raw_publisher="DC Comics",
                    file_count=6,
                    sample_paths=["/tmp/imports/batman-batch/Batman and Robin Eternal 001.cbz"],
                    source_folder="/tmp/imports/batman-batch/batman-and-robin-eternal",
                    status=ImportSeriesStatus.NO_MATCH,
                ),
                ImportedSeries(
                    import_job_id=completed_job.id,
                    raw_series_name="Batman Universe",
                    raw_year=2019,
                    raw_publisher="DC Comics",
                    file_count=3,
                    sample_paths=["/tmp/imports/batman-batch/Batman Universe 001.cbz"],
                    source_folder="/tmp/imports/batman-batch/batman-universe",
                    status=ImportSeriesStatus.SKIPPED,
                ),
            ]
        )

        batman_series_id = await session.scalar(select(Series.id).where(Series.title == "Batman"))
        batman_issue_1_id = await session.scalar(
            select(Issue.id).where(Issue.series_id == batman_series_id, Issue.issue_number == 1.0)
        )
        saga_series_id = await session.scalar(select(Series.id).where(Series.title == "Saga"))
        superman_series_id = await session.scalar(
            select(Series.id).where(Series.title == "Superman")
        )

        completed_imported_batman = ImportedSeries(
            import_job_id=completed_job.id,
            raw_series_name="Batman",
            raw_year=2016,
            raw_publisher="DC Comics",
            file_count=8,
            sample_paths=["/tmp/imports/batman-batch/Batman 001 (2016).cbz"],
            source_folder="/tmp/imports/batman-batch/batman",
            status=ImportSeriesStatus.IMPORTED,
            series_id=batman_series_id,
            files_total=8,
            files_matched=8,
            files_imported=8,
        )
        completed_imported_superman = ImportedSeries(
            import_job_id=completed_job.id,
            raw_series_name="Superman",
            raw_year=2018,
            raw_publisher="DC Comics",
            file_count=8,
            sample_paths=["/tmp/imports/batman-batch/Superman 001 (2018).cbz"],
            source_folder="/tmp/imports/batman-batch/superman",
            status=ImportSeriesStatus.IMPORTED,
            series_id=superman_series_id,
            files_total=8,
            files_matched=8,
            files_imported=8,
        )
        completed_failed_series = ImportedSeries(
            import_job_id=completed_job.id,
            raw_series_name="Batman Beyond",
            raw_year=2015,
            raw_publisher="DC Comics",
            file_count=2,
            sample_paths=["/tmp/imports/batman-batch/Batman Beyond 001.cbz"],
            source_folder="/tmp/imports/batman-batch/batman-beyond",
            status=ImportSeriesStatus.FAILED,
            files_total=2,
            files_failed=2,
            error_message="Metadata timeout while importing the series.",
        )
        session.add_all(
            [
                completed_imported_batman,
                completed_imported_superman,
                completed_failed_series,
            ]
        )
        await session.flush()

        session.add_all(
            [
                ImportedFile(
                    import_job_id=completed_job.id,
                    import_series_id=completed_failed_series.id,
                    file_path="/tmp/imports/batman-batch/Batman Beyond 001.cbz",
                    file_name="Batman Beyond 001.cbz",
                    file_size=16_384_000,
                    file_format="cbz",
                    parsed_series="Batman Beyond",
                    parsed_issue_number=1.0,
                    parsed_year=2015,
                    status=ImportedFileStatus.FAILED,
                    error_message="Timed out while fetching issue metadata.",
                ),
                ImportedFile(
                    import_job_id=completed_job.id,
                    import_series_id=completed_failed_series.id,
                    file_path="/tmp/imports/batman-batch/Batman Beyond 002.cbz",
                    file_name="Batman Beyond 002.cbz",
                    file_size=16_512_000,
                    file_format="cbz",
                    parsed_series="Batman Beyond",
                    parsed_issue_number=2.0,
                    parsed_year=2015,
                    status=ImportedFileStatus.FAILED,
                    error_message="Timed out while fetching issue metadata.",
                ),
            ]
        )

        review_matched_series = ImportedSeries(
            import_job_id=review_job.id,
            raw_series_name="Batman",
            raw_year=2016,
            raw_publisher="DC Comics",
            file_count=1,
            sample_paths=["/tmp/imports/review-queue/Batman 001 (2016).cbz"],
            source_folder="/tmp/imports/review-queue/batman",
            status=ImportSeriesStatus.MATCHED,
            series_id=batman_series_id,
            files_total=1,
            files_matched=1,
        )
        review_unmatched_series = ImportedSeries(
            import_job_id=review_job.id,
            raw_series_name="Saga",
            raw_year=2012,
            raw_publisher="Image",
            file_count=1,
            sample_paths=["/tmp/imports/review-queue/Saga 001 (2012).cbz"],
            source_folder="/tmp/imports/review-queue/saga",
            status=ImportSeriesStatus.NO_MATCH,
            series_id=saga_series_id,
            files_total=1,
            files_no_match=1,
        )
        session.add_all([review_matched_series, review_unmatched_series])
        await session.flush()

        session.add_all(
            [
                ImportedFile(
                    import_job_id=review_job.id,
                    import_series_id=review_matched_series.id,
                    file_path="/tmp/imports/review-queue/Batman 001 (2016).cbz",
                    file_name="Batman 001 (2016).cbz",
                    file_size=24_576_000,
                    file_format="cbz",
                    parsed_series="Batman",
                    parsed_issue_number=1.0,
                    parsed_year=2016,
                    status=ImportedFileStatus.MATCHED,
                    matched_issue_id=batman_issue_1_id,
                    match_confidence="high",
                    match_method="filename",
                ),
                ImportedFile(
                    import_job_id=review_job.id,
                    import_series_id=review_unmatched_series.id,
                    file_path="/tmp/imports/review-queue/Saga 001 (2012).cbz",
                    file_name="Saga 001 (2012).cbz",
                    file_size=18_432_000,
                    file_format="cbz",
                    parsed_series="Saga",
                    parsed_issue_number=1.0,
                    parsed_year=2012,
                    status=ImportedFileStatus.NO_MATCH,
                ),
            ]
        )

        await session.commit()


async def _seed_usage_stats_choice() -> None:
    """Seed a resolved usage-stats choice so shared authenticated E2E flows stay quiet."""
    from pullbox.database import get_session_factory
    from pullbox.models.config import SystemConfig

    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(SystemConfig, "usage_stats_consent")
        if row is None:
            session.add(
                SystemConfig(
                    key="usage_stats_consent",
                    value="disabled",
                    value_type="string",
                )
            )
        else:
            row.value = "disabled"
        await session.commit()


async def _reset_seeded_reader_state() -> None:
    """Restore the mutable Batman reader row shared by seeded E2E tests."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from pullbox.database import get_session_factory
    from pullbox.models.issue import Issue
    from pullbox.models.reader import IssueReaderState
    from pullbox.models.series import Series
    from pullbox.models.user import User

    factory = get_session_factory()
    async with factory() as session:
        state = await session.scalar(
            select(IssueReaderState)
            .join(User, User.id == IssueReaderState.user_id)
            .join(Issue, Issue.id == IssueReaderState.issue_id)
            .join(Series, Series.id == Issue.series_id)
            .where(
                User.username == "admin",
                Series.title == "Batman",
                Issue.issue_number == 1.0,
            )
        )
        if state is None:
            raise RuntimeError("Seeded Batman reader state is missing.")

        now = datetime.now(tz=UTC)
        state.last_page_index = 1
        state.content_revision = "e2e-reading-revision"
        state.page_count = 3
        state.progress_updated_at = now
        state.last_opened_at = now
        state.completed_at = None
        state.completion_updated_at = None
        state.want_to_read = False
        state.want_to_read_updated_at = None
        state.state_version = 1
        await session.commit()


# ── Server Fixtures ───────────────────────────────────────────────────


@contextlib.contextmanager
def _running_live_server() -> Generator[str, None, None]:
    """Start an isolated live uvicorn server with a fresh migrated database."""
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    # Create a temp directory for the test database
    tmp_dir = tempfile.mkdtemp(prefix="pullbox-e2e-")
    db_path = Path(tmp_dir) / "e2e_test.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    data_dir = Path(tmp_dir) / "data"
    logs_dir = data_dir / "logs"
    covers_dir = data_dir / "covers"
    temp_dir = data_dir / "tmp"
    backup_dir = data_dir / "backups"
    library_root = data_dir / "library"
    for path in (data_dir, logs_dir, covers_dir, temp_dir, backup_dir, library_root):
        path.mkdir(parents=True, exist_ok=True)

    # Set env vars for the app and alembic
    env_overrides = {
        "PULLBOX_SECRET_KEY": "e2e-test-secret-key-for-testing",
        "PULLBOX_DB_URL": db_url,
        "PULLBOX_DEBUG": "true",
        "PULLBOX_LOG_LEVEL": "WARNING",
        "PULLBOX_DATA_DIR": str(data_dir),
        "PULLBOX_LOGS_DIR": str(logs_dir),
        "PULLBOX_COVERS_DIR": str(covers_dir),
        "PULLBOX_TEMP_DIR": str(temp_dir),
        "PULLBOX_BACKUP_DIR": str(backup_dir),
        "PULLBOX_LIBRARY_ROOT": str(library_root),
        # The session-scoped browser suite intentionally sends far more traffic
        # than one real client would within a minute. Keep rate limiting active
        # while preventing unrelated E2E modules from exhausting shared quotas.
        "PULLBOX_RATE_LIMIT_ENABLED": "true",
        "PULLBOX_RATE_LIMIT_TIER1": "10000",
        "PULLBOX_RATE_LIMIT_TIER2": "10000",
        "PULLBOX_RATE_LIMIT_TIER3": "10000",
    }
    original_env = {}
    for key, val in env_overrides.items():
        original_env[key] = os.environ.get(key)
        os.environ[key] = val

    # Run Alembic migrations to set up the schema
    alembic_ini = Path(__file__).resolve().parents[2] / "alembic" / "alembic.ini"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(alembic_ini), "upgrade", "head"],
        capture_output=True,
        text=True,
        env={**os.environ},
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    if result.returncode != 0:
        raise RuntimeError(f"Alembic migration failed:\n{result.stderr}\n{result.stdout}")

    # Clear any cached state from previous app imports
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.core import scheduler as scheduler_module
    from pullbox.database import dispose_engine

    with contextlib.suppress(Exception):
        _run_async_blocking(dispose_engine())
    with contextlib.suppress(Exception):
        if scheduler_module._scheduler_instance is not None:
            scheduler_module._scheduler_instance.shutdown(wait=False)
    scheduler_module._scheduler_instance = None
    reset_setup_cache()

    # Invalidate cached settings so new env vars take effect
    from pullbox.config import get_settings

    get_settings.cache_clear()

    # Start uvicorn in a background thread
    config = uvicorn.Config(
        "pullbox.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        loop="asyncio",  # Force stdlib loop — uvloop leaks into other threads
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        _wait_for_server(base_url, timeout=20.0)
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)

        # Dispose the async engine so no connections leak into later tests
        with contextlib.suppress(Exception):
            _run_async_blocking(dispose_engine())
        with contextlib.suppress(Exception):
            if scheduler_module._scheduler_instance is not None:
                scheduler_module._scheduler_instance.shutdown(wait=False)
        scheduler_module._scheduler_instance = None

        # Clear any stale event loop reference left by the server thread
        # so pytest-asyncio can create a fresh loop for subsequent tests
        try:
            loop = asyncio.get_event_loop_policy().get_event_loop()
            if loop.is_closed():
                asyncio.set_event_loop(asyncio.new_event_loop())
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

        # Restore env
        for key, val in original_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

        # Invalidate settings cache again
        get_settings.cache_clear()
        reset_setup_cache()

        # Cleanup temp database
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def live_server() -> Generator[str, None, None]:
    """Start a live uvicorn server with a fresh database and migrations."""
    with _running_live_server() as base_url:
        yield base_url


@pytest.fixture
def first_run_server() -> Generator[str, None, None]:
    """Start a pristine first-run server in a subprocess for isolation."""
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    tmp_dir = tempfile.mkdtemp(prefix="pullbox-e2e-first-run-")
    db_path = Path(tmp_dir) / "e2e_first_run.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    data_dir = Path(tmp_dir) / "data"
    logs_dir = data_dir / "logs"
    covers_dir = data_dir / "covers"
    temp_dir = data_dir / "tmp"
    backup_dir = data_dir / "backups"
    library_root = data_dir / "library"
    for path in (data_dir, logs_dir, covers_dir, temp_dir, backup_dir, library_root):
        path.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "PULLBOX_SECRET_KEY": "e2e-first-run-secret-key",
        "PULLBOX_DB_URL": db_url,
        "PULLBOX_DEBUG": "true",
        "PULLBOX_LOG_LEVEL": "WARNING",
        "PULLBOX_DATA_DIR": str(data_dir),
        "PULLBOX_LOGS_DIR": str(logs_dir),
        "PULLBOX_COVERS_DIR": str(covers_dir),
        "PULLBOX_TEMP_DIR": str(temp_dir),
        "PULLBOX_BACKUP_DIR": str(backup_dir),
        "PULLBOX_LIBRARY_ROOT": str(library_root),
    }

    alembic_ini = Path(__file__).resolve().parents[2] / "alembic" / "alembic.ini"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(alembic_ini), "upgrade", "head"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    if result.returncode != 0:
        raise RuntimeError(f"Alembic migration failed:\n{result.stderr}\n{result.stdout}")

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "pullbox.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        _wait_for_server(base_url, timeout=20.0)
        yield base_url
    finally:
        with contextlib.suppress(Exception):
            process.terminate()
        with contextlib.suppress(Exception):
            process.wait(timeout=5.0)
        if process.poll() is None:
            with contextlib.suppress(Exception):
                process.kill()

        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def seeded_server(live_server: str) -> Generator[str, None, None]:
    """Seed the live server with test data (admin user, series, issues).

    Creates an admin user via the setup API endpoint, then adds
    basic test data for E2E tests to operate on.
    """
    import httpx

    # Step 1: Create admin user via setup endpoint
    setup_resp = httpx.post(
        f"{live_server}/api/v1/system/setup",
        json={
            "username": "admin",
            "password": "TestPassword1!",
            "confirm_password": "TestPassword1!",
        },
        timeout=10.0,
    )
    if setup_resp.status_code not in (200, 201):
        # Maybe setup already done (idempotent)
        status = httpx.get(f"{live_server}/setup", timeout=5.0, follow_redirects=False)
        if status.status_code != 302 or status.headers.get("location") != "/login":
            raise RuntimeError(f"Setup failed ({setup_resp.status_code}): {setup_resp.text}")

    # Step 2: Login for authenticated requests used during data seeding
    login_resp = httpx.post(
        f"{live_server}/api/v1/auth/login",
        json={"username": "admin", "password": "TestPassword1!"},
        timeout=10.0,
    )
    if login_resp.status_code != 200:
        raise RuntimeError(f"Login failed ({login_resp.status_code}): {login_resp.text}")

    _run_async_blocking(_seed_series_library_data())
    _run_async_blocking(_seed_usage_stats_choice())

    yield live_server


@pytest.fixture
def seeded_reader_state_guard(seeded_server: str) -> Generator[None, None, None]:
    """Prevent reader mutations from leaking through the session-scoped E2E seed."""
    _run_async_blocking(_reset_seeded_reader_state())
    try:
        yield
    finally:
        _run_async_blocking(_reset_seeded_reader_state())


@pytest.fixture(scope="session")
def auth_session_cookie(seeded_server: str) -> dict[str, str]:
    """Return a reusable authenticated session cookie for browser tests."""
    import httpx

    login_resp = httpx.post(
        f"{seeded_server}/api/v1/auth/login",
        json={"username": "admin", "password": "TestPassword1!"},
        timeout=10.0,
    )
    if login_resp.status_code != 200:
        raise RuntimeError(f"Login failed ({login_resp.status_code}): {login_resp.text}")

    session_cookie = login_resp.cookies.get("pullbox_session")
    if not session_cookie:
        raise RuntimeError("Login succeeded but no pullbox_session cookie was returned")

    parsed = urlparse(seeded_server)
    return {
        "name": "pullbox_session",
        "value": session_cookie,
        "domain": parsed.hostname or "127.0.0.1",
        "path": "/",
    }


# ── Browser Fixtures ──────────────────────────────────────────────────


@pytest.fixture()
def authed_page(page: Page, auth_session_cookie: dict[str, str]) -> Page:
    """A Playwright page that is logged in as the admin user.

    Uses a session-scoped authenticated cookie rather than logging in
    through the API for every test.
    """
    page.context.add_cookies([auth_session_cookie])

    return page
