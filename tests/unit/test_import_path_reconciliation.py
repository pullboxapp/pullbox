"""Stale Mylar references need independent identity, not filename guessing."""

from __future__ import annotations

import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import func, select

from pullbox.core.exceptions import ValidationError
from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.core.mylar3_reader import Mylar3Reader
from pullbox.core.source_metadata import MetadataSignal, SourceMetadataExtractor
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import IssueType
from pullbox.services.import_path_identity import same_trusted_issue
from pullbox.services.import_path_reconciliation import reconcile_saved_mylar_paths
from pullbox.services.import_referenced_sources import (
    MylarReferenceRootBoundary,
    validate_mylar_in_place_files,
)
from pullbox.services.import_safety_diagnostics import build_import_safety_diagnostics
from pullbox.services.import_scan_helpers import validate_discovered_files_safety
from pullbox.services.import_scan_materialization import materialize_discovered_scan_results
from pullbox.services.import_source_metadata import (
    load_deferred_source_metadata_for_import_file,
    source_metadata_for_import_file,
)
from scripts.mylar3_import_fixture import create_mylar3_db


def _archive(path, *, issue_id=703887, number="1", pages=2, series="Firefly: Bad Company"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "ComicInfo.xml",
            f"<ComicInfo><Series>{series}</Series><Number>{number}</Number>"
            f"<Year>2019</Year><Web>https://comicvine.gamespot.com/issue/4000-{issue_id}/</Web>"
            "</ComicInfo>",
        )
        for page in range(pages):
            archive.writestr(f"{page}.jpg", b"page")


async def _scan(tmp_path, *, issue_id=703887, pages=2, extra=False):
    folder = tmp_path / "Firefly Bad Company (2019)"
    actual = folder / "Firefly Bad Company 001 (2019).cbz"
    _archive(actual, issue_id=issue_id, pages=pages)
    if extra:
        _archive(folder / "Firefly Bad Company 001 Variant (2019).cbz")
    db = tmp_path / "mylar.db"
    create_mylar3_db(
        db,
        series=[
            {
                "ComicID": "117744",
                "ComicName": "Firefly: Bad Company",
                "ComicYear": "2019",
                "ComicLocation": str(folder),
            }
        ],
        issues=[
            {
                "IssueID": "703887",
                "ComicID": "117744",
                "Issue_Number": "1",
                "Location": "Firefly Bad Company #1 (2019).cbr",
            }
        ],
    )
    discovered = await Mylar3Reader(db, include_missing_files=True).read_series()
    stat = tmp_path.stat()
    validate_mylar_in_place_files(
        discovered, [MylarReferenceRootBoundary(1, tmp_path, tmp_path, stat.st_dev, stat.st_ino)]
    )
    return discovered, actual, db


async def test_scan_reconciles_hash_number_and_extension_only_with_verified_identity(
    db_session, tmp_path
):
    discovered, actual, db = await _scan(tmp_path)
    before = {p: p.read_bytes() for p in [actual, db]}
    await validate_discovered_files_safety(db_session, discovered)
    assert discovered[0].file_count == 1
    assert [f.file_path for f in discovered[0].files] == [str(actual)]
    evidence = discovered[0].files[0].metadata_diagnostics["mylar3_path_reconciliation"]
    assert evidence["recorded_path"].endswith("#1 (2019).cbr")
    assert evidence["method"] == "verified_same_folder_issue_identity"
    assert before == {p: p.read_bytes() for p in before}

    job = ImportJob(source_path=str(db), source_type=ImportSourceType.MYLAR3)
    db_session.add(job)
    await db_session.flush()
    pairs = await materialize_discovered_scan_results(db_session, job, discovered)
    file = await db_session.scalar(select(ImportedFile))
    metadata = await load_deferred_source_metadata_for_import_file(pairs[0][1], file)
    assert metadata.diagnostics["mylar3_path_reconciliation"] == evidence


async def test_scan_reconciliation_reuses_sidecars_already_read_by_mylar(
    db_session, tmp_path, monkeypatch
):
    discovered, actual, _db = await _scan(tmp_path)

    def unexpected_sidecar_read(*args, **kwargs):
        pytest.fail("Reconciliation must reuse the Mylar folder snapshot")

    monkeypatch.setattr(SourceMetadataExtractor, "read_sidecars", unexpected_sidecar_read)
    await validate_discovered_files_safety(db_session, discovered)
    assert [file.file_path for file in discovered[0].files] == [str(actual)]


def test_missing_source_message_does_not_claim_file_changed_after_scan():
    block = build_import_safety_diagnostics("missing", code="source_missing")
    assert block["category"] == "source_missing"
    assert "after scanning" not in block["reason"]
    assert "recorded path" in block["reason"]
    assert block["overrideable"] is False


@pytest.mark.parametrize("case", ["wrong_identity", "ambiguous", "no_pages", "one_page"])
async def test_scan_retains_unsafe_or_ambiguous_missing_reference(db_session, tmp_path, case):
    discovered, _actual, _db = await _scan(
        tmp_path,
        issue_id=42 if case == "wrong_identity" else 703887,
        pages=0 if case == "no_pages" else 1 if case == "one_page" else 2,
        extra=case == "ambiguous",
    )
    await validate_discovered_files_safety(db_session, discovered)
    assert any(f.file_name.endswith(".cbr") for f in discovered[0].files)


async def _saved(session, tmp_path):
    folder = tmp_path / "Firefly Bad Company (2019)"
    actual = folder / "Firefly Bad Company 001 (2019).cbz"
    _archive(actual)
    job = ImportJob(
        source_path=str(tmp_path / "mylar.db"),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.REVIEW,
        total_files_found=2,
    )
    session.add(job)
    await session.flush()
    series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Firefly: Bad Company",
        source_folder=str(folder),
        status=ImportSeriesStatus.MATCHED,
        cv_id=117744,
        files_total=2,
        file_count=2,
    )
    session.add(series)
    await session.flush()
    common = dict(
        import_job_id=job.id,
        import_series_id=series.id,
        comicvine_issue_id=703887,
        parsed_issue_number=1,
        parsed_year=2019,
    )
    missing = ImportedFile(
        **common,
        file_path=str(folder / "Firefly Bad Company #1 (2019).cbr"),
        file_name="Firefly Bad Company #1 (2019).cbr",
        file_size=0,
        file_format="cbr",
        status=ImportedFileStatus.SAFETY_BLOCKED,
        diagnostics={
            "safety_block": {"code": "source_missing"},
            "comicvine_series_id": 117744,
            "source_issue_type": "issue",
            "metadata_signals": {"comicvine_issue_id": "mylar3", "comicvine_series_id": "mylar3"},
        },
    )
    candidate = ImportedFile(
        **common,
        file_path=str(actual),
        file_name=actual.name,
        file_size=actual.stat().st_size,
        file_format="cbz",
        source_signature=build_file_identity_signature(actual),
        status=ImportedFileStatus.MATCHED,
        matched_issue_cv_id=703887,
        match_method="comicinfo",
        diagnostics={
            "comicvine_series_id": 117744,
            "source_issue_type": "issue",
            "metadata_signals": {"comicvine_issue_id": "comicinfo"},
        },
    )
    session.add_all([missing, candidate])
    await session.flush()
    return job, series, missing, candidate


async def test_saved_reconciliation_is_dry_run_first_idempotent_and_keeps_matches(
    db_session, tmp_path
):
    job, series, missing, actual = await _saved(db_session, tmp_path)
    original = Path(actual.file_path).read_bytes()
    report = await reconcile_saved_mylar_paths(db_session, job.id, source_roots=[tmp_path])
    assert report["references_reconciled"] == 1
    assert not db_session.dirty and not db_session.deleted
    assert await db_session.get(ImportedFile, missing.id) is missing
    report = await reconcile_saved_mylar_paths(
        db_session, job.id, source_roots=[tmp_path], apply=True
    )
    assert report["references_reconciled"] == 1
    assert await db_session.scalar(select(func.count()).select_from(ImportedFile)) == 1
    assert actual.status is ImportedFileStatus.MATCHED
    assert actual.matched_issue_cv_id == 703887
    assert series.files_total == series.file_count == job.total_files_found == 1
    assert job.status is ImportJobStatus.REVIEW
    assert Path(actual.file_path).read_bytes() == original
    assert (
        actual.diagnostics["source_metadata"]["mylar3_path_reconciliation"]["recorded_file_id"]
        == missing.id
    )
    again = await reconcile_saved_mylar_paths(
        db_session, job.id, source_roots=[tmp_path], apply=True
    )
    assert again["references_reconciled"] == 0


@pytest.mark.parametrize(
    "case",
    [
        "selected",
        "manual",
        "skip",
        "allow_once",
        "wrong_identity",
        "changed",
        "outside_root",
        "symlink",
        "no_pages",
        "duplicate_reference",
        "duplicate_candidate",
    ],
)
async def test_saved_reconciliation_never_guesses_or_changes_review_decisions(
    db_session, tmp_path, case
):
    job, series, missing, actual = await _saved(db_session, tmp_path)
    roots = [tmp_path]
    if case == "selected":
        actual.include_in_import = True
    elif case == "manual":
        series.user_selected_cv_id = 117744
    elif case == "skip":
        actual.status = ImportedFileStatus.SKIPPED
    elif case == "allow_once":
        actual.diagnostics = {**actual.diagnostics, "safety_exception": {"allowed_once": True}}
    elif case in {"wrong_identity", "changed", "no_pages"}:
        _archive(
            Path(actual.file_path),
            issue_id=42 if case == "wrong_identity" else 703887,
            pages=0 if case == "no_pages" else 2,
        )
        if case != "changed":
            actual.source_signature = build_file_identity_signature(Path(actual.file_path))
    elif case == "outside_root":
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        roots = [allowed]
    elif case == "symlink":
        path = Path(actual.file_path)
        other = tmp_path / "other.cbz"
        path.rename(other)
        path.symlink_to(other)
    else:
        reference = missing if case == "duplicate_reference" else actual
        clone = ImportedFile(
            import_job_id=job.id,
            import_series_id=series.id,
            file_path=reference.file_path,
            file_name=reference.file_name,
            file_size=reference.file_size,
            file_format=reference.file_format,
            comicvine_issue_id=703887,
            status=reference.status,
            diagnostics=reference.diagnostics,
            source_signature=reference.source_signature,
        )
        db_session.add(clone)
    await db_session.flush()
    report = await reconcile_saved_mylar_paths(db_session, job.id, source_roots=roots, apply=True)
    assert report["references_reconciled"] == 0
    assert await db_session.get(ImportedFile, missing.id) is missing


@pytest.mark.parametrize(
    "case",
    [
        "no_archive_id",
        "different_series",
        "different_number",
        "annual",
        "conflict",
        "date",
        "untrusted_record",
    ],
)
async def test_shared_identity_rule_rejects_conflicting_evidence(db_session, tmp_path, case):
    _job, series, missing, actual = await _saved(db_session, tmp_path)
    recorded = source_metadata_for_import_file(series, missing)
    fresh = SourceMetadataExtractor().from_path(actual.file_path)
    assert same_trusted_issue(recorded, fresh)
    if case == "no_archive_id":
        fresh = replace(fresh, signals={"comicvine_issue_id": MetadataSignal.RELEASE_TITLE})
    elif case == "different_series":
        fresh = replace(fresh, comicvine_series_id=42)
    elif case == "different_number":
        fresh = replace(fresh, issue_number=2)
    elif case == "annual":
        fresh = replace(fresh, issue_type=IssueType.ANNUAL)
    elif case == "conflict":
        fresh = replace(fresh, diagnostics={"identity_conflicts": [{"field": "series"}]})
    elif case == "date":
        recorded = replace(recorded, diagnostics={"mylar3_issue": {"release_date": "2020-01-01"}})
    else:
        recorded = replace(recorded, signals={"comicvine_issue_id": MetadataSignal.RELEASE_TITLE})
    assert not same_trusted_issue(recorded, fresh)


@pytest.mark.parametrize("case", ["active_job", "folder_import", "no_roots"])
async def test_saved_reconciliation_rejects_invalid_scope(db_session, tmp_path, case):
    job, _series, _missing, _actual = await _saved(db_session, tmp_path)
    if case == "active_job":
        job.status = ImportJobStatus.IMPORTING
    elif case == "folder_import":
        job.source_type = ImportSourceType.FILESYSTEM
    with pytest.raises(ValidationError):
        await reconcile_saved_mylar_paths(
            db_session, job.id, source_roots=[] if case == "no_roots" else [tmp_path]
        )


async def test_reconciliation_streams_over_multiple_batches_and_rolls_back(db_session, tmp_path):
    job, series, missing, actual = await _saved(db_session, tmp_path)
    # One large review series crosses the 250-row cursor boundary.
    for index in range(1, 255):
        issue_id = 703887 + index
        path = Path(actual.file_path).with_name(f"Firefly Bad Company {index + 1:03} (2019).cbz")
        _archive(path, issue_id=issue_id, number=str(index + 1))
        for reference in (missing, actual):
            is_missing = reference is missing
            name = f"Firefly Bad Company #{index + 1} (2019).cbr" if is_missing else path.name
            db_session.add(
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=series.id,
                    file_path=str(path.with_name(name)),
                    file_name=name,
                    file_size=0 if is_missing else path.stat().st_size,
                    file_format="cbr" if is_missing else "cbz",
                    status=reference.status,
                    comicvine_issue_id=issue_id,
                    parsed_issue_number=index + 1,
                    parsed_year=2019,
                    matched_issue_cv_id=None if is_missing else issue_id,
                    source_signature={} if is_missing else build_file_identity_signature(path),
                    diagnostics=reference.diagnostics,
                )
            )
    await db_session.flush()
    async with db_session.begin_nested() as transaction:
        report = await reconcile_saved_mylar_paths(
            db_session, job.id, source_roots=[tmp_path], apply=True
        )
        assert report["references_reconciled"] == 255
        assert len(report["samples"]) == 20
        assert await db_session.scalar(select(func.count()).select_from(ImportedFile)) == 255
        await transaction.rollback()
    assert await db_session.scalar(select(func.count()).select_from(ImportedFile)) == 510


async def test_report_accounts_for_unmatched_and_unsafe_references(db_session, tmp_path):
    job, series, missing, actual = await _saved(db_session, tmp_path)
    actual.include_in_import = True
    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=series.id,
            file_path=str(tmp_path / "unavailable.cbz"),
            file_name="unavailable.cbz",
            file_size=0,
            file_format="cbz",
            status=ImportedFileStatus.SAFETY_BLOCKED,
            comicvine_issue_id=98765,
            diagnostics=missing.diagnostics,
        )
    )
    await db_session.flush()
    report = await reconcile_saved_mylar_paths(db_session, job.id, source_roots=[tmp_path])
    assert report["missing_references"] == 2
    assert report["remaining_missing_references"] == 2
    assert report["retained_reasons"] == {
        "review_or_source_protected": 1,
        "no_unique_matched_counterpart": 1,
    }
