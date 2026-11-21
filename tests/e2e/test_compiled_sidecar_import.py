"""Compiled sidecars reuse the stable native issue-review surface."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _run_async_blocking
from tests.e2e.pages.import_page import ImportPage
from tests.integration.test_compiled_sidecar_import import _comic, _sidecar
from tests.integration.test_metron_folder_import import _service, _setup
from tests.integration.test_mylar_compiled_sidecar import sidecar_mylar_fixture

pytestmark = pytest.mark.e2e


async def _seed(root, namespace, theme, source_type):
    from pullbox.core.metadata_identity import IdentityNamespace
    from pullbox.database import get_session_factory
    from pullbox.models.import_job import ImportJob, ImportSourceType

    base = (
        60000000
        + ["light", "dark", "tron"].index(theme) * 10000
        + ["metron", "gcd"].index(namespace) * 1000
        + (1000000 if source_type == "mylar3" else 0)
    )
    native = IdentityNamespace(namespace)
    async with get_session_factory()() as session:
        if source_type == "mylar3":
            job, service, *_ = await sidecar_mylar_fixture(
                session, root, namespace, tagged=False, identity_base=base, admit=False
            )
            await service.start_scan(session, job.id)
            return job.id
        destination, _, _ = await _setup(
            session,
            root,
            namespace=native,
            number="13a",
            series_external_id=str(base + 21),
            issue_external_id=str(base + 101),
            default_destination=False,
        )
        folder = root / "source" / "Unsorted"
        _sidecar(folder, native, str(base + 21))
        comic = _comic(folder, native, tagged=False)
        (folder / "neighbor.cbz").write_bytes(comic.read_bytes())
        job = ImportJob(
            source_path=str(folder.parent),
            source_type=ImportSourceType.FILESYSTEM,
            target_library_root_id=destination.id,
        )
        session.add(job)
        await session.commit()
        await _service().start_scan(session, job.id)
        return job.id


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("theme", ["light", "dark", "tron"])
@pytest.mark.parametrize("source_type", ["filesystem", "mylar3"])
def test_sidecar_issue_choice_preserves_neighbors_and_review_shell(
    authed_page, seeded_server, tmp_path, namespace, theme, browser_name, source_type
):
    page = authed_page
    job_id = _run_async_blocking(_seed(tmp_path, namespace, theme, source_type))
    page.add_init_script(f"localStorage.setItem('pullbox-theme', '{theme}')")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{seeded_server}/import?tab=collection&resume_job_id={job_id}&resume_step=3")
    review = ImportPage(page, seeded_server)
    review.click_review_control(page.get_by_test_id("import-review-lane-decide"))
    page.locator(
        '[data-testid="import-review-workspace-table"] button[aria-expanded="false"]'
    ).first.click()
    expect(page.get_by_test_id("import-review-choose-issue")).to_have_count(2)
    page.locator("#import-step-review-shell").evaluate("el => window.sidecarReviewShell = el")
    page.get_by_test_id("import-review-choose-issue").first.click()
    choices = page.get_by_test_id("import-review-issue-choices")
    expect(choices).to_contain_text("series.json identifies the series, not this individual issue")
    expect(choices.get_by_test_id("import-review-use-issue")).to_be_enabled()
    assert_no_axe_violations(page, name=f"sidecar-choices-{source_type}-{namespace}-{theme}")
    screenshots = Path(__file__).resolve().parents[2] / "test-results/sidecar-import"
    screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(screenshots / f"{browser_name}-{source_type}-{namespace}-{theme}-choices.png")
    )
    choices.get_by_test_id("import-review-use-issue").click()
    expect(page.get_by_test_id("import-review-choose-issue")).to_have_count(1)
    ready_count = 2 if source_type == "mylar3" else 1
    expect(page.get_by_test_id("import-review-lane-ready")).to_contain_text(f"({ready_count})")
    assert page.locator("#import-step-review-shell").evaluate(
        "el => el === window.sidecarReviewShell"
    )
    review.click_review_control(page.get_by_test_id("import-review-lane-ready"))
    ready_tab = page.get_by_test_id("import-review-lane-ready")
    expect(ready_tab).to_have_attribute("aria-selected", "true")
    assert ready_tab.evaluate("el => getComputedStyle(el).transitionDuration") == "0s"
    assert ready_tab.evaluate(
        "el => getComputedStyle(el).color === getComputedStyle(el.querySelector('span')).color"
    )
    label = "Metron" if namespace == "metron" else "GCD"
    identity = page.get_by_test_id("import-review-workspace-table").get_by_text(
        f"series.json ({label})"
    )
    identity.scroll_into_view_if_needed()
    expect(identity).to_be_visible()
    expect(
        page.get_by_role("checkbox", name="Import ready files for Harbor Lights", exact=True)
    ).to_be_checked()
    if source_type == "mylar3":
        expect(
            page.get_by_role("checkbox", name="Import ready files for Old Harbor", exact=True)
        ).not_to_be_checked()
    assert_no_axe_violations(page, name=f"sidecar-ready-{source_type}-{namespace}-{theme}")
    page.screenshot(
        path=str(screenshots / f"{browser_name}-{source_type}-{namespace}-{theme}-ready.png")
    )
    assert not errors
