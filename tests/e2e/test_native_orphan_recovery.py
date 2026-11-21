"""Follow-up's native issue picker keeps choices local until recovery is confirmed."""

from pathlib import Path

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from pullbox.core.metadata_identity import IdentityEvidenceKind, IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.import_job import ImportedFile, ImportedFileStatus
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile
from pullbox.models.metadata_identity import IssueExternalIdentity
from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _run_async_blocking
from tests.integration.test_native_orphan_recovery import _follow_up

pytestmark = pytest.mark.e2e


async def _seed(root, namespace, theme, browser_name):
    from pullbox.database import get_session_factory

    async with get_session_factory()() as session:
        base = (
            72000000
            + ["chromium", "firefox", "webkit"].index(browser_name) * 100000
            + ["light", "dark", "tron"].index(theme) * 10000
            + ["metron", "gcd"].index(namespace) * 1000
        )
        service, job, parent, file, series, issue, comic = await _follow_up(
            session, root, IdentityNamespace(namespace), identity_base=base, admit=False
        )
        for number in range(1, 31):
            if number == 13:
                continue
            extra = Issue(series_id=series.id, issue_number=number, issue_number_text=str(number))
            session.add(extra)
            await session.flush()
            session.add(
                IssueExternalIdentity(
                    issue_id=extra.id,
                    identity_namespace=IdentityNamespace(namespace),
                    external_id=str(base + 200 + number),
                    verification_state=IdentityVerificationState.VERIFIED,
                    evidence_kind=IdentityEvidenceKind.PROVIDER_RESULT,
                )
            )
        await session.commit()
        return service, job.id, parent.id, file.id, issue.id, comic, comic.read_bytes()


async def _assert_saved(file_id, issue_id, comic, original, imported=False):
    from pullbox.database import get_session_factory

    async with get_session_factory()() as session:
        file = await session.get(ImportedFile, file_id)
        assert file.status == (
            ImportedFileStatus.IMPORTED if imported else ImportedFileStatus.NO_MATCH
        )
        library = await session.scalar(select(LibraryFile).where(LibraryFile.issue_id == issue_id))
        assert (library is not None) == imported
        assert comic.read_bytes() == original


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("theme", ["light", "dark", "tron"])
def test_native_manual_follow_up_choices(
    authed_page, seeded_server, tmp_path, monkeypatch, namespace, theme, browser_name
):
    service, job_id, parent_id, file_id, issue_id, comic, original = _run_async_blocking(
        _seed(tmp_path, namespace, theme, browser_name)
    )

    async def build_service(session):
        return service

    monkeypatch.setattr("pullbox.composition.services.build_import_service", build_service)
    page = authed_page
    page.add_init_script(f"localStorage.setItem('pullbox-theme', '{theme}')")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{seeded_server}/import?tab=follow-up&job_id={job_id}")
    page.get_by_test_id(f"import-orphaned-recover-{parent_id}").click()
    modal = page.get_by_test_id("import-orphaned-recovery-modal")
    expect(modal).to_be_visible()
    if namespace == "metron" and theme == "light":
        page.route(
            "**/assign-native?follow_up=true", lambda route: route.fulfill(status=503), times=1
        )
        modal.get_by_test_id(f"orphaned-recovery-issue-trigger-{file_id}").click()
        expect(modal.get_by_role("alert")).to_contain_text("Could not load verified issues")
        modal.get_by_role("button", name="Retry issue choices", exact=True).click()
    else:
        modal.get_by_test_id(f"orphaned-recovery-issue-trigger-{file_id}").click()
    choices = modal.get_by_test_id("import-review-issue-choices")
    expect(choices).to_be_visible()
    expect(choices.get_by_test_id("import-review-use-issue")).to_have_count(25)
    expect(modal.get_by_test_id("orphaned-recovery-save")).to_be_in_viewport(ratio=1)
    choices.get_by_role("button", name="Next", exact=True).click()
    expect(choices.get_by_test_id("import-review-use-issue")).to_have_count(5)
    choices.get_by_role("button", name="Previous", exact=True).click()
    expect(choices.get_by_test_id("import-review-use-issue")).to_have_count(25)
    modal.get_by_test_id(f"orphaned-recovery-issue-trigger-{file_id}").click()
    expect(choices).not_to_be_visible()
    modal.get_by_test_id(f"orphaned-recovery-issue-trigger-{file_id}").click()
    expect(choices).to_be_visible()
    choices.get_by_role("searchbox").fill("13a")
    choices.get_by_role("button", name="Find issue", exact=True).click()
    expect(choices.get_by_test_id("import-review-use-issue")).to_have_count(1)
    assert_no_axe_violations(
        page,
        name=f"native-manual-follow-up-{namespace}-{theme}",
        include=['[data-testid="import-orphaned-recovery-modal"]'],
    )
    screenshots = Path(__file__).resolve().parents[2] / "test-results/native-follow-up"
    screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshots / f"{browser_name}-{namespace}-{theme}.png"))
    choices.get_by_role("button", name="Use this", exact=True).click()
    expect(modal.get_by_test_id(f"orphaned-recovery-issue-trigger-{file_id}")).to_contain_text(
        "#13A"
    )
    _run_async_blocking(_assert_saved(file_id, issue_id, comic, original))
    if namespace == "metron" and theme == "light":
        page.set_viewport_size({"width": 390, "height": 844})
        expect(modal.get_by_test_id("orphaned-recovery-save")).to_be_in_viewport(ratio=1)
        expect(modal.get_by_role("dialog")).to_be_in_viewport(ratio=1)
        page.screenshot(path=str(screenshots / f"{browser_name}-mobile.png"))
    modal.get_by_test_id("orphaned-recovery-save").click()
    expect(modal).not_to_be_visible(timeout=20000)
    _run_async_blocking(_assert_saved(file_id, issue_id, comic, original, imported=True))
    expect(page.get_by_test_id(f"import-orphaned-recover-{parent_id}")).not_to_be_visible()
    assert not errors
    assert service._metadata_service.mock_calls == []
