"""Folder discovery can use local title/year evidence without remote searches."""

import zipfile
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.providers.base import IssueSummary, SeriesSearchResult
from pullbox.services.catalog.contract import CatalogError
from pullbox.services.catalog.lookup import CatalogLookupService
from pullbox.services.import_provider_cache import CachedImportMetadataProvider
from pullbox.services.import_service import ImportService
from pullbox.services.import_source_metadata import source_metadata_for_matching_series
from tests.unit.test_catalog_reader import installed_reader


class LocalCatalog:
    is_local_catalog = True

    def __init__(self):
        self.results = [
            SeriesSearchResult("123", "Absolute Batman", 2024, "DC", 20, None, None, None)
        ]
        self.issues = [IssueSummary("456", 1, None, "2024-10-01", None, "issue")]
        self.searches = []

    async def search_series(self, query, year=None, *, limit=1000, **kwargs):
        self.searches.append((query, limit))
        return self.results[:limit]

    async def search_series_globally(self, query, *, max_results=1000, **kwargs):
        return await self.search_series(query, limit=max_results), len(self.results)

    async def get_issues_for_series(self, series_id):
        return self.issues

    async def get_issues_for_series_by_numbers(self, series_id, numbers):
        assert len(numbers) <= 200
        return self.issues

    async def get_issue_batch_cached(self, ids):
        return {}


async def seed(session, *, title="Absolute Batman", year=2024, number=1):
    job = ImportJob(
        source_path="/imports",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.MATCHING,
    )
    session.add(job)
    await session.flush()
    series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=title,
        raw_year=year,
        source_folder=f"/imports/{title} {year}",
        file_count=1,
        status=ImportSeriesStatus.PENDING,
    )
    session.add(series)
    await session.flush()
    file = ImportedFile(
        import_job_id=job.id,
        import_series_id=series.id,
        file_path=f"/imports/{title} {year}/{title} #001.cbz",
        file_name=f"{title} #001.cbz",
        file_format="cbz",
        parsed_series=title,
        parsed_year=year,
        parsed_issue_number=number,
        status=ImportedFileStatus.PENDING,
    )
    session.add(file)
    await session.flush()
    return job, series, file


def service(job, provider):
    result = ImportService(
        series_service=AsyncMock(), metadata_service=AsyncMock(), event_bus=AsyncMock()
    )
    result._scan_provider_cache_by_job[job.id] = CachedImportMetadataProvider(provider)
    return result


async def test_folder_title_year_matches_local_catalog_and_issue(db_session):
    job, series, file = await seed(db_session)
    provider = LocalCatalog()
    owner = service(job, provider)
    await owner._run_matching(db_session, job)
    assert series.status == ImportSeriesStatus.MATCHED
    assert series.cv_id == 123
    assert series.diagnostics["reason"] == "local_catalog_title_year_verified"
    await owner._run_file_matching(db_session, job)
    assert file.status == ImportedFileStatus.MATCHED
    assert file.matched_issue_cv_id == 456


async def test_real_catalog_matches_fractional_issue_without_remote_provider(db_session, tmp_path):
    job, series, file = await seed(db_session, title="Batman", year=2016, number=0.5)
    owner = service(job, CatalogLookupService(installed_reader(tmp_path)))
    await owner._run_matching(db_session, job)
    assert series.cv_id == 10
    await owner._run_file_matching(db_session, job)
    assert file.matched_issue_cv_id == 100


@pytest.mark.parametrize(
    "case",
    [
        "remote",
        "missing",
        "ambiguous",
        "year",
        "fuzzy",
        "no_issue",
        "embedded_id",
        "blocked",
        "manual",
        "collection",
        "annual",
        "catalog_error",
        "overflow",
        "wrong_year",
        "lettered_issue",
        "identity_conflict",
    ],
)
async def test_uncertain_or_ineligible_folder_does_not_auto_match(db_session, case):
    job, series, file = await seed(db_session)
    provider = LocalCatalog()
    if case == "remote":
        provider.is_local_catalog = False
    elif case == "missing":
        provider.results = []
    elif case == "ambiguous":
        provider.results.append(replace(provider.results[0], provider_id="124"))
    elif case == "year":
        series.raw_year = file.parsed_year = None
    elif case == "fuzzy":
        provider.results = [replace(provider.results[0], title="Absolute Batman Special")]
    elif case == "no_issue":
        provider.issues = []
    elif case == "embedded_id":
        file.comicvine_issue_id = 987
        file.diagnostics = {"metadata_signals": {"comicvine_issue_id": "comicinfo"}}
    elif case == "blocked":
        file.status = ImportedFileStatus.SAFETY_BLOCKED
    elif case == "manual":
        series.status = ImportSeriesStatus.MATCHED
        series.cv_id = series.user_selected_cv_id = 999
    elif case in {"collection", "annual"}:
        file.diagnostics = {"source_issue_type": "tpb" if case == "collection" else "annual"}
    elif case == "catalog_error":
        provider.search_series = AsyncMock(side_effect=CatalogError("Unavailable"))
    elif case == "overflow":
        provider.results *= 1001
    elif case == "wrong_year":
        provider.results = [replace(provider.results[0], year_start=2011)]
    elif case == "lettered_issue":
        file.issue_number_raw = "1AU"
    elif case == "identity_conflict":
        file.diagnostics = {
            "source_metadata": {
                "identity_conflicts": [
                    {"field": "comicvine_series_id", "first": 123, "conflicting": 999}
                ]
            }
        }
    await service(job, provider)._run_matching(db_session, job)
    if case == "manual":
        assert series.cv_id == 999
    else:
        assert series.status == ImportSeriesStatus.NO_MATCH
        assert series.cv_id is None
    if case in {"remote", "blocked", "embedded_id", "manual"}:
        assert provider.searches == []


@pytest.mark.parametrize("known_empty", [False, True])
async def test_deferred_comicinfo_is_persisted_with_bounded_probes(
    db_session, tmp_path, known_empty
):
    job, series, first = await seed(db_session)
    files = [first]
    for n in range(4):
        path = tmp_path / f"Absolute Batman {n + 1:03}.cbz"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "ComicInfo.xml",
                "<ComicInfo><Series>Absolute Batman</Series>"
                "<Number>1</Number><Year>2024</Year></ComicInfo>",
            )
        if n:
            file = ImportedFile(
                import_job_id=job.id,
                import_series_id=series.id,
                file_path=str(path),
                file_name=path.name,
                file_format="cbz",
                status=ImportedFileStatus.PENDING,
            )
            db_session.add(file)
            files.append(file)
        else:
            first.file_path, first.file_name = str(path), path.name
        files[-1].diagnostics = {
            "source_metadata": {"archive_metadata_loaded": False, "archive_metadata_deferred": True}
        }
        if known_empty and n < 3:
            files[-1].diagnostics["archive_member_evidence"] = {
                "member_index_scanned": True,
                "comicinfo_entry_count": 0,
            }
    await db_session.flush()
    await source_metadata_for_matching_series(db_session, series, trusted_identity_probe_limit=3)
    if known_empty:
        assert files[-1].has_comicinfo is True
        assert sum(f.has_comicinfo for f in files) == 1
        return
    assert sum(f.has_comicinfo for f in files) == 3
    assert all(f.diagnostics["source_metadata"]["archive_metadata_loaded"] for f in files[:3])
    assert files[-1].diagnostics["source_metadata"]["archive_metadata_deferred"] is True


@pytest.mark.parametrize("number", [0, 0.5, 1000000])
async def test_local_match_uses_actual_issue_numbers_not_issue_count(db_session, number):
    job, series, _file = await seed(db_session, number=number)
    provider = LocalCatalog()
    provider.issues = [replace(provider.issues[0], issue_number=number)]
    await service(job, provider)._run_matching(db_session, job)
    assert series.cv_id == 123


@pytest.mark.parametrize(
    "kind,title", [("annual", "Absolute Batman Annual"), ("omnibus", "Absolute Batman Omnibus")]
)
async def test_explicit_nonstandard_title_can_match_its_own_catalog(db_session, kind, title):
    job, series, file = await seed(db_session, title=title)
    file.diagnostics = {"source_issue_type": kind}
    provider = LocalCatalog()
    provider.results = [replace(provider.results[0], title=title, issue_count=1)]
    await service(job, provider)._run_matching(db_session, job)
    assert series.cv_id == 123
