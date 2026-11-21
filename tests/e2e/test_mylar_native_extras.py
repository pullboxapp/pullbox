"""A completed Mylar import offers real native issue recovery without rescanning."""

from pathlib import Path

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from pullbox.models.import_job import ImportedFile, ImportedFileStatus
from pullbox.models.library import LibraryFile
from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _run_async_blocking
from tests.ui.test_mylar_native_extras import completed_mylar_extra

pytestmark = pytest.mark.e2e


async def seed(root, namespace, theme, browser_name):
    from pullbox.database import get_session_factory

    base = (
        85000000
        + ["chromium", "firefox", "webkit"].index(browser_name) * 100000
        + ["light", "dark", "tron"].index(theme) * 10000
        + ["metron", "gcd"].index(namespace) * 1000
    )
    async with get_session_factory()() as session:
        return await completed_mylar_extra(
            session, root, namespace, identity_base=base, admit=False
        )


async def assert_state(file_id, issue_id, originals, imported=False):
    from pullbox.database import get_session_factory

    async with get_session_factory()() as session:
        file = await session.get(ImportedFile, file_id)
        assert file.status == (
            ImportedFileStatus.IMPORTED if imported else ImportedFileStatus.NO_MATCH
        )
        library = await session.scalar(
            select(LibraryFile.id).where(LibraryFile.issue_id == issue_id)
        )
        assert (library is not None) == imported
    assert all(path.read_bytes() == content for path, content in originals.items())


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("theme", ["light", "dark", "tron"])
def test_mylar_extra_can_be_recovered_after_importing_recorded_files(
    authed_page, seeded_server, tmp_path, monkeypatch, namespace, theme, browser_name
):
    job_id, file_id, parent_id, issue_id, _series_id, service, originals = _run_async_blocking(
        seed(tmp_path, namespace, theme, browser_name)
    )

    async def build_service(session):
        return service

    monkeypatch.setattr("pullbox.composition.services.build_import_service", build_service)
    page = authed_page
    page.add_init_script(f"localStorage.setItem('pullbox-theme', '{theme}')")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{seeded_server}/import?tab=follow-up&job_id={job_id}")
    row = page.get_by_test_id(f"import-orphaned-row-{parent_id}")
    expect(row).to_contain_text("Needs an issue match")
    expect(row).to_contain_text("Harbor Lights")
    page.locator("#cv-search-modal").evaluate("el => window.mylarModalHost = el")
    row.get_by_test_id(f"import-orphaned-recover-{parent_id}").click()
    modal = page.get_by_test_id("import-orphaned-recovery-modal")
    expect(modal).to_be_visible()
    modal.get_by_role("button", name="Close", exact=True).click()
    expect(modal).not_to_be_visible()
    _run_async_blocking(assert_state(file_id, issue_id, originals))
    row.get_by_test_id(f"import-orphaned-recover-{parent_id}").click()
    modal.get_by_test_id(f"orphaned-recovery-issue-trigger-{file_id}").click()
    expect(modal.get_by_test_id("import-review-use-issue")).to_have_count(1)
    expect(modal.get_by_test_id("import-review-use-issue")).to_be_enabled()
    assert_no_axe_violations(
        page,
        name=f"mylar-native-{namespace}-{theme}",
        include=['[data-testid="import-orphaned-recovery-modal"]'],
    )
    if theme == "light":
        page.set_viewport_size({"width": 390, "height": 844})
        expect(modal.get_by_test_id("orphaned-recovery-save")).to_be_in_viewport(ratio=1)
    screenshots = Path(__file__).resolve().parents[2] / "test-results/mylar-native-extras"
    screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshots / f"{browser_name}-{namespace}-{theme}.png"))
    modal.get_by_test_id("import-review-use-issue").click()
    modal.get_by_test_id("orphaned-recovery-save").click()
    expect(modal).not_to_be_visible(timeout=20000)
    expect(row).not_to_be_visible()
    _run_async_blocking(assert_state(file_id, issue_id, originals, imported=True))
    assert page.evaluate("window.mylarModalHost === document.getElementById('cv-search-modal')")
    assert service._metadata_service.mock_calls == []
    assert not errors
