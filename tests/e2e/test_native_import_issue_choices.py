"""Saved native issue selection keeps the review shell, focus, and pagination."""

import pytest
from playwright.sync_api import expect

from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _run_async_blocking
from tests.e2e.pages.import_page import ImportPage
from tests.ui.test_native_import_issue_assignment import seed_native_choices

pytestmark = pytest.mark.e2e


async def _seed(root, namespace, theme):
    from pullbox.database import get_session_factory

    async with get_session_factory()() as session:
        identity_base = (
            9100000
            + ["light", "dark", "tron"].index(theme) * 10000
            + (["metron", "gcd"].index(namespace) * 1000)
        )
        job_id, file_id, _, _, _, _ = await seed_native_choices(
            session, root, namespace, identity_base, admit=False
        )
        return job_id, file_id


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("theme", ["light", "dark", "tron"])
def test_native_issue_picker_selection_preserves_review(
    authed_page, seeded_server, tmp_path, namespace, theme
):
    page = authed_page
    job_id, _file_id = _run_async_blocking(_seed(tmp_path, namespace, theme))
    page.add_init_script(f"localStorage.setItem('pullbox-theme', '{theme}')")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{seeded_server}/import?tab=collection&resume_job_id={job_id}&resume_step=3")
    ImportPage(page, seeded_server).click_review_control(
        page.get_by_test_id("import-review-lane-decide")
    )
    page.locator(
        '[data-testid="import-review-workspace-table"] button[aria-expanded="false"]'
    ).first.click()
    trigger = page.get_by_test_id("import-review-choose-issue")
    expect(trigger).to_be_visible()
    page.locator("#import-step-review-shell").evaluate("el => window.nativeReviewShell = el")
    trigger.click()
    choices = page.get_by_test_id("import-review-issue-choices")
    expect(choices).to_be_visible()
    expect(choices.get_by_test_id("import-review-use-issue")).to_have_count(25)
    trigger.click()
    expect(choices).to_be_hidden()
    trigger.click()
    expect(choices).to_be_visible()
    choices.get_by_role("button", name="Next", exact=True).click()
    expect(choices.get_by_test_id("import-review-use-issue")).to_have_count(7)
    choices.get_by_role("searchbox").fill("13a")
    choices.get_by_role("button", name="Find issue", exact=True).click()
    expect(choices.get_by_test_id("import-review-use-issue")).to_have_count(1)
    expect(choices.get_by_test_id("import-review-use-issue")).to_be_enabled()
    assert_no_axe_violations(page, name=f"native-issue-picker-{namespace}-{theme}")
    choices.get_by_test_id("import-review-use-issue").click()
    expect(page.get_by_test_id("import-review-lane-ready")).to_contain_text("(1)")
    assert page.evaluate(
        "window.nativeReviewShell === document.getElementById('import-step-review-shell')"
    )
    assert f"resume_job_id={job_id}" in page.url
    assert not errors
