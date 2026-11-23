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
    from scripts.mylar3_import_fixture import create_minimal_cbz, create_mylar3_db
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
    root = imports / "saved-mylar"
    folder = root / "Cutover Shelf 1 (2024)"
    wrong, ready, later = [folder / f"Cutover Shelf 1 00{n}.cbz" for n in (1, 2, 3)]
    _archive(wrong, number="1", primary="Metron")
    _archive(later, number="3", primary="GCD")
    create_minimal_cbz(ready)
    database = root / "mylar.db"
    create_mylar3_db(
        database,
        series=[
            dict(
                ComicID="92000",
                ComicName="Cutover Shelf 1",
                ComicYear="2024",
                ComicLocation=f"/old-lib/{folder.name}",
                Total=3,
            )
        ],
        issues=[
            dict(ComicID="92000", IssueID=str(92000 + n), Issue_Number=str(n), Location=path.name)
            for n, path in enumerate((wrong, ready, later), 1)
        ],
    )
    originals["mylar"] = {p: p.read_bytes() for p in (database, wrong, ready, later)}
    return originals


async def _finish_mylar(session, service):
    from pullbox.models.series import Series
    from pullbox.schemas.import_job import ConfirmImportRequest

    (await session.get(Series, 1001)).comicvine_id = 92000
    for issue_id, cv in ((10016, 92001), (10017, 92002), (10018, 92003)):
        (await session.get(Issue, issue_id)).comicvine_id = cv
    job = ImportJob(
        source_type=ImportSourceType.MYLAR3,
        source_path="/imports/saved-mylar/mylar.db",
        mylar3_path_map={"/old-lib": "/imports/saved-mylar"},
        mylar3_path_map_confirmed=True,
        target_library_root_id=77,
    )
    apply_ingest_policy_to_import_job(job, await load_library_ingest_policy(session))
    session.add(job)
    await session.commit()
    await service.start_scan(session, job.id)
    wrong, ready, later = (
        await session.scalars(
            select(ImportedFile)
            .where(ImportedFile.import_job_id == job.id)
            .order_by(ImportedFile.file_name)
        )
    ).all()
    assert job.status is ImportJobStatus.REVIEW
    assert wrong.status is ImportedFileStatus.NO_MATCH and wrong.matched_issue_id is None
    assert wrong.diagnostics["conflict_type"] == "mylar_native_identity_conflict"
    assert ready.matched_issue_id == 10017
    await service.update_file_selection(session, job.id, ready.id, include_in_import=True)
    await service.confirm_import(
        session, job.id, ConfirmImportRequest(series_ids=[ready.import_series_id])
    )
    await service.run_import(session, job.id)
    await session.commit()
    for obj in (job, wrong, ready):
        await session.refresh(obj)
    assert job.status is ImportJobStatus.COMPLETED
    assert ready.status is ImportedFileStatus.IMPORTED, ready.error_message
    assert wrong.status is ImportedFileStatus.NO_MATCH and not wrong.include_in_import
    return (job.id, wrong.id, ready.id, later.id, wrong.import_series_id)


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
            mylar = await _finish_mylar(session, service)
    finally:
        await engine.dispose()
    return {"folder": cases, "mylar": mylar}


async def exercise(origin, cases, originals, library, db, sql, owned_digest):
    from playwright.async_api import async_playwright, expect

    from tests.e2e.accessibility import AXE_SCRIPT, WCAG_AA_TAGS
    from tests.integration import postgres_linked_catalog as linked
    from tests.integration import postgres_manual_metadata as manual

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
            for ns, job, issue, file, unverified in cases["folder"]:
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
            job, wrong, ready, later, group = cases["mylar"]
            await page.goto(f"/import?tab=follow-up&job_id={job}")
            row = page.get_by_test_id(f"import-orphaned-row-{group}")
            await expect(row).to_contain_text("Cutover Shelf 1")
            await row.get_by_test_id(f"import-orphaned-recover-{group}").click()
            dialog = page.get_by_test_id("import-orphaned-recovery-modal")
            await expect(dialog).to_contain_text("Cutover Shelf 1 001.cbz")
            disputed = page.get_by_test_id(f"orphaned-recovery-file-{wrong}")
            await expect(disputed).not_to_contain_text("Suggested #1")
            await expect(disputed.get_by_role("link", name="Open Metron issue")).to_have_attribute(
                "href", "/issues/10012"
            )
            await expect(dialog.get_by_role("link", name="Open GCD issue")).to_have_attribute(
                "href", "/issues/10012"
            )
            await page.evaluate(AXE_SCRIPT.read_text())
            violations = await page.evaluate(
                """async tags => (await axe.run(
                '[data-testid="import-orphaned-recovery-modal"]',
                {runOnly: {type: 'tag', values: tags}})).violations.map(v => v.id)""",
                WCAG_AA_TAGS,
            )
            assert not violations, violations
            await expect(
                disputed.get_by_role("link", name="Open recorded issue")
            ).to_have_attribute("href", "/issues/10016")
            async with page.expect_popup() as popup:
                await disputed.get_by_role("link", name="Open recorded issue").click()
            recorded = await popup.value
            await expect(recorded.get_by_test_id("issue-action-import")).to_be_visible()
            await recorded.close()
            await expect(dialog).not_to_contain_text("Cutover Shelf 1 002.cbz")
            neighbor = library / "Cutover Shelf 1 (2024)/Cutover Shelf 1 (2024) #002.cbz"
            before = neighbor.read_bytes(), neighbor.stat().st_mtime_ns
            assert (
                sql(
                    db,
                    f"SELECT status, diagnostics->>'conflict_type' FROM import_files "
                    f"WHERE id={wrong} AND matched_issue_id IS NULL",
                )
                == "NO_MATCH|mylar_native_identity_conflict"
            )
            await disputed.get_by_test_id(f"orphaned-recovery-skip-{wrong}").click()
            await dialog.get_by_test_id("orphaned-recovery-save").click()
            await expect(dialog.get_by_test_id(f"orphaned-recovery-file-{wrong}")).to_have_count(
                0, timeout=20000
            )
            await expect(dialog.get_by_test_id(f"orphaned-recovery-file-{later}")).to_be_visible()
            assert sql(db, f"SELECT status FROM import_files WHERE id={wrong}") == "SKIPPED"
            assert sql(db, f"SELECT status FROM import_files WHERE id={later}") == "NO_MATCH"
            await dialog.get_by_test_id(f"orphaned-recovery-skip-{later}").click()
            await dialog.get_by_test_id("orphaned-recovery-save").click()
            await expect(row).to_have_count(0, timeout=20000)
            assert sql(db, f"SELECT status FROM import_files WHERE id={later}") == "SKIPPED"
            assert sql(db, f"SELECT status FROM import_files WHERE id={ready}") == "IMPORTED"
            assert sql(db, f"SELECT status FROM import_series WHERE id={group}") == "DUPLICATE"
            assert (neighbor.read_bytes(), neighbor.stat().st_mtime_ns) == before
            assert sql(db, "SELECT count(*) FROM library_files WHERE issue_id=10016") == "0"
            assert sql(db, "SELECT count(*) FROM library_files WHERE issue_id=10017") == "1"
            assert all(path.read_bytes() == before for path, before in originals["mylar"].items())
            catalog_rows = await linked.exercise(page, library, sql, db, owned_digest)
            await manual.exercise(page, library, sql, db)
            return catalog_rows
        finally:
            await browser.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(scan())))
