"""Metron preview/add stays in import review and never selects files implicitly."""

from pathlib import Path

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _run_async_blocking
from tests.e2e.pages.import_page import ImportPage
from tests.e2e.test_source_series_add import preview
from tests.integration.test_metron_folder_import import _archive, _service

pytestmark = pytest.mark.e2e


def browser_identity_offset(browser_name: str) -> int:
    # Combined browser runs share a seeded server; each must start with an unowned ID.
    return ["chromium", "firefox", "webkit"].index(browser_name) * 1_000_000


async def _seed_review(
    root: Path, identifier: str, namespace: str = "metron"
) -> tuple[int, int, int]:
    from pullbox.database import get_session_factory
    from pullbox.models.import_job import ImportedFile, ImportJob, ImportSourceType
    from pullbox.models.library import LibraryRoot

    library = root / "library"
    library.mkdir()
    source = root / "source"
    _archive(
        source / "Unsorted" / "test.cbz",
        series_id=identifier,
        issue_id=identifier,
        primary=namespace,
    )
    async with get_session_factory()() as session:
        destination = LibraryRoot(name="Metron test", path=str(library), allow_managed_writes=True)
        session.add(destination)
        await session.commit()
        job = ImportJob(
            source_type=ImportSourceType.FILESYSTEM,
            source_path=str(source),
            target_library_root_id=destination.id,
        )
        session.add(job)
        await session.commit()
        await _service().start_scan(session, job.id)
        file = await session.scalar(
            select(ImportedFile).where(ImportedFile.import_job_id == job.id)
        )
        await session.commit()
        return job.id, file.import_series_id, destination.id


async def _add_verified_series(identifier: str, namespace: str = "metron") -> int:
    from pullbox.core.metadata_identity import IdentityEvidenceKind, IdentityNamespace
    from pullbox.core.metadata_identity_state import IdentityVerificationState
    from pullbox.database import get_session_factory
    from pullbox.models.issue import Issue
    from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
    from pullbox.models.series import Series

    async with get_session_factory()() as session:
        series = Series(title="Harbor Lights", sort_title="Harbor Lights", year_start=2024)
        session.add(series)
        await session.flush()
        issue = Issue(series_id=series.id, issue_number=3, issue_number_text="3")
        session.add(issue)
        await session.flush()
        evidence = dict(
            identity_namespace=IdentityNamespace(namespace),
            external_id=identifier,
            verification_state=IdentityVerificationState.VERIFIED,
            evidence_kind=IdentityEvidenceKind.PROVIDER_RESULT,
        )
        session.add_all(
            [
                SeriesExternalIdentity(series_id=series.id, **evidence),
                IssueExternalIdentity(issue_id=issue.id, **evidence),
            ]
        )
        await session.commit()
        return series.id


@pytest.mark.parametrize("theme", ["light", "dark", "tron"])
@pytest.mark.parametrize("namespace", ["metron", "gcd"])
def test_metron_preview_cancel_add_and_recheck_stay_in_review(
    authed_page, seeded_server, tmp_path, monkeypatch, theme, namespace, browser_name
):
    page = authed_page
    identifier = str(
        29000001 + browser_identity_offset(browser_name) + ["light", "dark", "tron"].index(theme)
    )
    job_id, group_id, root_id = _run_async_blocking(_seed_review(tmp_path, identifier, namespace))
    source_name = "metron_api" if namespace == "metron" else "gcd_local"
    if namespace == "gcd":
        from pullbox.core.metadata_identity import MetadataSource
        from pullbox.services.metadata_sources import SourceRuntime, default_policy

        async def configured_gcd(_session, **_kwargs):
            policy = default_policy(MetadataSource.GCD_LOCAL).model_copy(update={"enabled": True})
            return [SourceRuntime(policy)]

        monkeypatch.setattr("pullbox.services.metadata_sources.load_source_runtime", configured_gcd)

    async def local_service(_session):
        return _service()

    monkeypatch.setattr("pullbox.tasks.import_task._build_import_service", local_service)
    page.add_init_script(f"localStorage.setItem('pullbox-theme', '{theme}')")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    held = []
    adds = []
    page.route("**/api/v1/metadata/series/preview", lambda route: held.append(route))

    def add(route):
        adds.append(route.request.post_data_json)
        assert route.request.headers.get("x-csrf-token")
        series_id = _run_async_blocking(_add_verified_series(identifier, namespace))
        route.fulfill(status=201, json={"id": series_id, "title": "Harbor Lights"})

    page.route("**/api/v1/series", add)
    page.goto(f"{seeded_server}/import?tab=collection&resume_job_id={job_id}&resume_step=3")
    expect(page.locator("html")).to_have_attribute("data-theme", theme)
    ImportPage(page, seeded_server).click_review_control(
        page.get_by_test_id("import-review-lane-decide")
    )
    page.get_by_role("button", name="Review identity", exact=True).click()
    trigger = page.get_by_test_id(f"import-review-{namespace}-preview")
    expect(trigger).to_be_visible()
    shell = page.locator("#import-step-review-shell")
    shell.evaluate("el => { window.metronReviewShell = el; }")
    trigger.click()
    dialog = page.get_by_test_id("add-series-dialog")
    expect(
        dialog.get_by_role(
            "checkbox", name="Create series.json in the new folder", include_hidden=True
        )
    ).to_have_count(0)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_test_id("add-series-preview-status")).to_be_visible()
    expect(dialog.get_by_role("button", name="Add series", exact=True)).to_be_disabled()
    dialog.get_by_role("button", name="Cancel", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(trigger).to_be_focused()
    assert not adds
    # A cancelled provider response must not reopen the dialog or make it ready.
    for route in held:
        route.fulfill(json=preview(source_name, identifier=identifier))
    held.clear()
    with page.expect_request("**/api/v1/metadata/series/preview"):
        trigger.click()
    expect(dialog.get_by_test_id("add-series-preview-status")).to_be_visible()
    assert len(held) == 1
    assert held[0].request.post_data_json == {
        "source": source_name,
        "external_id": identifier,
        "library_root_id": root_id,
    }
    held[0].fulfill(json=preview(source_name, identifier=identifier))
    expect(dialog.get_by_role("button", name="Add series", exact=True)).to_be_enabled()
    expect(dialog).to_contain_text("will not remove the added series")
    assert_no_axe_violations(page, name=f"{namespace}-import-preview-{theme}")
    box = dialog.bounding_box()
    assert box and box["y"] >= 0 and box["y"] + box["height"] <= page.viewport_size["height"]
    dialog.get_by_role("button", name="Add series", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_test_id("import-review-lane-ready")).to_contain_text("(1)", timeout=15000)
    assert adds == [
        {
            "source": source_name,
            "external_id": identifier,
            "source_revision": 7,
            "library_root_id": root_id,
        }
    ]
    assert page.evaluate(
        "window.metronReviewShell === document.getElementById('import-step-review-shell')"
    )
    assert f"resume_job_id={job_id}" in page.url and "resume_step=3" in page.url
    page.get_by_test_id("import-review-lane-ready").click()
    expect(page.get_by_test_id("import-review-workspace-table")).to_contain_text("1 ready")
    checkbox = page.locator(f'[data-import-review-duplicate-selectable="{group_id}"]')
    expect(checkbox).not_to_be_checked()
    assert not errors
    page.screenshot(path=str(Path("test-results") / f"{namespace}-import-{theme}.png"))


@pytest.mark.parametrize("theme", ["light", "dark", "tron"])
def test_gcd_recheck_uses_same_review_surface_without_metron_or_cv_controls(
    authed_page, seeded_server, tmp_path, monkeypatch, theme, browser_name
):
    page = authed_page
    identifier = str(
        39000001 + browser_identity_offset(browser_name) + ["light", "dark", "tron"].index(theme)
    )
    job_id, group_id, _ = _run_async_blocking(_seed_review(tmp_path, identifier, "gcd"))

    async def local_service(_session):
        return _service()

    monkeypatch.setattr("pullbox.tasks.import_task._build_import_service", local_service)
    page.add_init_script(f"localStorage.setItem('pullbox-theme', '{theme}')")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{seeded_server}/import?tab=collection&resume_job_id={job_id}&resume_step=3")
    expect(page.locator("html")).to_have_attribute("data-theme", theme)
    page.get_by_test_id("import-review-lane-decide").click()
    page.get_by_role("button", name="Review identity", exact=True).click()
    workspace = page.get_by_test_id("import-review-workspace-table")
    expect(workspace).to_contain_text("GCD identity needs review")
    expect(workspace.get_by_role("button", name="Preview GCD series")).to_be_visible()
    expect(page.get_by_test_id("import-review-metron-preview")).to_have_count(0)
    expect(workspace).not_to_contain_text("Search ComicVine")
    # Correcting the local link is independent of the import; recheck only reads it.
    _run_async_blocking(_add_verified_series(identifier, "gcd"))
    page.get_by_role("button", name="Recheck linked metadata", exact=True).click()
    expect(page.get_by_test_id("import-review-lane-ready")).to_contain_text("(1)", timeout=15000)
    page.get_by_test_id("import-review-lane-ready").click()
    expect(workspace).to_contain_text("GCD identity")
    expect(workspace).not_to_contain_text("Metron identity")
    expect(
        page.locator(f'[data-import-review-duplicate-selectable="{group_id}"]')
    ).not_to_be_checked()
    # Check settled tab colors, not an interpolated frame during lane selection.
    page.wait_for_function(
        """() => [...document.querySelectorAll('[id^="import-review-tab-"]')]
            .every(tab => tab.getAnimations({subtree: true})
                .every(animation => animation.playState === 'finished'))"""
    )
    assert_no_axe_violations(page, name=f"gcd-import-review-{theme}")
    assert not errors
