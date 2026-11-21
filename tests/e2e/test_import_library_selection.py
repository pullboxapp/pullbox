"""Untagged imports can choose a saved native series without leaving review."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _run_async_blocking
from tests.ui.test_import_library_selection import seed_library_choice

pytestmark = pytest.mark.e2e


async def _seed(root, namespace, theme):
    from pullbox.database import get_session_factory

    async with get_session_factory()() as session:
        identity_base = (
            9300000
            + ["light", "dark", "tron"].index(theme) * 10000
            + ["metron", "gcd"].index(namespace) * 1000
        )
        job_id, _, series_id, _, _, _, _ = await seed_library_choice(
            session, root, namespace, identity_base, admit=False, other_catalog=True
        )
        return job_id, series_id


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("theme", ["light", "dark", "tron"])
def test_library_series_and_issue_choices(
    authed_page, seeded_server, tmp_path, namespace, theme, browser_name
):
    page = authed_page
    job_id, series_id = _run_async_blocking(_seed(tmp_path, namespace, theme))
    page.add_init_script(f"localStorage.setItem('pullbox-theme', '{theme}')")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{seeded_server}/import?tab=collection&resume_job_id={job_id}&resume_step=3")
    page.locator("#import-step-review-shell").evaluate("el => window.libraryReviewShell = el")
    page.get_by_test_id("import-review-choose-library").click()
    modal = page.get_by_test_id("import-library-series-choices")
    expect(modal).to_be_visible()
    modal.get_by_role("button", name="Cancel", exact=True).click()
    expect(modal).not_to_be_visible()
    expect(page.get_by_test_id("import-review-lane-ready")).to_contain_text("(0)")
    page.get_by_test_id("import-review-choose-library").click()
    modal.get_by_role("searchbox").fill("Harbor")
    modal.get_by_role("button", name="Search library", exact=True).click()
    selected = modal.locator(f'form:has(input[name="library_series_id"][value="{series_id}"])')
    expect(selected).to_be_visible()
    assert_no_axe_violations(page, name=f"library-series-{namespace}-{theme}")
    screenshots = Path(__file__).resolve().parents[2] / "test-results/library-selection"
    screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshots / f"{browser_name}-{namespace}-{theme}-choose.png"))
    pending_refresh = []

    def observe_pending_refresh(route):
        pending_refresh.append(
            page.evaluate("""() => {
                const modal = document.querySelector('[data-testid="import-library-series-choices"]');
                const button = modal?.querySelector('[data-testid="import-library-series-use"]');
                return { visible: !!modal?.getClientRects().length, disabled: button?.disabled };
            }""")
        )
        route.continue_()

    refresh_url = f"**/import/{job_id}/review-partial*"
    page.route(refresh_url, observe_pending_refresh)
    selected.get_by_role("button", name="Use series", exact=True).click()
    expect(modal).not_to_be_visible()
    page.unroute(refresh_url, observe_pending_refresh)
    assert pending_refresh == [{"visible": True, "disabled": True}]
    expect(page.get_by_test_id("import-review-lane-ready")).to_contain_text("(0)")
    page.get_by_test_id("import-review-primary-action").click()
    expect(page.get_by_test_id("import-review-workspace-table")).to_contain_text(
        "Choose from this series' verified linked issue catalogs."
    )
    page.get_by_test_id("import-review-choose-issue").click()
    choices = page.get_by_test_id("import-review-issue-choices")
    expect(choices).to_contain_text("Confirm its contents")
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/assign-native")
        )
    ) as response:
        choices.get_by_test_id("import-review-use-issue").click()
    assert response.value.ok, response.value.text()
    expect(page.get_by_test_id("import-review-lane-ready")).to_contain_text("(1)")
    assert page.evaluate(
        "window.libraryReviewShell === document.getElementById('import-step-review-shell')"
    )
    page.get_by_test_id("import-review-lane-ready").click()
    expect(page.get_by_test_id("import-review-workspace-table")).to_contain_text(
        f"Library selection ({'GCD' if namespace == 'gcd' else 'Metron'})"
    )
    expect(page.get_by_test_id("import-review-lane-ready")).to_have_attribute(
        "aria-selected", "true"
    )
    page.get_by_test_id("import-review-workspace-table").scroll_into_view_if_needed()
    page.screenshot(path=str(screenshots / f"{browser_name}-{namespace}-{theme}-ready.png"))
    assert not errors
