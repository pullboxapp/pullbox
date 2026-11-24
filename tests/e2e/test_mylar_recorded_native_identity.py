"""Recorded Mylar identity disagreements are actionable without replacing the shell."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _run_async_blocking
from tests.e2e.pages.import_page import ImportPage
from tests.integration.test_metron_folder_import import _archive
from tests.integration.test_mylar_first_series_followup import import_first_mylar_series
from tests.integration.test_mylar_native_extras import seed_mylar_native_extra
from tests.integration.test_mylar_recorded_native_identity import run_late_mylar_conflict

pytestmark = pytest.mark.e2e


def test_new_mylar_series_can_skip_conflicts_then_remaining_unidentified_file(
    authed_page, seeded_server, tmp_path, browser_name
):
    from pullbox.database import get_session_factory

    base = 85000000 + (10000 if browser_name == "firefox" else 0)
    title = "Winter Orchard" if browser_name == "firefox" else "Lantern Watch"

    async def seed():
        async with get_session_factory()() as session:
            _, job, group, files, _, originals = await import_first_mylar_series(
                session, tmp_path, "metron", False, "unmatched", base=base, admit=False, title=title
            )
            return job.id, group.id, {k: v.id for k, v in files.items()}, originals

    job, group, files, originals = _run_async_blocking(seed())
    page = authed_page
    page.goto(f"{seeded_server}/import?tab=follow-up&job_id={job}")
    row = page.get_by_test_id(f"import-orphaned-row-{group}")
    row.get_by_test_id(f"import-orphaned-recover-{group}").click()
    dialog = page.get_by_test_id("import-orphaned-recovery-modal")
    other = files["Unknown.cbz"]
    expect(dialog.get_by_test_id(f"orphaned-recovery-file-{other}")).to_have_count(0)
    for name in (f"{title} 001.cbz", f"{title} 002.cbz"):
        dialog.get_by_test_id(f"orphaned-recovery-skip-{files[name]}").click()
    dialog.get_by_test_id("orphaned-recovery-save").click()
    dialog.get_by_test_id(f"orphaned-recovery-skip-{other}").click()
    dialog.get_by_test_id("orphaned-recovery-save").click()
    expect(row).to_have_count(0)
    page.reload()
    expect(page.get_by_test_id("import-follow-up-job-panel")).to_be_visible()
    expect(row).to_have_count(0)
    assert all(path.read_bytes() == content for path, content in originals.items())


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
def test_changed_link_after_confirm_can_be_reviewed_and_skipped(
    authed_page, seeded_server, tmp_path, namespace, browser_name
):
    from pullbox.database import get_session_factory

    base = (
        84000000 + (10000 if browser_name == "firefox" else 0) + (1000 if namespace == "gcd" else 0)
    )

    async def seed():
        async with get_session_factory()() as session:
            job, _, file, native, recorded, originals = await run_late_mylar_conflict(
                session, tmp_path, namespace, False, identity_base=base, admit=False
            )
            return job.id, file.import_series_id, file.id, native.id, recorded.id, originals

    job, group, file, native, recorded, originals = _run_async_blocking(seed())
    page = authed_page
    page.goto(f"{seeded_server}/import?tab=follow-up&job_id={job}")
    row = page.get_by_test_id(f"import-orphaned-row-{group}")
    row.get_by_test_id(f"import-orphaned-recover-{group}").click()
    dialog = page.get_by_test_id("import-orphaned-recovery-modal")
    disputed = dialog.get_by_test_id(f"orphaned-recovery-file-{file}")
    expect(disputed.get_by_role("link", name="Open recorded issue")).to_have_attribute(
        "href", f"/issues/{recorded}"
    )
    label = "Metron" if namespace == "metron" else "GCD"
    expect(disputed.get_by_role("link", name=f"Open {label} issue")).to_have_attribute(
        "href", f"/issues/{native}"
    )
    disputed.get_by_test_id(f"orphaned-recovery-skip-{file}").click()
    dialog.get_by_test_id("orphaned-recovery-save").click()
    expect(row).to_have_count(0)
    page.reload()
    expect(page.get_by_test_id("import-follow-up-job-panel")).to_be_visible()
    expect(row).to_have_count(0)
    assert all(path.read_bytes() == content for path, content in originals.items())


async def _seed(root, namespace, theme, action, mapped_source):
    from pullbox.database import get_session_factory

    base = (
        80000000
        + ["light", "dark", "system"].index(theme) * 10000
        + ["metron", "gcd"].index(namespace) * 1000
        + (1000000 if action == "skip" else 0)
        + (2000000 if not mapped_source else 0)
    )
    async with get_session_factory()() as session:
        job, service, *_, extra = await seed_mylar_native_extra(
            session, root, namespace, identity_base=base, mapped_source=mapped_source, admit=False
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
@pytest.mark.parametrize("mapped_source", [True, False])
def test_recorded_mylar_conflict_recheck_or_skip_preserves_review_shell(
    authed_page, seeded_server, tmp_path, namespace, theme, action, browser_name, mapped_source
):
    page = authed_page
    job_id, tracked, base = _run_async_blocking(
        _seed(tmp_path, namespace, theme, action, mapped_source)
    )
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
    page.screenshot(
        path=str(screenshots / f"{browser_name}-{namespace}-{theme}-{action}-{mapped_source}.png")
    )
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
