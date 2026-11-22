"""Drive migrated native-provider follow-up and rollback through the packaged web app."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullbox.core.library_policy import load_library_ingest_policy
from pullbox.core.metadata_identity import IdentityEvidenceKind, IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState as State
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportJob,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.services.import_policy_snapshot import apply_ingest_policy_to_import_job


def make_sources(imports):
    from tests.integration.test_metron_folder_import import _archive

    originals = {}
    for ns, label in [("metron", "Metron"), ("gcd", "GCD")]:
        folder = imports / ns / "Unsorted"
        comic = folder / "Deferred.cbz"
        unknown = folder / "Harbor Lights 014 (2024).cbz"
        originals[ns] = (
            comic,
            _archive(comic, number="13a", primary=label),
            unknown,
            _archive(unknown, number="14", issue_id="999999", primary=label),
        )
    return originals


async def scan():
    """Perform a real SQLite scan while a link is stale; verify it before the transfer."""
    from pullbox.composition.services import build_import_service

    engine = create_async_engine("sqlite+aiosqlite:////data/pullbox.db")
    cases = []
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            issue = await session.get(Issue, 10012)
            issue.issue_number_text = "13a"
            for ns in (IdentityNamespace.METRON, IdentityNamespace.GCD):
                identity = dict(
                    identity_namespace=ns, evidence_kind=IdentityEvidenceKind.PROVIDER_RESULT
                )
                link = SeriesExternalIdentity(
                    series_id=issue.series_id,
                    external_id="21",
                    verification_state=State.STALE,
                    **identity,
                )
                session.add_all(
                    [
                        link,
                        IssueExternalIdentity(
                            issue_id=issue.id,
                            external_id="101",
                            verification_state=State.VERIFIED,
                            **identity,
                        ),
                    ]
                )
                folder = Path("/imports") / ns.value
                job = ImportJob(
                    source_path=str(folder),
                    source_type=ImportSourceType.FILESYSTEM,
                    target_library_root_id=77,
                    source_layout_snapshot={"mode": "auto"},
                )
                apply_ingest_policy_to_import_job(job, await load_library_ingest_policy(session))
                session.add(job)
                await session.commit()
                service = await build_import_service(session)
                await service.start_scan(session, job.id)
                rows = (
                    await session.scalars(
                        select(ImportedFile).where(ImportedFile.import_job_id == job.id)
                    )
                ).all()
                assert len(rows) == 2 and all(
                    row.status == ImportedFileStatus.NO_MATCH for row in rows
                )
                job.status = ImportJobStatus.COMPLETED
                job.import_completed_at = datetime.now(UTC)
                link.verification_state = State.VERIFIED
                await session.commit()
                by_name = {row.file_name: row.id for row in rows}
                cases.append(
                    (
                        ns.value,
                        job.id,
                        issue.id,
                        by_name["Deferred.cbz"],
                        by_name["Harbor Lights 014 (2024).cbz"],
                    )
                )
    finally:
        await engine.dispose()
    return cases


async def exercise(origin, cases, originals, library, db, sql):
    from playwright.async_api import async_playwright, expect

    def posted_to(suffix):
        return lambda response: response.request.method == "POST" and response.url.endswith(suffix)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(base_url=origin)
            await page.goto("/login")
            await page.get_by_test_id("login-username").fill("reader")
            await page.get_by_test_id("login-password").fill("test-passphrase")
            await page.get_by_test_id("login-submit").click()
            await expect(page.get_by_role("complementary", name="Sidebar")).to_be_visible()
            await page.get_by_test_id("usage-stats-modal-disable").click()
            await expect(page.get_by_test_id("usage-stats-modal")).to_be_hidden()
            for ns, job, issue, file, unverified in cases:
                comic, original, unknown, untouched = originals[ns]
                await page.goto(f"/import?tab=follow-up&job_id={job}")
                await page.get_by_test_id("import-cleanup-apply-recheck_deferred_files").click()
                dialog = page.get_by_role("dialog").filter(has_text="Recheck deferred files?")
                await expect(dialog).to_contain_text("Source files will remain unchanged")
                async with page.expect_response(
                    posted_to(f"/{job}/cleanup/recheck_deferred_files")
                ) as pending:
                    await dialog.get_by_role("button", name="Recheck files", exact=True).click()
                assert (await pending.value).ok
                progress = page.get_by_test_id("import-progress-title")
                await expect(progress).to_contain_text("Import complete", timeout=15000)
                counters = page.get_by_test_id("import-collection-progress")
                await expect(
                    counters.locator('[x-text="stats.total_files_imported || 0"]')
                ).to_have_text("1")
                await expect(
                    counters.locator('[x-text="stats.series_imported || 0"]')
                ).to_have_text("0")
                outcome = sql(
                    db,
                    f"SELECT status,match_method,error_message FROM import_files WHERE id={file}",
                )
                assert outcome == f"IMPORTED|{ns}_id|", outcome
                assert (
                    sql(db, f"SELECT status FROM import_files WHERE id={unverified}") == "NO_MATCH"
                )
                path = Path(sql(db, f"SELECT file_path FROM library_files WHERE issue_id={issue}"))
                managed = library / path.relative_to("/comics")
                assert managed.is_file() and comic.read_bytes() == original
                await page.get_by_test_id("import-progress-continue-results").click()
                await page.get_by_test_id("import-results-rollback-action").click()
                async with page.expect_response(posted_to(f"/{job}/rollback")) as pending:
                    await page.get_by_role("button", name="Start rollback", exact=True).click()
                assert (await pending.value).ok
                await expect(progress).to_contain_text("Rollback complete", timeout=15000)
                assert sql(db, f"SELECT status FROM import_jobs WHERE id={job}") == "ROLLED_BACK"
                assert not managed.exists() and comic.read_bytes() == original
                assert unknown.read_bytes() == untouched
                assert sql(db, f"SELECT count(*) FROM library_files WHERE issue_id={issue}") == "0"
                assert sql(db, f"SELECT status FROM issues WHERE id={issue}") == "SKIPPED"
        finally:
            await browser.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(scan())))
