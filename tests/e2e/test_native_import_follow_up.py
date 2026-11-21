"""The existing Follow-up action resumes native exact matches through Step 4."""

from datetime import UTC, datetime

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportJob,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.models.library import LibraryFile
from pullbox.models.metadata_identity import SeriesExternalIdentity
from pullbox.services.import_deferred_recovery_execution import prepare_deferred_recovery
from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _run_async_blocking
from tests.integration.test_metron_folder_import import _archive, _service, _setup

pytestmark = pytest.mark.e2e


async def _seed(root, namespace, theme):
    from pullbox.core.library_policy import load_library_ingest_policy
    from pullbox.database import get_session_factory
    from pullbox.services.import_policy_snapshot import apply_ingest_policy_to_import_job

    base = (
        71000000
        + ["light", "dark", "tron"].index(theme) * 10000
        + ["metron", "gcd"].index(namespace) * 1000
    )
    async with get_session_factory()() as session:
        destination, series, issue = await _setup(
            session,
            root,
            namespace=IdentityNamespace(namespace),
            number="13a",
            series_external_id=str(base + 21),
            issue_external_id=str(base + 101),
            default_destination=False,
        )
        link = await session.scalar(
            select(SeriesExternalIdentity).where(SeriesExternalIdentity.series_id == series.id)
        )
        link.verification_state = IdentityVerificationState.STALE
        await session.commit()
        comic = root / "source" / "Unsorted" / "Synthetic deferred native.cbz"
        original = _archive(
            comic,
            number="13a",
            series_id=str(base + 21),
            issue_id=str(base + 101),
            primary="Metron" if namespace == "metron" else "GCD",
        )
        service = _service()
        job = ImportJob(
            source_path=str(comic.parent.parent),
            source_type=ImportSourceType.FILESYSTEM,
            target_library_root_id=destination.id,
        )
        apply_ingest_policy_to_import_job(job, await load_library_ingest_policy(session))
        session.add(job)
        await session.commit()
        await service.start_scan(session, job.id)
        job.status = ImportJobStatus.COMPLETED
        job.import_completed_at = datetime.now(UTC)
        link.verification_state = IdentityVerificationState.VERIFIED
        await session.commit()
        return job.id, issue.id, comic, original


async def _execute_and_check(job_id, issue_id, comic, original):
    from pullbox.database import get_session_factory

    async with get_session_factory()() as session:
        job = await session.get(ImportJob, job_id)
        assert job.status == ImportJobStatus.IMPORTING
        service = _service()
        assert await prepare_deferred_recovery(
            session, job_id, metadata_service=service._metadata_service
        )
        await service.run_import(session, job_id)
        await session.commit()
        await session.refresh(job)
        assert job.status == ImportJobStatus.COMPLETED
        file = await session.scalar(
            select(ImportedFile).where(ImportedFile.import_job_id == job_id)
        )
        assert file.status == ImportedFileStatus.IMPORTED
        assert (
            await session.scalar(select(LibraryFile).where(LibraryFile.issue_id == issue_id))
        ) is not None
        assert job.total_files_imported == 1 and job.series_imported == 0
        assert comic.read_bytes() == original
        assert service._metadata_service.mock_calls == []


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("theme", ["light", "dark", "tron"])
def test_native_follow_up_uses_confirmed_existing_recheck_action(
    authed_page, seeded_server, tmp_path, monkeypatch, namespace, theme
):
    from pullbox.api.v1 import import_completed_cleanup

    job_id, issue_id, comic, original = _run_async_blocking(_seed(tmp_path, namespace, theme))
    queued = []
    # Observe the real authenticated enqueue, then run its services deterministically.
    monkeypatch.setattr(import_completed_cleanup, "trigger_import_execute", queued.append)
    page = authed_page
    page.add_init_script(f"localStorage.setItem('pullbox-theme', '{theme}')")
    page.goto(f"{seeded_server}/import?tab=follow-up&job_id={job_id}")
    action = page.get_by_test_id("import-cleanup-apply-recheck_deferred_files")
    expect(action).to_be_visible()
    action.click()
    dialog = page.get_by_role("dialog").filter(has_text="Recheck deferred files?")
    expect(dialog).to_contain_text("Source files will remain unchanged")
    expect(dialog.locator(".fixed")).to_have_css("opacity", "1")
    expect(dialog.locator(".relative")).to_have_css("opacity", "1")
    assert_no_axe_violations(
        page,
        name=f"native-follow-up-{namespace}-{theme}",
        include=['[role="dialog"]'],
    )
    dialog.get_by_role("button", name="Cancel", exact=True).click()
    assert queued == []
    action.click()
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and f"/{job_id}/cleanup/recheck_deferred_files" in response.url
        )
    ) as response:
        dialog.get_by_role("button", name="Recheck files", exact=True).click()
    assert response.value.status == 200, response.value.text()
    assert queued == [job_id]
    _run_async_blocking(_execute_and_check(job_id, issue_id, comic, original))
    page.goto(f"{seeded_server}/import?tab=collection&resume_job_id={job_id}&resume_step=4")
    expect(page.get_by_test_id("import-progress-title")).to_contain_text("Import complete")
    progress = page.get_by_test_id("import-collection-progress")
    expect(progress.locator('[x-text="stats.total_files_imported || 0"]')).to_have_text("1")
    expect(progress.locator('[x-text="stats.series_imported || 0"]')).to_have_text("0")
