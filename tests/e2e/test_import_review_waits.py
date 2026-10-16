"""Browser regressions for request-specific import review test waits."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests.e2e.pages.import_page import ImportPage

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

BASE_URL = "http://review.test"
REVIEW_URL = f"{BASE_URL}/import/7/review-partial?status=ready"


def _review_page(page: Page, *, mode: str = "success", status: int = 200) -> ImportPage:
    # Keep the pre-existing state identical to the requested state: it cannot
    # establish that this click actually finished a new refresh.
    page.route(REVIEW_URL, lambda route: route.fulfill(status=status, body="ready"))
    page.route(
        f"{BASE_URL}/import",
        lambda route: route.fulfill(
            content_type="text/html",
            body="""
                <div id="unrelated" class="htmx-request"></div>
                <div id="import-step-review-shell" data-revision="old">
                  <input name="review_status_filter" value="ready">
                  <button hx-get="/import/7/review-partial?status=ready"
                          hx-target="#import-step-review-shell">Refresh</button>
                </div>
                <script>
                  window.htmx = {};
                  document.querySelector('button').onclick = async () => {
                    if (window.mode === 'no-request') return;
                    const shell = document.querySelector('#import-step-review-shell');
                    shell.classList.add('htmx-request');
                    window.__pbImportReviewNavPending = true;
                    // An unrelated event must not finish the review wait.
                    setTimeout(() => document.dispatchEvent(new CustomEvent(
                        'htmx:afterSettle', {detail: {target: document.querySelector('#unrelated')}}
                    )), 10);
                    try {
                      const response = await fetch(document.querySelector('button').getAttribute('hx-get'));
                      await response.text();
                      if (!response.ok) return;
                      await new Promise(resolve => setTimeout(resolve, 150));
                      shell.dataset.revision = 'new';
                    } finally {
                      if (window.mode !== 'stuck') {
                        shell.classList.remove('htmx-request');
                        window.__pbImportReviewNavPending = false;
                      }
                    }
                  };
                </script>
            """,
        ),
    )
    page.goto(f"{BASE_URL}/import")
    page.evaluate("mode => window.mode = mode", mode)
    return ImportPage(page, BASE_URL)


def test_review_wait_requires_fresh_response_and_finished_dom_update(page: Page) -> None:
    review = _review_page(page)

    review.click_review_control(page.get_by_role("button", name="Refresh"), timeout=2000)

    assert page.locator("#import-step-review-shell").get_attribute("data-revision") == "new"
    assert page.locator("#unrelated").get_attribute("class") == "htmx-request"
    assert page.locator("input").input_value() == "ready"


def test_review_wait_rejects_failed_response(page: Page) -> None:
    review = _review_page(page, status=500)

    with pytest.raises(AssertionError, match=r"review.*500"):
        review.click_review_control(page.get_by_role("button", name="Refresh"), timeout=2000)


def test_review_wait_cannot_accept_stale_ready_state_without_request(page: Page) -> None:
    review = _review_page(page, mode="no-request")

    with pytest.raises(PlaywrightTimeoutError):
        review.click_review_control(page.get_by_role("button", name="Refresh"), timeout=1000)

    assert page.locator("#import-step-review-shell").get_attribute("data-revision") == "old"


def test_review_wait_times_out_if_target_never_finishes(page: Page) -> None:
    review = _review_page(page, mode="stuck")

    with pytest.raises(PlaywrightTimeoutError):
        review.click_review_control(page.get_by_role("button", name="Refresh"), timeout=1000)

    assert page.locator("#import-step-review-shell").get_attribute("data-revision") == "new"
