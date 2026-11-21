"""Recorded Mylar identity disagreements are actionable without replacing the shell."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _run_async_blocking
from tests.e2e.pages.import_page import ImportPage
from tests.integration.test_metron_folder_import import _archive
from tests.integration.test_mylar_native_extras import seed_mylar_native_extra

pytestmark = pytest.mark.e2e


async def _seed(root, namespace, theme, action):
    from pullbox.database import get_session_factory

    base = (
        80000000
        + ["light", "dark", "system"].index(theme) * 10000
        + ["metron", "gcd"].index(namespace) * 1000
        + (1000000 if action == "skip" else 0)
    )
    async with get_session_factory()() as session:
        job, service, *_, extra = await seed_mylar_native_extra(
            session, root, namespace, identity_base=base, mapped_source=True, admit=False
        )
        tracked = extra.parent / "Old Harbor 001.cbz"
        _archive(
            tracked,
            primary=namespace,
            number="1",
            series_id=str(base + 21),
            issue_id=str(base + 101),
        )
        await service.start_scan(session, job.id)
        return job.id, tracked, base


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("theme", ["light", "dark", "system"])
@pytest.mark.parametrize("action", ["recheck", "skip"])
def test_recorded_mylar_conflict_recheck_or_skip_preserves_review_shell(
    authed_page, seeded_server, tmp_path, namespace, theme, action, browser_name
):
    page = authed_page
    job_id, tracked, base = _run_async_blocking(_seed(tmp_path, namespace, theme, action))
    if theme == "system":
        page.emulate_media(color_scheme="light")
        page.add_init_script("localStorage.removeItem('pullbox-theme')")
    else:
        page.add_init_script(f"localStorage.setItem('pullbox-theme', '{theme}')")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{seeded_server}/import?tab=collection&resume_job_id={job_id}&resume_step=3")
    expect(page.locator("html")).to_have_attribute(
        "data-theme", "light" if theme == "system" else theme
    )
    review = ImportPage(page, seeded_server)
    review.click_review_control(page.get_by_test_id("import-review-lane-decide"))
    page.locator(
        '[data-testid="import-review-workspace-table"] button[aria-expanded="false"]'
    ).first.click()
    conflict = page.get_by_test_id("import-review-mylar-native-conflict")
    expect(conflict).to_be_visible()
    expect(conflict.get_by_role("link", name="Review linked metadata", exact=True)).to_be_visible()
    page.locator("#import-step-review-shell").evaluate("el => window.mylarReviewShell = el")
    assert_no_axe_violations(page, name=f"mylar-native-{namespace}-{theme}-{action}")
    screenshots = Path(__file__).resolve().parents[2] / "test-results/mylar-native-conflict"
    screenshots.mkdir(parents=True, exist_ok=True)
    conflict.scroll_into_view_if_needed()
    page.screenshot(path=str(screenshots / f"{browser_name}-{namespace}-{theme}-{action}.png"))
    if action == "recheck":
        _archive(
            tracked,
            primary=namespace,
            number="1",
            series_id=str(base + 22),
            issue_id=str(base + 102),
        )
        conflict.get_by_role("button", name="Recheck source", exact=True).click()
        dialog = page.get_by_role("dialog", name="Recheck recorded path")
        expect(dialog).to_be_visible()
        assert_no_axe_violations(page, name=f"mylar-native-modal-{namespace}-{theme}")
        dialog.get_by_role("button", name="Recheck recorded path", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(page.get_by_test_id("import-review-lane-ready")).to_contain_text(
            "(2)", timeout=20000
        )
    else:
        conflict.get_by_role("button", name="Skip this file", exact=True).click()
    expect(conflict).to_have_count(0, timeout=20000)
    assert page.locator("#import-step-review-shell").evaluate(
        "el => el === window.mylarReviewShell"
    )
    assert not errors
