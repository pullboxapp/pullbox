"""Folder scans resolve ComicInfo issue links without remote discovery calls."""

from __future__ import annotations

import hashlib
import zipfile
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.providers.base import IssueMetadata, IssueSummary, SeriesMetadata
from pullbox.services.catalog.contract import CatalogError
from pullbox.services.import_provider_cache import CachedImportMetadataProvider
from pullbox.services.import_service import ImportService
from tests.unit.test_catalog_reader import installed_reader


def _issue(issue_id, series_id="177600", number=1):
    return IssueMetadata(
        provider_id=str(issue_id),
        series_provider_id=series_id,
        issue_number=float(number),
        title=None,
        description=None,
        release_date=None,
        store_date=None,
        cover_url=None,
        page_count=None,
        comicvine_url=None,
    )


class LocalIdentityProvider:
    """Only cached identity reads are allowed during series discovery."""

    def __init__(self, issues):
        self.issues = issues
        self.batches = []
        self.series_reads = []

    async def get_issue_batch_cached(self, ids):
        self.batches.append(list(ids))
        return {key: self.issues[key] for key in ids if key in self.issues}

    async def get_series_cached(self, series_id):
        self.series_reads.append(series_id)
        return SeriesMetadata(
            provider_id=series_id,
            title="1776",
            sort_title="1776",
            year_start=2026,
            year_end=None,
            status=None,
            publisher="Marvel",
            description=None,
            cover_url=None,
            issue_count=5,
            comicvine_url=None,
        )

    async def get_series(self, *args, **kwargs):
        pytest.fail("Series discovery must not fetch remote metadata")

    async def get_issue(self, *args, **kwargs):
        pytest.fail("Series discovery must not fetch remote metadata")

    async def search_series(self, *args, **kwargs):
        pytest.fail("Series discovery must not start a remote title search")

    async def get_issues_for_series(self, series_id):
        return [
            IssueSummary(
                provider_id=key,
                issue_number=value.issue_number,
                title=None,
                release_date=None,
                cover_url=None,
                issue_type="issue",
            )
            for key, value in self.issues.items()
            if value.series_provider_id == series_id
        ]


async def _seed(session, ids, *, status=ImportSeriesStatus.PENDING, conflicts=None):
    job = ImportJob(
        source_path="/imports/1776",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.MATCHING,
    )
    session.add(job)
    await session.flush()
    series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="1776",
        raw_year=2026,
        status=status,
        file_count=len(ids),
        source_folder="/imports/1776",
    )
    session.add(series)
    await session.flush()
    files = []
    for number, issue_id in enumerate(ids, 1):
        file = ImportedFile(
            import_job_id=job.id,
            import_series_id=series.id,
            file_path=f"/imports/1776/1776 {number:03} (2026).cbz",
            file_name=f"1776 {number:03} (2026).cbz",
            file_format="cbz",
            parsed_series="1776",
            parsed_year=2026,
            parsed_issue_number=float(number),
            has_comicinfo=True,
            comicvine_issue_id=issue_id,
            status=ImportedFileStatus.PENDING,
            diagnostics={
                "metadata_signals": {
                    "series_name": "comicinfo",
                    "year": "comicinfo",
                    "comicvine_issue_id": "comicinfo",
                },
                "source_metadata": {
                    "comicinfo": {
                        "series": "1776",
                        "number": str(number),
                        "web": f"https://comicvine.gamespot.com/issue/4000-{issue_id}/",
                    },
                    "identity_conflicts": conflicts or [],
                },
            },
        )
        session.add(file)
        files.append(file)
    await session.flush()
    return job, series, files


def _service(job, provider):
    service = ImportService(
        series_service=AsyncMock(), metadata_service=AsyncMock(), event_bus=AsyncMock()
    )
    service._scan_provider_cache_by_job[job.id] = CachedImportMetadataProvider(provider)
    return service


async def test_issue_only_comicinfo_matches_parent_and_files(db_session):
    ids = [1143246, 1149812, 1154053, 1156257, 1159114]
    job, series, files = await _seed(db_session, ids)
    provider = LocalIdentityProvider(
        {str(key): _issue(key, number=n) for n, key in enumerate(ids, 1)}
    )
    service = _service(job, provider)

    await service._run_matching(db_session, job)

    assert series.status == ImportSeriesStatus.MATCHED
    assert series.cv_id == 177600
    assert series.cv_match_method == "comicinfo_cv_id"
    assert series.diagnostics["reason"] == "comicinfo_issue_parent_verified"
    assert provider.batches == [[str(key) for key in ids]]
    await service._run_file_matching(db_session, job)
    assert [file.matched_issue_cv_id for file in files] == ids
    assert all(file.status == ImportedFileStatus.MATCHED for file in files)


@pytest.mark.parametrize(
    "case",
    [
        "mixed",
        "missing",
        "wrong_issue",
        "wrong_parent",
        "conflict",
        "untrusted",
        "blocked",
        "null_signals",
    ],
)
async def test_unverified_or_conflicting_issue_identity_stays_in_review(db_session, case):
    conflicts = (
        [{"field": "comicvine_issue_id", "first": 100, "conflicting": 101}]
        if case == "conflict"
        else None
    )
    job, series, files = await _seed(db_session, [100, 101], conflicts=conflicts)
    provider = LocalIdentityProvider({"100": _issue(100), "101": _issue(101)})
    if case == "mixed":
        provider.issues["101"] = _issue(101, "888")
    elif case == "missing":
        del provider.issues["101"]
    elif case == "wrong_issue":
        provider.issues["101"] = _issue(999)
    elif case == "wrong_parent":
        provider.issues["101"] = _issue(101, "invalid")
    elif case == "untrusted":
        for file in files:
            file.diagnostics = {"metadata_signals": {"comicvine_issue_id": "filename"}}
    elif case == "blocked":
        for file in files:
            file.status = ImportedFileStatus.SAFETY_BLOCKED
    elif case == "null_signals":
        for file in files:
            file.diagnostics = {"metadata_signals": None}

    await _service(job, provider)._run_matching(db_session, job)

    assert series.status == ImportSeriesStatus.NO_MATCH
    assert series.cv_id is None
    if case in {"conflict", "untrusted", "blocked", "null_signals"}:
        assert provider.batches == []
    if case == "mixed":
        assert series.diagnostics["reason"] == "trusted_source_identity_conflict"


async def test_manual_series_choice_is_not_replaced(db_session):
    job, series, _ = await _seed(db_session, [100], status=ImportSeriesStatus.MATCHED)
    series.cv_id = 777
    series.user_selected_cv_id = 777
    series.cv_match_method = "user_override"
    provider = LocalIdentityProvider({"100": _issue(100)})
    await _service(job, provider)._run_matching(db_session, job)
    assert series.cv_id == series.user_selected_cv_id == 777
    assert series.cv_match_method == "user_override"
    assert provider.batches == []


async def test_issue_parent_discovery_pages_and_checks_late_conflicts(db_session):
    ids = list(range(1000, 1251))
    job, series, _ = await _seed(db_session, ids)
    provider = LocalIdentityProvider({str(key): _issue(key) for key in ids})
    provider.issues[str(ids[-1])] = _issue(ids[-1], "888")
    await _service(job, provider)._run_matching(db_session, job)
    assert series.status == ImportSeriesStatus.NO_MATCH
    assert series.diagnostics["reason"] == "trusted_source_identity_conflict"
    assert max(map(len, provider.batches)) <= 200
    assert sum(map(len, provider.batches)) == len(ids)


async def test_issue_parent_match_does_not_hide_a_number_conflict(db_session):
    job, series, files = await _seed(db_session, [100])
    files[0].parsed_issue_number = 0.0
    provider = LocalIdentityProvider({"100": _issue(100, number=1)})
    service = _service(job, provider)
    await service._run_matching(db_session, job)
    assert series.status == ImportSeriesStatus.MATCHED
    await service._run_file_matching(db_session, job)
    assert files[0].status == ImportedFileStatus.NO_MATCH
    assert files[0].matched_issue_cv_id is None
    assert files[0].diagnostics["conflict_type"] == "comicinfo_issue_number_mismatch"
    assert files[0].parsed_issue_number == 0.0


@pytest.mark.parametrize("phase", ["series", "files"])
async def test_unavailable_catalog_does_not_trigger_remote_fallback(db_session, phase):
    job, series, files = await _seed(db_session, [100])
    provider = LocalIdentityProvider({"100": _issue(100)})
    service = _service(job, provider)
    if phase == "files":
        await service._run_matching(db_session, job)
        assert series.status == ImportSeriesStatus.MATCHED
    provider.get_issue_batch_cached = AsyncMock(side_effect=CatalogError("Catalog unavailable"))
    if phase == "series":
        await service._run_matching(db_session, job)
        assert series.status == ImportSeriesStatus.NO_MATCH
    else:
        await service._run_file_matching(db_session, job)
    assert files[0].status == ImportedFileStatus.NO_MATCH
    assert files[0].matched_issue_cv_id is None


async def test_issue_parent_match_preserves_title_conflicts(db_session):
    job, _series, files = await _seed(db_session, [100])
    files[0].parsed_series = "Completely Different Comic"
    provider = LocalIdentityProvider({"100": _issue(100)})
    service = _service(job, provider)
    await service._run_matching(db_session, job)
    await service._run_file_matching(db_session, job)
    assert files[0].status == ImportedFileStatus.NO_MATCH
    assert files[0].matched_issue_cv_id is None


async def test_issue_parent_requires_matching_cached_series_profile(db_session):
    job, series, _ = await _seed(db_session, [100])
    provider = LocalIdentityProvider({"100": _issue(100)})
    provider.get_series_cached = AsyncMock(return_value=None)
    await _service(job, provider)._run_matching(db_session, job)
    assert series.status == ImportSeriesStatus.NO_MATCH
    assert series.cv_id is None


@pytest.mark.parametrize(
    "identity, expected_id",
    [
        ("<Web>https://comicvine.gamespot.com/issue/4000-100/</Web>", 100),
        ("<Notes>[cv_issue_id:100]</Notes>", 100),
        ("<Notes>Scraped metadata from ComicVine [CVDB100].</Notes>", None),
    ],
)
async def test_real_cbz_scan_uses_embedded_issue_identity_without_changing_archive(
    db_session, tmp_path, monkeypatch, identity, expected_id
):
    reader = installed_reader(tmp_path)
    monkeypatch.setattr("pullbox.services.catalog.reader.get_catalog_reader", lambda: reader)
    source = tmp_path / "imports"
    folder = source / "Batman (2016)"
    folder.mkdir(parents=True)
    comic = folder / "Batman 0.5 (2016).cbz"
    with zipfile.ZipFile(comic, "w") as archive:
        archive.writestr(
            "ComicInfo.xml",
            f"""<ComicInfo><Series>Batman</Series>
            <Number>0.5</Number><Year>2016</Year><Volume>2016</Volume>
            {identity}</ComicInfo>""",
        )
        archive.writestr("001.jpg", b"first test page")
        archive.writestr("002.jpg", b"second test page")
    original = hashlib.sha256(comic.read_bytes()).digest()
    job = ImportJob(source_path=str(source), source_type=ImportSourceType.FILESYSTEM)
    db_session.add(job)
    await db_session.flush()
    remote = AsyncMock()
    metadata_service = AsyncMock()
    metadata_service._provider = remote
    service = ImportService(
        series_service=AsyncMock(), metadata_service=metadata_service, event_bus=AsyncMock()
    )

    await service.start_scan(db_session, job.id)

    item = await db_session.scalar(
        select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
    )
    file = await db_session.scalar(select(ImportedFile).where(ImportedFile.import_job_id == job.id))
    assert job.status == ImportJobStatus.REVIEW
    assert item.cv_id == 10
    assert file.status == ImportedFileStatus.MATCHED
    assert file.matched_issue_cv_id == 100
    assert file.comicvine_issue_id == expected_id
    assert item.diagnostics["reason"] == (
        "comicinfo_issue_parent_verified" if expected_id else "local_catalog_title_year_verified"
    )
    assert file.diagnostics.get("comicvine_series_id") is None
    assert remote.mock_calls == []
    assert hashlib.sha256(comic.read_bytes()).digest() == original
