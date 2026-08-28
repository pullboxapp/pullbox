"""Focused E2E coverage for the issue detail rewrite."""

from __future__ import annotations

import base64
import json

import pytest
from playwright.sync_api import expect

from tests.e2e.pages.issue_detail import IssueDetailPage

_READER_PAGE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAMCAIAAADQ/GvKAAAAEklEQVR42mMwqfiGFTGMSqAjAJZBnMFc9NzZ"
    "AAAAAElFTkSuQmCC"
)


def _mock_reader(
    page,
    *,
    initial_page: int = 0,
    fail_once: set[int] | None = None,
    completed: bool = False,
):  # type: ignore[no-untyped-def]
    progress_writes: list[dict[str, object]] = []
    remaining_failures = set(fail_once or set())
    manifest = {
        "issue_id": 1,
        "title": "I Am Gotham",
        "issue_label": "Batman #1",
        "format": "cbz",
        "page_count": 3,
        "revision": "reader-test-revision",
        "initial_page_index": initial_page,
        "page_url_template": (
            "/api/v1/reader/issues/1/pages/{page_index}?revision=reader-test-revision"
        ),
        "progress_url": "/api/v1/reader/issues/1/progress",
        "state": {
            "completed_at": "2026-08-03T00:00:00Z" if completed else None,
        },
    }
    page.route(
        "**/api/v1/reader/issues/1/manifest",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(manifest)
        ),
    )

    def serve_page(route) -> None:  # type: ignore[no-untyped-def]
        page_index = int(route.request.url.split("/pages/", 1)[1].split("?", 1)[0])
        if page_index in remaining_failures:
            remaining_failures.remove(page_index)
            route.fulfill(status=422, content_type="application/json", body="{}")
            return
        route.fulfill(status=200, content_type="image/png", body=_READER_PAGE_PNG)

    def save_progress(route) -> None:  # type: ignore[no-untyped-def]
        payload = json.loads(route.request.post_data or "{}")
        progress_writes.append(payload)
        manifest["initial_page_index"] = int(payload["page_index"])
        if payload.get("reread_started"):
            manifest["state"]["completed_at"] = None
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "page_index": payload["page_index"],
                    "page_count": 3,
                    "revision": "reader-test-revision",
                    "completed_at": (
                        "2026-08-03T00:00:00Z" if payload["completion_candidate"] else None
                    ),
                    "updated_at": "2026-08-03T00:00:00Z",
                    "state": manifest["state"],
                }
            ),
        )

    page.route("**/api/v1/reader/issues/1/pages/*", serve_page)
    page.route(
        "**/api/v1/reader/issues/1/progress",
        save_progress,
    )
    return progress_writes


def _mock_cross_issue_reader(
    page,
    *,
    issue_count: int = 3,
    progress_failures: set[int] | None = None,
    manifest_failures: set[int] | None = None,
    page_failures: set[int] | None = None,
):  # type: ignore[no-untyped-def]
    traces: dict[str, list[object]] = {
        "manifest_requests": [],
        "page_requests": [],
        "progress_writes": [],
        "completion_writes": [],
    }
    failed_progress = set(progress_failures or set())
    failed_manifests = set(manifest_failures or set())
    failed_pages = set(page_failures or set())
    completed: set[int] = set()

    def issue_id_from_url(url: str) -> int:
        return int(url.split("/reader/issues/", 1)[1].split("/", 1)[0])

    def adjacent(issue_id: int) -> dict[str, object]:
        return {
            "issue_id": issue_id,
            "issue_label": f"Batman #{issue_id}",
            "title": f"I Am Gotham {issue_id}",
            "manifest_url": f"/api/v1/reader/issues/{issue_id}/manifest",
            "issue_detail_url": f"/issues/{issue_id}",
            "download_url": f"/api/v1/issues/{issue_id}/download-file",
        }

    def state(issue_id: int) -> dict[str, object]:
        return {
            "page_index": 1 if issue_id in completed else 0,
            "page_count": 2,
            "progress_updated_at": "2026-08-25T00:00:00Z",
            "last_opened_at": "2026-08-25T00:00:00Z",
            "completed_at": ("2026-08-25T00:00:00Z" if issue_id in completed else None),
            "completion_updated_at": ("2026-08-25T00:00:00Z" if issue_id in completed else None),
            "want_to_read": False,
            "want_to_read_updated_at": None,
            "state_version": 2 if issue_id in completed else 1,
        }

    def manifest(issue_id: int) -> dict[str, object]:
        revision = f"reader-revision-{issue_id}"
        return {
            "issue_id": issue_id,
            "title": f"I Am Gotham {issue_id}",
            "issue_label": f"Batman #{issue_id}",
            "format": "cbz",
            "page_count": 2,
            "revision": revision,
            "initial_page_index": 0,
            "page_url_template": (
                f"/api/v1/reader/issues/{issue_id}/pages/{{page_index}}?revision={revision}"
            ),
            "progress_url": f"/api/v1/reader/issues/{issue_id}/progress",
            "completion_url": f"/api/v1/reader/issues/{issue_id}/completion",
            "want_to_read_url": f"/api/v1/reader/issues/{issue_id}/want-to-read",
            "issue_detail_url": f"/issues/{issue_id}",
            "download_url": f"/api/v1/issues/{issue_id}/download-file",
            "state": state(issue_id),
            "previous_issue": adjacent(issue_id - 1) if issue_id > 1 else None,
            "next_issue": adjacent(issue_id + 1) if issue_id < issue_count else None,
        }

    def serve_manifest(route) -> None:  # type: ignore[no-untyped-def]
        issue_id = issue_id_from_url(route.request.url)
        traces["manifest_requests"].append(issue_id)
        if issue_id in failed_manifests:
            route.fulfill(
                status=503,
                content_type="application/json",
                body=json.dumps({"detail": "The next comic is temporarily unavailable."}),
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(manifest(issue_id)),
        )

    def serve_page(route) -> None:  # type: ignore[no-untyped-def]
        issue_id = issue_id_from_url(route.request.url)
        page_index = int(route.request.url.split("/pages/", 1)[1].split("?", 1)[0])
        traces["page_requests"].append((issue_id, page_index))
        if issue_id in failed_pages and page_index == 0:
            route.fulfill(status=422, content_type="application/json", body="{}")
            return
        route.fulfill(status=200, content_type="image/png", body=_READER_PAGE_PNG)

    def save_progress(route) -> None:  # type: ignore[no-untyped-def]
        issue_id = issue_id_from_url(route.request.url)
        payload = json.loads(route.request.post_data or "{}")
        traces["progress_writes"].append((issue_id, payload))
        if issue_id in failed_progress:
            route.fulfill(
                status=500,
                content_type="application/json",
                body=json.dumps({"detail": "Reading position was not saved."}),
            )
            return
        if payload.get("completion_candidate"):
            completed.add(issue_id)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "page_index": payload["page_index"],
                    "page_count": 2,
                    "revision": f"reader-revision-{issue_id}",
                    "completed_at": state(issue_id)["completed_at"],
                    "updated_at": "2026-08-25T00:00:00Z",
                    "state": state(issue_id),
                }
            ),
        )

    def save_completion(route) -> None:  # type: ignore[no-untyped-def]
        issue_id = issue_id_from_url(route.request.url)
        payload = json.loads(route.request.post_data or "{}")
        traces["completion_writes"].append((issue_id, payload))
        if payload.get("completed"):
            completed.add(issue_id)
        else:
            completed.discard(issue_id)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"changed": True, "state": state(issue_id)}),
        )

    page.route("**/api/v1/reader/issues/*/manifest", serve_manifest)
    page.route("**/api/v1/reader/issues/*/pages/*", serve_page)
    page.route("**/api/v1/reader/issues/*/progress", save_progress)
    page.route("**/api/v1/reader/issues/*/completion", save_completion)
    return traces


pytestmark = pytest.mark.e2e


class TestIssueDetailPage:
    """Behavior-first E2E coverage for /issues/{id}."""

    def test_reader_opens_seeded_cbz_through_real_backend(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)

        issue.open_reader()

        assert issue.reader_status.inner_text() == "Page 2 of 3"
        assert issue.reader_page.get_attribute("src") is not None
        next_page = authed_page.locator("[data-testid='comic-reader-next']")
        expect(next_page).to_be_enabled()
        next_page.click()
        expect(issue.reader_status).to_have_text("Page 3 of 3")

    def test_reader_switches_issues_only_after_saving_and_preserves_preferences(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        traces = _mock_cross_issue_reader(authed_page)
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()
        previous_issue = authed_page.locator("[data-testid='comic-reader-previous-issue']")
        next_issue = authed_page.locator("[data-testid='comic-reader-next-issue']")

        expect(previous_issue).to_be_disabled()
        expect(next_issue).to_have_attribute("aria-label", "Next issue, Batman #2")
        assert traces["page_requests"] == [(1, 0), (1, 1)]
        authed_page.locator("[data-testid='comic-reader-direction']").click()
        authed_page.locator("[data-testid='comic-reader-fit-width']").click()
        authed_page.wait_for_timeout(850)

        next_issue.click()

        expect(authed_page.locator("#comic-reader-title")).to_have_text("I Am Gotham 2")
        expect(issue.reader_status).to_have_text("Page 1 of 2")
        expect(authed_page.locator("[data-testid='comic-reader-issue-status']")).to_have_text(
            "Opened Batman #2, page 1 of 2"
        )
        expect(
            authed_page.locator("[data-testid='comic-reader-active-download']")
        ).to_have_attribute("href", "/api/v1/issues/2/download-file")
        assert issue.reader_page.get_attribute("data-fit-mode") == "width"
        assert (
            authed_page.locator("[data-testid='comic-reader-direction']").get_attribute(
                "aria-pressed"
            )
            == "true"
        )
        expect(authed_page.locator("[data-testid='comic-reader-viewport']")).to_be_focused()
        assert traces["page_requests"] == [(1, 0), (1, 1), (2, 0), (2, 1)]
        issue.close_reader()
        expect(issue.read_button).to_be_focused()
        assert authed_page.url.endswith("/issues/1")

    def test_reader_switches_issues_from_the_keyboard(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        traces = _mock_cross_issue_reader(authed_page)
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()
        next_issue = authed_page.locator("[data-testid='comic-reader-next-issue']")

        next_issue.focus()
        expect(next_issue).to_be_focused()
        next_issue.press("Enter")

        expect(authed_page.locator("#comic-reader-title")).to_have_text("I Am Gotham 2")
        expect(authed_page.locator("[data-testid='comic-reader-viewport']")).to_be_focused()
        assert traces["manifest_requests"] == [1, 2]

    def test_reader_blocks_issue_switch_when_progress_cannot_be_saved(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        _mock_cross_issue_reader(authed_page, progress_failures={1})
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()
        authed_page.wait_for_timeout(850)

        authed_page.locator("[data-testid='comic-reader-next-issue']").click()

        expect(authed_page.locator("#comic-reader-title")).to_have_text("I Am Gotham 1")
        expect(issue.reader_status).to_have_text("Page 1 of 2")
        expect(authed_page.locator("[data-testid='comic-reader-switch-error']")).to_have_text(
            "Your reading position hasn’t saved yet. Try again before changing issues."
        )

    def test_reader_retains_current_issue_when_target_manifest_fails(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        _mock_cross_issue_reader(authed_page, manifest_failures={2})
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()
        authed_page.wait_for_timeout(850)

        authed_page.locator("[data-testid='comic-reader-next-issue']").click()

        expect(authed_page.locator("#comic-reader-title")).to_have_text("I Am Gotham 1")
        expect(issue.reader_page).to_be_visible()
        expect(authed_page.locator("[data-testid='comic-reader-switch-error']")).to_have_text(
            "The next comic is temporarily unavailable."
        )

    def test_reader_retains_current_issue_when_target_first_page_fails(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        _mock_cross_issue_reader(authed_page, page_failures={2})
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()
        original_page_url = issue.reader_page.get_attribute("src")
        authed_page.wait_for_timeout(850)

        authed_page.locator("[data-testid='comic-reader-next-issue']").click()

        expect(authed_page.locator("[data-testid='comic-reader-switch-error']")).to_have_text(
            "The next comic page could not be displayed."
        )
        expect(authed_page.locator("#comic-reader-title")).to_have_text("I Am Gotham 1")
        expect(issue.reader_page).to_have_attribute("src", original_page_url or "")
        expect(issue.reader_page).to_be_visible()

    def test_reader_shows_completion_state_and_can_open_the_next_issue(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        traces = _mock_cross_issue_reader(authed_page)
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()

        authed_page.locator("[data-testid='comic-reader-next']").click()
        expect(issue.reader_status).to_have_text("Page 2 of 2")
        authed_page.wait_for_timeout(850)

        completion = authed_page.locator("[data-testid='comic-reader-completion']")
        expect(completion).to_be_visible()
        expect(completion).to_contain_text("Finished Batman #1")
        expect(authed_page.locator("[data-testid='comic-reader-read-next']")).to_have_text(
            "Read next issue"
        )
        assert traces["progress_writes"][-1][0] == 1  # type: ignore[index]
        authed_page.locator("[data-testid='comic-reader-read-next']").click()
        expect(authed_page.locator("#comic-reader-title")).to_have_text("I Am Gotham 2")

    def test_reader_caught_up_state_can_be_marked_unread(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        traces = _mock_cross_issue_reader(authed_page, issue_count=1)
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()

        authed_page.locator("[data-testid='comic-reader-next']").click()
        authed_page.wait_for_timeout(850)
        completion = authed_page.locator("[data-testid='comic-reader-completion']")
        expect(completion).to_contain_text("You’re caught up in this series.")

        authed_page.locator("[data-testid='comic-reader-mark-unread']").click()

        expect(completion).to_be_hidden()
        assert traces["completion_writes"] == [(1, {"completed": False})]
        expect(authed_page.locator("[data-testid='comic-reader-issue-status']")).to_have_text(
            "Batman #1 marked unread"
        )

    def test_reader_ignores_rapid_duplicate_issue_switches(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        traces = _mock_cross_issue_reader(authed_page)
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()

        authed_page.evaluate(
            """() => {
                const root = document.querySelector('[data-testid="issue-detail-page"]');
                const state = root && root._x_dataStack ? root._x_dataStack[0] : null;
                return Promise.all([state.readerNextIssue(), state.readerNextIssue()]);
            }"""
        )

        expect(authed_page.locator("#comic-reader-title")).to_have_text("I Am Gotham 2")
        assert traces["manifest_requests"] == [1, 2]

    def test_reader_releases_issue_resources_across_fifty_explicit_switches(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        traces = _mock_cross_issue_reader(authed_page, issue_count=51)
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()
        assert {request[0] for request in traces["page_requests"]} == {1}  # type: ignore[index]

        for issue_id in range(2, 52):
            assert all(
                request[0] < issue_id
                for request in traces["page_requests"]  # type: ignore[index]
            )
            authed_page.locator("[data-testid='comic-reader-next-issue']").click()
            expect(authed_page.locator("#comic-reader-title")).to_have_text(
                f"I Am Gotham {issue_id}"
            )

        resources = authed_page.evaluate(
            """() => {
                const root = document.querySelector('[data-testid="issue-detail-page"]');
                const state = root && root._x_dataStack ? root._x_dataStack[0] : null;
                return state ? {
                    prefetchImages: state.readerPrefetchImages.length,
                    manifestController: Boolean(state.readerManifestController),
                    progressController: Boolean(state.readerProgressController),
                } : null;
            }"""
        )
        assert resources is not None
        assert resources["prefetchImages"] <= 1
        assert resources["manifestController"] is False
        assert resources["progressController"] is False
        assert {request[0] for request in traces["page_requests"]} == set(range(1, 52))  # type: ignore[index]

    def test_read_query_opens_once_and_restores_focus_to_the_canonical_action(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        _mock_reader(authed_page)
        issue = IssueDetailPage(authed_page, seeded_server)

        issue.navigate("/issues/1?read=1")
        issue.wait_until_ready()
        issue.reader_dialog.wait_for(state="visible", timeout=5000)
        issue.reader_page.wait_for(state="visible", timeout=5000)

        assert authed_page.url.endswith("/issues/1")
        issue.close_reader()
        expect(issue.read_button).to_be_focused()

    def test_reading_state_action_refreshes_only_the_issue_hero(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
        seeded_reader_state_guard: None,
    ) -> None:
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        authed_page.evaluate(
            """() => document.querySelector('[data-testid="issue-detail-page"]')
                ?.setAttribute('data-reading-shell', 'preserved')"""
        )

        completion = authed_page.locator("[data-testid='issue-action-completion']").first
        expect(completion).to_have_text("Mark read")
        completion.click()

        expect(authed_page.locator("[data-testid='issue-reading-state']").first).to_have_text(
            "Read"
        )
        expect(authed_page.locator("[data-testid='issue-action-completion']").first).to_have_text(
            "Mark unread"
        )
        assert issue.page_shell.get_attribute("data-reading-shell") == "preserved"

        authed_page.locator("[data-testid='issue-action-completion']").first.click()
        expect(authed_page.locator("[data-testid='issue-action-completion']").first).to_have_text(
            "Mark read"
        )

    def test_reader_opens_navigates_and_restores_exact_issue_context(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        _mock_reader(authed_page)
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        original_url = authed_page.url
        authed_page.evaluate("window.scrollTo(0, 180)")
        original_scroll = authed_page.evaluate("window.scrollY")

        issue.open_reader()

        assert authed_page.url == original_url
        assert issue.reader_status.inner_text() == "Page 1 of 3"
        authed_page.locator("[data-testid='comic-reader-next']").click()
        expect(issue.reader_status).to_have_text("Page 2 of 3")
        issue.close_reader()

        assert authed_page.url == original_url
        assert authed_page.evaluate("window.scrollY") == original_scroll
        expect(issue.read_button).to_be_focused()

    def test_reader_keyboard_direction_and_sizing_controls(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        _mock_reader(authed_page)
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()

        authed_page.keyboard.press("ArrowRight")
        expect(issue.reader_status).to_have_text("Page 2 of 3")
        authed_page.keyboard.press("Home")
        expect(issue.reader_status).to_have_text("Page 1 of 3")
        authed_page.locator("[data-testid='comic-reader-direction']").click()
        authed_page.keyboard.press("ArrowLeft")
        expect(issue.reader_status).to_have_text("Page 2 of 3")

        authed_page.locator("[data-testid='comic-reader-fit-width']").click()
        assert issue.reader_page.get_attribute("data-fit-mode") == "width"
        authed_page.locator("[data-testid='comic-reader-zoom-in']").click()
        assert issue.reader_page.get_attribute("data-fit-mode") == "actual"

    def test_reader_positions_sizing_left_and_navigation_right(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        _mock_reader(authed_page)
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()

        sizing_box = authed_page.locator(".comic-reader__sizing").bounding_box()
        navigation_box = authed_page.locator(".comic-reader__navigation").bounding_box()

        assert sizing_box is not None
        assert navigation_box is not None
        assert sizing_box["x"] < navigation_box["x"]

    def test_reader_fullscreen_targets_shell_and_tracks_browser_state(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.add_init_script(
            """
            (() => {
                let fullscreenTarget = null;
                Object.defineProperty(document, "fullscreenElement", {
                    configurable: true,
                    get: () => fullscreenTarget,
                });
                Element.prototype.requestFullscreen = function () {
                    fullscreenTarget = this;
                    window.__readerFullscreenTargetClass = this.className;
                    document.dispatchEvent(new Event("fullscreenchange"));
                    return Promise.resolve();
                };
                document.exitFullscreen = function () {
                    fullscreenTarget = null;
                    document.dispatchEvent(new Event("fullscreenchange"));
                    return Promise.resolve();
                };
            })();
            """
        )
        _mock_reader(authed_page)
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()
        fullscreen_button = authed_page.locator("[data-testid='comic-reader-fullscreen']")

        fullscreen_button.click()

        assert authed_page.evaluate("window.__readerFullscreenTargetClass") == (
            "comic-reader__shell"
        )
        assert fullscreen_button.get_attribute("aria-pressed") == "true"

        fullscreen_button.click()
        assert fullscreen_button.get_attribute("aria-pressed") == "false"

    def test_reader_settles_resumes_and_only_deliberate_final_page_completes(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        progress_writes = _mock_reader(authed_page, initial_page=2)
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()

        authed_page.wait_for_timeout(850)
        assert progress_writes[-1]["page_index"] == 2
        assert progress_writes[-1]["completion_candidate"] is False

        authed_page.locator("[data-testid='comic-reader-previous']").click()
        expect(issue.reader_status).to_have_text("Page 2 of 3")
        authed_page.locator("[data-testid='comic-reader-next']").click()
        expect(issue.reader_status).to_have_text("Page 3 of 3")
        authed_page.wait_for_timeout(850)
        assert progress_writes[-1]["completion_candidate"] is True

        issue.close_reader()
        issue.open_reader()
        expect(issue.reader_status).to_have_text("Page 3 of 3")

    def test_completed_issue_leaves_read_only_after_reread_page_settles(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        progress_writes = _mock_reader(authed_page, completed=True)
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()

        assert progress_writes == []
        authed_page.wait_for_timeout(850)

        assert progress_writes[-1]["page_index"] == 0
        assert progress_writes[-1]["completion_candidate"] is False
        assert progress_writes[-1]["reread_started"] is True

    def test_failed_reread_page_keeps_completed_state_unchanged(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        progress_writes = _mock_reader(authed_page, completed=True, fail_once={0})
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.read_button.click()
        issue.reader_dialog.wait_for(state="visible", timeout=5000)

        authed_page.wait_for_timeout(850)

        assert progress_writes == []
        expect(authed_page.get_by_text("Page 1 could not be displayed.")).to_be_visible()

    def test_reader_page_jump_input_is_bounded_and_blocks_shortcuts_while_typing(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        _mock_reader(authed_page)
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()
        page_input = authed_page.locator(".comic-reader__page-jump input")

        page_input.fill("99")
        page_input.press("Enter")
        assert page_input.get_attribute("aria-invalid") == "true"
        expect(authed_page.locator("#comic-reader-page-input-error")).to_have_text(
            "Enter a page from 1 to 3."
        )
        assert issue.reader_status.inner_text() == "Page 1 of 3"

        page_input.fill("w")
        assert issue.reader_page.get_attribute("data-fit-mode") == "page"
        page_input.fill("2")
        page_input.press("Enter")
        expect(issue.reader_status).to_have_text("Page 2 of 3")

    def test_reader_page_error_preserves_context_and_retries_in_place(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        _mock_reader(authed_page, fail_once={2})
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()

        authed_page.keyboard.press("End")
        error = authed_page.locator("[data-testid='comic-reader-error']")
        error.wait_for(state="visible", timeout=5000)
        assert issue.reader_page.is_visible()
        expect(error).to_contain_text("Page 3 could not be displayed")

        authed_page.locator("[data-testid='comic-reader-retry']").click()
        expect(issue.reader_status).to_have_text("Page 3 of 3")
        error.wait_for(state="hidden", timeout=5000)

    def test_reader_swipe_respects_fit_mode_and_center_zone_toggles_controls(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        _mock_reader(authed_page)
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()

        authed_page.locator("[data-testid='comic-reader-viewport']").evaluate(
            """(viewport) => {
                viewport.dispatchEvent(new PointerEvent('pointerdown', {
                    bubbles: true, pointerId: 7, pointerType: 'touch', isPrimary: true,
                    clientX: 300, clientY: 200,
                }));
                viewport.dispatchEvent(new PointerEvent('pointerup', {
                    bubbles: true, pointerId: 7, pointerType: 'touch', isPrimary: true,
                    clientX: 120, clientY: 205,
                }));
            }"""
        )
        expect(issue.reader_status).to_have_text("Page 2 of 3")

        authed_page.locator("[data-testid='comic-reader-zoom-in']").click()
        authed_page.locator("[data-testid='comic-reader-viewport']").evaluate(
            """(viewport) => {
                viewport.dispatchEvent(new PointerEvent('pointerdown', {
                    bubbles: true, pointerId: 8, pointerType: 'touch', isPrimary: true,
                    clientX: 300, clientY: 200,
                }));
                viewport.dispatchEvent(new PointerEvent('pointerup', {
                    bubbles: true, pointerId: 8, pointerType: 'touch', isPrimary: true,
                    clientX: 120, clientY: 205,
                }));
            }"""
        )
        assert issue.reader_status.inner_text() == "Page 2 of 3"

        authed_page.locator(".comic-reader__tap-zone--center").click(position={"x": 2, "y": 2})
        shell_class = authed_page.locator(".comic-reader__shell").get_attribute("class") or ""
        assert "is-controls-hidden" in shell_class

    @pytest.mark.parametrize(
        ("width", "height"),
        [(320, 568), (568, 320), (390, 844), (844, 390), (820, 1180)],
    )
    def test_reader_is_full_viewport_and_controls_remain_reachable(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
        width: int,
        height: int,
    ) -> None:
        authed_page.set_viewport_size({"width": width, "height": height})
        _mock_reader(authed_page)
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()

        dialog_box = issue.reader_dialog.bounding_box()
        close_box = authed_page.locator("[data-testid='comic-reader-close']").bounding_box()
        next_box = authed_page.locator("[data-testid='comic-reader-next']").bounding_box()

        assert dialog_box is not None
        assert close_box is not None
        assert next_box is not None
        assert dialog_box["width"] == pytest.approx(width, abs=1)
        assert dialog_box["height"] == pytest.approx(height, abs=1)
        for control_box in (close_box, next_box):
            assert control_box["x"] >= 0
            assert control_box["y"] >= 0
            assert control_box["x"] + control_box["width"] <= width + 1
            assert control_box["y"] + control_box["height"] <= height + 1

    def test_reader_reflows_at_200_percent_equivalent_without_horizontal_overflow(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.set_viewport_size({"width": 320, "height": 568})
        _mock_cross_issue_reader(authed_page)
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()

        geometry = authed_page.evaluate(
            """() => ({
                documentWidth: document.documentElement.scrollWidth,
                viewportWidth: document.documentElement.clientWidth,
                issueControlsVisible: Array.from(
                    document.querySelectorAll('[data-testid="comic-reader-previous-issue"], [data-testid="comic-reader-next-issue"]')
                ).every((control) => {
                    const rect = control.getBoundingClientRect();
                    return rect.left >= 0 && rect.right <= window.innerWidth;
                }),
            })"""
        )

        assert geometry["documentWidth"] <= geometry["viewportWidth"]
        assert geometry["issueControlsVisible"] is True

    def test_reader_controls_remain_usable_with_reduced_motion(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.emulate_media(reduced_motion="reduce")
        _mock_cross_issue_reader(authed_page)
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)
        issue.open_reader()

        next_issue = authed_page.locator("[data-testid='comic-reader-next-issue']")
        expect(next_issue).to_be_visible()
        next_issue.click()

        expect(authed_page.locator("#comic-reader-title")).to_have_text("I Am Gotham 2")
        expect(authed_page.locator("[data-testid='comic-reader-close']")).to_be_visible()

    def test_copy_path_tooltip_renders_on_hover(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)

        issue.library_file_copy.hover()

        assert issue.library_file_copy.get_attribute("data-tip") == "Copy path"

    def test_initial_load_renders_stable_issue_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)

        header_box = authed_page.locator("[data-testid='app-header']").bounding_box()
        hero_box = issue.hero.bounding_box()

        assert header_box is not None
        assert hero_box is not None
        assert issue.page_shell.is_visible()
        assert issue.back_link.is_visible()
        assert issue.hero.is_visible()
        assert issue.hero_summary_panel.is_visible()
        title_link = authed_page.locator("[data-testid='issue-detail-title-link']").first
        assert title_link.is_visible()
        assert title_link.get_attribute("href") == (
            "https://comicvine.gamespot.com/batman-1/4000-50001/"
        )
        assert title_link.get_attribute("target") == "_blank"
        assert issue.hero_actions_panel.is_visible()
        actions_title = authed_page.locator("[data-testid='issue-detail-actions-title']").first
        assert actions_title.inner_text().lower() == "manage issue"
        download_box = authed_page.locator(
            "[data-testid='issue-action-download']"
        ).first.bounding_box()
        manual_search_box = authed_page.locator(
            "[data-testid='issue-action-manual-search']"
        ).first.bounding_box()
        assert download_box is not None
        assert manual_search_box is not None
        assert abs(download_box["width"] - manual_search_box["width"]) <= 2
        action_gaps = authed_page.evaluate(
            """() => [
                getComputedStyle(document.querySelector("[data-testid='issue-action-download']")).gap,
                getComputedStyle(document.querySelector("[data-testid='issue-action-manual-search']")).gap,
            ]"""
        )
        assert action_gaps == ["8px", "8px"]
        assert issue.page.locator("[data-testid='issue-action-manual-search']").first.is_visible()
        assert not issue.search_region.is_visible()
        assert issue.description_section.is_visible()
        assert issue.description_title.is_visible()
        assert issue.creators_section.is_visible()
        assert issue.creators_title.is_visible()
        assert issue.library_file_section.is_visible()
        assert issue.library_file_title.is_visible()
        assert issue.library_file_copy.is_visible()
        assert issue.footer.is_visible()
        assert authed_page.locator("[data-testid='issue-detail-telemetry-strip']").count() == 0
        assert hero_box["y"] >= header_box["y"] + header_box["height"] + 12

    def test_back_link_returns_to_series_detail_without_shell_blank(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)

        issue.open_back_link()

        assert "/series/" in authed_page.url
        assert authed_page.locator("[data-testid='page-footer-dock']").first.is_visible()
        assert authed_page.locator("h1, h2").filter(has_text="Batman").first.is_visible()

    def test_tab_switch_keeps_issue_detail_content_visible(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)

        issue.round_trip_tab_visibility()

        assert issue.page_shell.is_visible()
        assert issue.hero.is_visible()
        assert issue.description_section.is_visible()
        assert issue.footer.is_visible()
        assert (
            authed_page.locator("#content").first.get_attribute("data-detail-history-hidden")
            is None
        )

    def test_status_row_uses_shared_pill_contracts(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)

        status_contract = authed_page.evaluate(
            """() => {
                const row = document.querySelector("[data-testid='issue-detail-status-row']");
                const pills = row ? Array.from(row.querySelectorAll(".pill")) : [];
                const first = pills[0];
                if (!first) {
                    return {
                        count: pills.length,
                        firstBackground: null,
                        firstFontFamily: null,
                    };
                }
                const firstStyle = window.getComputedStyle(first);
                return {
                    count: pills.length,
                    firstBackground: firstStyle.backgroundColor,
                    firstFontFamily: firstStyle.fontFamily,
                };
            }"""
        )

        assert status_contract["count"] >= 4
        assert status_contract["firstBackground"] not in ("rgba(0, 0, 0, 0)", "transparent")
        assert "DM Sans" in status_contract["firstFontFamily"]

    def test_sections_share_a_wider_aligned_content_width(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.set_viewport_size({"width": 1600, "height": 1200})
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)

        geometry = authed_page.evaluate(
            """() => {
                const hero = document.querySelector("[data-testid='issue-detail-hero']");
                const actions = document.querySelector("[data-testid='issue-detail-hero-actions-panel']");
                const sections = [
                    document.querySelector("[data-testid='issue-description-section']"),
                    document.querySelector("[data-testid='issue-creators-section']"),
                    document.querySelector("[data-testid='issue-library-file-section']"),
                ];
                const rectFor = (el) => {
                    if (!el) return null;
                    const rect = el.getBoundingClientRect();
                    return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, width: rect.width };
                };
                return {
                    hero: rectFor(hero),
                    actions: rectFor(actions),
                    sections: sections.map(rectFor),
                    actionsParentTestId: actions?.closest("[data-testid='issue-detail-hero']")?.dataset.testid || null,
                };
            }"""
        )

        assert geometry["hero"] is not None
        assert geometry["actions"] is not None
        assert all(rect is not None for rect in geometry["sections"])
        assert geometry["actionsParentTestId"] == "issue-detail-hero"
        lefts = [geometry["hero"]["left"], *(rect["left"] for rect in geometry["sections"])]
        rights = [geometry["hero"]["right"], *(rect["right"] for rect in geometry["sections"])]
        widths = [geometry["hero"]["width"], *(rect["width"] for rect in geometry["sections"])]
        assert max(lefts) - min(lefts) <= 2
        assert max(rights) - min(rights) <= 2
        assert min(widths) >= 1200
        assert geometry["actions"]["left"] > geometry["hero"]["left"] + 330
        assert geometry["actions"]["right"] <= geometry["hero"]["right"] + 1
        assert geometry["actions"]["top"] >= geometry["hero"]["top"]
        assert geometry["actions"]["bottom"] <= geometry["hero"]["bottom"]

    def test_wanted_issue_import_file_browser_opens_without_disturbing_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(2)

        authed_page.route(
            "**/api/v1/filesystem/browse?**",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "path": "/tmp/imports",
                        "parent": "/tmp",
                        "directories": [],
                        "files": [
                            {
                                "name": "Batman 002.cbz",
                                "path": "/tmp/imports/Batman 002.cbz",
                                "size": 52428800,
                            }
                        ],
                        "quick_links": [],
                    }
                ),
            ),
        )

        assert issue.hero_actions_panel.is_visible()
        issue.open_import_file_browser()

        assert issue.page_shell.is_visible()
        assert issue.hero.is_visible()
        assert issue.hero_actions_panel.is_visible()
        assert issue.file_browser_modal.is_visible()
        assert issue.import_modal.is_visible() is False

        issue.close_file_browser()

        assert issue.page_shell.is_visible()
        assert issue.hero_actions_panel.is_visible()
        assert issue.footer.is_visible()

    def test_wanted_issue_import_selection_opens_live_progress_modal(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(2)

        authed_page.route(
            "**/api/v1/filesystem/browse?**",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "path": "/tmp/imports",
                        "parent": "/tmp",
                        "directories": [],
                        "files": [
                            {
                                "name": "Batman 002.cbz",
                                "path": "/tmp/imports/Batman 002.cbz",
                                "size": 52428800,
                            }
                        ],
                        "quick_links": [],
                    }
                ),
            ),
        )

        def handle_import_start(route) -> None:  # type: ignore[no-untyped-def]
            route.fulfill(
                status=202,
                content_type="application/json",
                body=json.dumps(
                    {
                        "issue_id": 2,
                        "state": "running",
                        "message": "Preparing import...",
                        "current_file_name": "Batman 002.cbz",
                        "current_file_stage": "preparing",
                        "current_file_progress_current": 0,
                        "current_file_progress_total": 1,
                        "current_file_progress_pct": 0,
                        "current_file_progress_unit": "steps",
                        "file_index": 1,
                        "total_files": 1,
                    }
                ),
            )

        authed_page.route("**/api/v1/issues/2/import-file/start", handle_import_start)
        authed_page.route(
            "**/api/v1/issues/2/import-file/progress",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "issue_id": 2,
                        "state": "running",
                        "message": "Importing selected file...",
                        "current_file_name": "Batman 002.cbz",
                        "current_file_stage": "transferring",
                        "current_file_progress_current": 26214400,
                        "current_file_progress_total": 52428800,
                        "current_file_progress_pct": 50,
                        "current_file_progress_unit": "bytes",
                        "file_index": 1,
                        "total_files": 1,
                    }
                ),
            ),
        )

        issue.open_import_file_browser()
        with authed_page.expect_request("**/api/v1/issues/2/import-file/start") as request_info:
            authed_page.locator("[data-testid='file-browser-file-entry']").first.click()

        assert request_info.value.post_data_json == {
            "allow_resource_safety_exception": False,
            "file_path": "/tmp/imports/Batman 002.cbz",
            "move_to_library": True,
        }
        issue.import_modal.wait_for(state="visible", timeout=5000)
        assert authed_page.locator("[data-testid='issue-import-progress-bar']").first.is_visible()
        assert authed_page.locator("[data-testid='issue-import-progress-value']").first.is_visible()

    def test_manual_import_cancel_posts_cancel_and_closes_modal(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(2)

        authed_page.route(
            "**/api/v1/filesystem/browse?**",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "path": "/tmp/imports",
                        "parent": "/tmp",
                        "directories": [],
                        "files": [
                            {
                                "name": "Batman 002.cbz",
                                "path": "/tmp/imports/Batman 002.cbz",
                                "size": 52428800,
                            }
                        ],
                        "quick_links": [],
                    }
                ),
            ),
        )
        authed_page.route(
            "**/api/v1/issues/2/import-file/start",
            lambda route: route.fulfill(
                status=202,
                content_type="application/json",
                body=json.dumps(
                    {
                        "issue_id": 2,
                        "state": "running",
                        "message": "Preparing import...",
                        "current_file_name": "Batman 002.cbz",
                        "current_file_stage": "preparing",
                        "current_file_progress_current": 0,
                        "current_file_progress_total": 1,
                        "current_file_progress_pct": 0,
                        "current_file_progress_unit": "steps",
                        "file_index": 1,
                        "total_files": 1,
                    }
                ),
            ),
        )
        authed_page.route(
            "**/api/v1/issues/2/import-file/progress",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "issue_id": 2,
                        "state": "running",
                        "message": "Importing selected file...",
                        "current_file_name": "Batman 002.cbz",
                        "current_file_stage": "transferring",
                        "current_file_progress_current": 26214400,
                        "current_file_progress_total": 52428800,
                        "current_file_progress_pct": 50,
                        "current_file_progress_unit": "bytes",
                        "file_index": 1,
                        "total_files": 1,
                    }
                ),
            ),
        )
        authed_page.route(
            "**/api/v1/issues/2/import-file/cancel",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "issue_id": 2,
                        "state": "cancelled",
                        "message": "Import cancelled.",
                    }
                ),
            ),
        )

        issue.open_import_file_browser()
        authed_page.locator("[data-testid='file-browser-file-entry']").first.click()
        issue.import_modal.wait_for(state="visible", timeout=5000)

        with authed_page.expect_request("**/api/v1/issues/2/import-file/cancel"):
            authed_page.locator("[data-testid='issue-import-cancel']").click()

        issue.import_modal.wait_for(state="hidden", timeout=5000)

    def test_manual_search_block_posts_to_blocklist(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(2)

        authed_page.evaluate("() => { window.pbConfirm = async () => true; }")

        authed_page.route(
            "**/htmx/issues/2/search-results",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body="""
<div data-testid="issue-search-results">
  <table class="downloads-table issue-search-results-table">
    <tbody id="issue-search-results-body">
      <tr>
        <td class="is-right" x-data="{ grabbing: false, blocking: false, blocked: false, blockRelease(btn) { if (this.grabbing || this.blocking || this.blocked) return; pbConfirm({ title: 'Block Release', message: 'Add this release to the blocklist? It won\\'t appear in future search results.', confirmText: 'Block' }).then((ok) => { if (!ok) return; this.blocking = true; fetch('/api/v1/blocklist', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': 'test-csrf' }, body: JSON.stringify({ release_title: btn.dataset.blockTitle, series_id: 1, issue_id: 2 }) }).then((response) => response.json().catch(() => ({})).then((data) => ({ response, data }))).then(({ response, data }) => { const detail = data.detail?.error?.message || data.detail || data.error?.message || ''; const alreadyBlocked = response.status === 409 && String(detail).toLowerCase().includes('already in blocklist'); if (!response.ok && !alreadyBlocked) { throw new Error(detail || 'Failed to block release.'); } this.blocked = true; }).finally(() => { this.blocking = false; }); }); } }">
          <div class="issue-search-action-row">
            <button
              x-show="!blocked"
              data-block-title="Batman.018.2026.Digital.Zone-Empire"
              @click="blockRelease($el)"
              class="chip-btn chip-btn-sm chip-btn-error cursor-pointer"
            >
              Block
            </button>
            <span x-show="blocked" x-cloak class="pill pill-error opacity-60">Blocked</span>
          </div>
        </td>
      </tr>
    </tbody>
  </table>
</div>
""",
            ),
        )

        authed_page.route(
            "**/api/v1/blocklist",
            lambda route: route.fulfill(
                status=201,
                content_type="application/json",
                body=json.dumps(
                    {
                        "id": 1,
                        "release_title": "Batman.018.2026.Digital.Zone-Empire",
                        "release_title_normalized": "batman.018.2026.digital.zone-empire",
                        "download_url": None,
                        "series_id": 1,
                        "issue_id": 2,
                        "indexer_id": None,
                        "reason": "manual",
                        "error_message": None,
                        "release_group": None,
                        "download_history_id": None,
                        "series_title": "Batman",
                        "created_at": "2026-05-02T20:00:00Z",
                        "updated_at": "2026-05-02T20:00:00Z",
                    }
                ),
            ),
        )

        issue.run_manual_search()

        with authed_page.expect_request("**/api/v1/blocklist") as request_info:
            issue.search_region.locator("button", has_text="Block").first.click()

        payload = json.loads(request_info.value.post_data or "{}")
        assert payload == {
            "release_title": "Batman.018.2026.Digital.Zone-Empire",
            "series_id": 1,
            "issue_id": 2,
        }
        issue.search_region.locator("text=Blocked").first.wait_for(state="visible", timeout=5000)

    def test_manual_search_modal_opens_without_disturbing_shell(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)

        issue.run_manual_search()

        assert issue.page_shell.is_visible()
        assert issue.hero.is_visible()
        assert issue.search_region.is_visible()

    def test_tab_switch_preserves_active_issue_manual_search(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)

        issue.run_manual_search()
        issue.round_trip_tab_visibility()

        assert issue.page_shell.is_visible()
        assert issue.hero.is_visible()
        assert issue.search_region.is_visible()
        assert issue.search_results.is_visible() or issue.search_results_empty_state.is_visible()
        assert (
            authed_page.locator("#content").first.get_attribute("data-detail-history-hidden")
            is None
        )
        assert issue.search_results.is_visible() or issue.search_results_empty_state.is_visible()
        assert authed_page.locator("[data-testid='issue-search-modal-footer-close']").is_visible()
        assert issue.footer.is_visible()

        issue.close_manual_search()

        assert issue.page_shell.is_visible()
        assert issue.footer.is_visible()

    def test_manual_search_modal_fits_action_controls_without_clipping(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        authed_page.set_viewport_size({"width": 1600, "height": 1200})
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(1)

        issue.run_manual_search()

        modal_box = issue.search_region.locator(".issue-search-modal-panel").first.bounding_box()
        action_rows = issue.search_results.locator(".issue-search-action-row")

        assert modal_box is not None
        assert modal_box["width"] >= 1180
        if action_rows.count() > 0:
            action_box = action_rows.first.bounding_box()
            assert action_box is not None
            assert (action_box["x"] + action_box["width"]) <= (
                modal_box["x"] + modal_box["width"] - 12
            )

    def test_skipped_issue_can_be_marked_wanted_from_detail_page(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        issue = IssueDetailPage(authed_page, seeded_server)
        issue.goto(3)

        assert authed_page.locator("[data-testid='issue-action-toggle']").first.text_content()
        assert "Skipped" in (issue.status_row.text_content() or "")
        issue.toggle_status()

        assert "Wanted" in (issue.status_row.text_content() or "")
        assert authed_page.locator("[data-testid='issue-action-search']").first.is_visible()
        assert "Mark Skipped" in (
            authed_page.locator("[data-testid='issue-action-toggle']").first.text_content() or ""
        )
