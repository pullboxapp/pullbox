"""Tests for Tasks R-4.1 and R-4.2 — file matching engine and conflict detection.

Verifies that _run_file_matching() matches ImportedFile records to Issues
using CV ID, issue number, and fuzzy strategies, and that _detect_conflicts()
groups duplicate matches with tiebreaker logic.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from pullbox.core.exceptions import JobPausedError
from pullbox.core.source_metadata import MetadataSignal, SourceMetadata
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobLog,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.providers.base import IssueMetadata, IssueSummary, SeriesMetadata
from pullbox.services import import_file_matching_progress
from pullbox.services.import_service import ImportService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.schemas.import_job import ImportProgressEvent


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_service() -> ImportService:
    return ImportService(
        series_service=AsyncMock(),
        metadata_service=AsyncMock(),
        event_bus=AsyncMock(),
    )


async def _setup_series_with_issues(
    session: AsyncSession,
    *,
    title: str = "Batman",
    year: int = 2016,
    cv_id: int = 97508,
    issues: list[tuple[float, int | None, str | None]] | None = None,
    publisher_id: int | None = None,
    series_status: SeriesStatus = SeriesStatus.CONTINUING,
    series_type: SeriesType = SeriesType.STANDARD,
) -> tuple[Series, list[Issue]]:
    """Create a Series with Issues. issues = [(issue_number, cv_issue_id, title), ...]"""
    if publisher_id is None:
        pub = Publisher(name="DC Comics", comicvine_id=10)
        session.add(pub)
        await session.flush()
        publisher_id = pub.id

    s = Series(
        title=title,
        sort_title=title.lower(),
        year_start=year,
        comicvine_id=cv_id,
        publisher_id=publisher_id,
        status=series_status,
        series_type=series_type,
    )
    session.add(s)
    await session.flush()

    if issues is None:
        issues = [
            (1.0, 100001, "I Am Gotham Part 1"),
            (2.0, 100002, "I Am Gotham Part 2"),
            (3.0, 100003, "I Am Gotham Part 3"),
        ]

    issue_objs = []
    for num, cv_issue_id, title in issues:
        issue = Issue(
            series_id=s.id,
            issue_number=num,
            comicvine_id=cv_issue_id,
            title=title,
            status=IssueStatus.WANTED,
        )
        session.add(issue)
        issue_objs.append(issue)

    await session.flush()
    return s, issue_objs


async def _setup_import_job(
    session: AsyncSession,
    series: Series,
    *,
    files: list[dict[str, object]] | None = None,
) -> tuple[ImportJob, ImportedSeries, list[ImportedFile]]:
    """Create an ImportJob with an ImportedSeries and ImportedFile records."""
    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.FILE_MATCHING,
    )
    session.add(job)
    await session.flush()

    imp_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=series.title,
        raw_year=series.year_start,
        status=ImportSeriesStatus.MATCHED,
        cv_id=series.comicvine_id,
        cv_match_score=0.95,
        cv_match_method="exact_title_year",
        file_count=len(files) if files else 3,
        series_id=series.id,
    )
    session.add(imp_series)
    await session.flush()

    if files is None:
        files = [
            {"file_name": "Batman 001.cbz", "parsed_issue_number": 1.0, "parsed_series": "Batman"},
            {"file_name": "Batman 002.cbz", "parsed_issue_number": 2.0, "parsed_series": "Batman"},
            {"file_name": "Batman 003.cbz", "parsed_issue_number": 3.0, "parsed_series": "Batman"},
        ]

    imp_files = []
    for f in files:
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path=str(f.get("file_path", f"/tmp/comics/{f['file_name']}")),
            file_name=str(f["file_name"]),
            file_size=int(f.get("file_size", 1024)),
            file_format=str(f.get("file_format", "cbz")),
            parsed_series=f.get("parsed_series"),  # type: ignore[arg-type]
            parsed_issue_number=f.get("parsed_issue_number"),  # type: ignore[arg-type]
            parsed_year=f.get("parsed_year"),  # type: ignore[arg-type]
            has_comicinfo=bool(f.get("has_comicinfo", False)),
            comicvine_issue_id=f.get("comicvine_issue_id"),  # type: ignore[arg-type]
            issue_number_raw=f.get("issue_number_raw"),  # type: ignore[arg-type]
            status=ImportedFileStatus.PENDING,
            diagnostics=f.get("diagnostics", {}),  # type: ignore[arg-type]
        )
        session.add(imp_file)
        imp_files.append(imp_file)

    await session.flush()
    return job, imp_series, imp_files


# ── R-4.1: File Matching Tests ───────────────────────────────────────────


class TestHighConfidenceComicVineId:
    """File with ComicInfo CV issue ID matching a known Issue → HIGH."""

    @pytest.mark.asyncio
    async def test_cv_issue_id_match(self, db_session: AsyncSession) -> None:
        series, issues = await _setup_series_with_issues(db_session)
        job, _imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 001.cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                    "has_comicinfo": True,
                    "comicvine_issue_id": 100001,
                }
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_files[0])
        assert imp_files[0].status == ImportedFileStatus.MATCHED
        assert imp_files[0].matched_issue_id == issues[0].id
        assert imp_files[0].match_confidence == "high"
        assert imp_files[0].match_method == "comicvine_id"


class TestVolumeIssueMatching:
    """Volume-style files should match issue targets when no issue number is parsed."""

    @pytest.mark.asyncio
    async def test_volume_number_matches_issue_number(self, db_session: AsyncSession) -> None:
        series, issues = await _setup_series_with_issues(
            db_session,
            title="Alien By Shalvey & Broccardo",
            year=2023,
            cv_id=154680,
            issues=[
                (1.0, 111001, "Thaw"),
                (2.0, 111002, "Descendant"),
            ],
            series_type=SeriesType.TPB,
        )
        job, _imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": (
                        "Alien by Shalvey & Broccardo v01 - Thaw (2024) "
                        "(Digital) (dekabro-Empire).cbz"
                    ),
                    "parsed_issue_number": None,
                    "parsed_series": "Alien by Shalvey & Broccardo",
                    "parsed_year": 2024,
                    "has_comicinfo": True,
                    "diagnostics": {
                        "source_issue_type": IssueType.VOLUME.value,
                        "metadata_signals": {
                            "issue_type": "release_title",
                            "series_name": "comicinfo",
                        },
                        "source_metadata": {
                            "filename_parse": {
                                "series_name": "Alien by Shalvey & Broccardo - Thaw",
                                "issue_number": None,
                                "year": 2024,
                                "volume": "v01",
                                "issue_type": IssueType.VOLUME.value,
                            },
                            "comicinfo": {
                                "series": "Alien by Shalvey & Broccardo",
                                "number": None,
                                "year": 2024,
                                "title": None,
                            },
                        },
                    },
                },
                {
                    "file_name": (
                        "Alien by Shalvey & Broccardo v02 - Descendant (2024) "
                        "(Digital) (Kileko-Empire).cbz"
                    ),
                    "parsed_issue_number": None,
                    "parsed_series": "Alien by Shalvey & Broccardo",
                    "parsed_year": 2024,
                    "has_comicinfo": True,
                    "diagnostics": {
                        "source_issue_type": IssueType.VOLUME.value,
                        "metadata_signals": {
                            "issue_type": "release_title",
                            "series_name": "comicinfo",
                        },
                        "source_metadata": {
                            "filename_parse": {
                                "series_name": "Alien by Shalvey & Broccardo - Descendant",
                                "issue_number": None,
                                "year": 2024,
                                "volume": "v02",
                                "issue_type": IssueType.VOLUME.value,
                            },
                            "comicinfo": {
                                "series": "Alien by Shalvey & Broccardo",
                                "number": None,
                                "year": 2024,
                                "title": None,
                            },
                        },
                    },
                },
            ],
        )
        _imp_series.diagnostics = {"source_issue_type": IssueType.VOLUME.value}

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        for imp_file in imp_files:
            await db_session.refresh(imp_file)

        assert imp_files[0].status == ImportedFileStatus.MATCHED
        assert imp_files[0].matched_issue_id == issues[0].id
        assert imp_files[0].match_method == "issue_number"
        assert imp_files[1].status == ImportedFileStatus.MATCHED
        assert imp_files[1].matched_issue_id == issues[1].id
        assert imp_files[1].match_method == "issue_number"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        (
            "series_title",
            "cv_id",
            "file_name",
            "parsed_series",
            "parsed_year",
            "target_issue_number",
            "target_issue_id",
            "target_issue_title",
        ),
        [
            (
                "Alien By Shalvey & Broccardo",
                154680,
                "Alien by Shalvey & Broccardo v02 - Descendant (2024) "
                "(Digital) (Kileko-Empire).cbz",
                "Alien by Shalvey & Broccardo",
                2024,
                2.0,
                "111002",
                "Descendant",
            ),
            (
                "Babyteeth",
                102755,
                "Babyteeth v04 - Grave (2020) (Digital) (Zone-Empire).cbz",
                "Babyteeth",
                2020,
                4.0,
                "1166031",
                "Vol. 4: Grave",
            ),
            (
                "Fearscape",
                103001,
                "Fearscape.Vol.01.2019.pdf",
                "Fearscape",
                2019,
                1.0,
                "1030015",
                "Vol. 1",
            ),
            (
                "Fearscape",
                103001,
                "Fearscape.Vol.02.A.Dark.Interlude.2023.pdf",
                "Fearscape A Dark Interlude",
                2023,
                2.0,
                "1030016",
                "Vol. 2: A Dark Interlude",
            ),
        ],
    )
    async def test_provider_volume_files_match_without_persisted_source_diagnostics(
        self,
        db_session: AsyncSession,
        series_title: str,
        cv_id: int,
        file_name: str,
        parsed_series: str,
        parsed_year: int,
        target_issue_number: float,
        target_issue_id: str,
        target_issue_title: str,
    ) -> None:
        provider = AsyncMock()
        provider.get_issues_for_series = AsyncMock(
            return_value=[
                IssueSummary(
                    provider_id=target_issue_id,
                    issue_number=target_issue_number,
                    title=target_issue_title,
                    release_date=None,
                    cover_url=None,
                    issue_type="issue",
                )
            ]
        )
        metadata_service = AsyncMock()
        metadata_service._provider = provider
        svc = ImportService(
            series_service=AsyncMock(),
            metadata_service=metadata_service,
            event_bus=AsyncMock(),
        )
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()
        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name=parsed_series,
            raw_year=parsed_year,
            status=ImportSeriesStatus.MATCHED,
            cv_id=cv_id,
            cv_title=series_title,
            cv_year=parsed_year,
            cv_match_score=1.0,
            cv_match_method="exact_title_year",
            file_count=1,
            diagnostics={},
        )
        db_session.add(imp_series)
        await db_session.flush()
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path=f"/tmp/comics/{file_name}",
            file_name=file_name,
            file_size=1024,
            file_format=file_name.rsplit(".", 1)[-1],
            parsed_series=parsed_series,
            parsed_issue_number=None,
            parsed_year=parsed_year,
            status=ImportedFileStatus.PENDING,
            diagnostics={},
        )
        db_session.add(imp_file)
        await db_session.flush()

        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_file)
        await db_session.refresh(imp_series)
        assert imp_file.status == ImportedFileStatus.MATCHED
        assert imp_file.matched_issue_cv_id == int(target_issue_id)
        assert imp_file.match_method == "issue_number"
        assert imp_series.files_matched == 1
        assert imp_series.files_no_match == 0

    @pytest.mark.asyncio
    async def test_provider_collection_volume_files_ignore_metadata_only_tails(
        self,
        db_session: AsyncSession,
    ) -> None:
        provider = AsyncMock()
        provider.get_issues_for_series = AsyncMock(
            return_value=[
                IssueSummary(
                    provider_id="900001",
                    issue_number=1.0,
                    title="Volume 1: Born",
                    release_date=None,
                    cover_url=None,
                    issue_type="issue",
                ),
                IssueSummary(
                    provider_id="900002",
                    issue_number=2.0,
                    title="Volume 2: Razed",
                    release_date=None,
                    cover_url=None,
                    issue_type="issue",
                ),
                IssueSummary(
                    provider_id="900003",
                    issue_number=3.0,
                    title="Vol. 3: Cradle",
                    release_date=None,
                    cover_url=None,
                    issue_type="issue",
                ),
                IssueSummary(
                    provider_id="900004",
                    issue_number=4.0,
                    title="Vol. 4: Grave",
                    release_date=None,
                    cover_url=None,
                    issue_type="issue",
                ),
            ]
        )
        metadata_service = AsyncMock()
        metadata_service._provider = provider
        svc = ImportService(
            series_service=AsyncMock(),
            metadata_service=metadata_service,
            event_bus=AsyncMock(),
        )
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()
        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Collected Series",
            raw_year=2018,
            status=ImportSeriesStatus.MATCHED,
            cv_id=171891,
            cv_title="Collected Series",
            cv_year=2018,
            cv_match_score=1.0,
            cv_match_method="exact_title_year",
            file_count=4,
            diagnostics={"source_issue_type": IssueType.VOLUME.value},
        )
        db_session.add(imp_series)
        await db_session.flush()

        file_names = [
            "Collected Series v01 (2018) (Digital).cbz",
            "Collected Series v02 (2018) (Digital).cbz",
            "Collected Series v03 - Cradle (2020).cbz",
            "Collected Series Vol. 4: Grave [Digital].cbz",
        ]
        imp_files = []
        for file_name in file_names:
            imp_file = ImportedFile(
                import_job_id=job.id,
                import_series_id=imp_series.id,
                file_path=f"/tmp/comics/{file_name}",
                file_name=file_name,
                file_size=1024,
                file_format="cbz",
                parsed_series="Collected Series",
                parsed_issue_number=None,
                parsed_year=2018,
                status=ImportedFileStatus.PENDING,
                diagnostics={"source_issue_type": IssueType.VOLUME.value},
            )
            db_session.add(imp_file)
            imp_files.append(imp_file)
        await db_session.flush()

        await svc._run_file_matching(db_session, job)

        for imp_file in imp_files:
            await db_session.refresh(imp_file)
        await db_session.refresh(imp_series)

        assert [imp_file.status for imp_file in imp_files] == [
            ImportedFileStatus.MATCHED,
            ImportedFileStatus.MATCHED,
            ImportedFileStatus.MATCHED,
            ImportedFileStatus.MATCHED,
        ]
        assert [imp_file.matched_issue_cv_id for imp_file in imp_files] == [
            900001,
            900002,
            900003,
            900004,
        ]
        assert [imp_file.match_method for imp_file in imp_files] == [
            "issue_number",
            "issue_number",
            "issue_number",
            "issue_number",
        ]
        assert imp_series.files_matched == 4
        assert imp_series.files_no_match == 0

    @pytest.mark.asyncio
    async def test_provider_collection_volume_uses_series_title_for_subtitle_match(
        self,
        db_session: AsyncSession,
    ) -> None:
        provider = AsyncMock()
        provider.get_issues_for_series = AsyncMock(
            return_value=[
                IssueSummary(
                    provider_id="483095",
                    issue_number=1.0,
                    title="Volume 1",
                    release_date=None,
                    cover_url=None,
                    issue_type="issue",
                )
            ]
        )
        metadata_service = AsyncMock()
        metadata_service._provider = provider
        svc = ImportService(
            series_service=AsyncMock(),
            metadata_service=metadata_service,
            event_bus=AsyncMock(),
        )
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()
        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="The United States of Murder Inc",
            raw_year=2015,
            status=ImportSeriesStatus.MATCHED,
            cv_id=80837,
            cv_title="The United States of Murder Inc.: Truth",
            cv_year=2015,
            cv_match_score=1.0,
            cv_match_method="fuzzy_title",
            file_count=1,
            files_total=1,
            diagnostics={"source_issue_type": IssueType.VOLUME.value},
        )
        db_session.add(imp_series)
        await db_session.flush()
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/comics/The United States of Murder Inc. v01 - Truth (2015).cbz",
            file_name="The United States of Murder Inc. v01 - Truth (2015).cbz",
            file_size=1024,
            file_format="cbz",
            parsed_series="The United States of Murder Inc",
            parsed_issue_number=1.0,
            parsed_year=2015,
            status=ImportedFileStatus.PENDING,
            diagnostics={"source_issue_type": IssueType.VOLUME.value},
        )
        db_session.add(imp_file)
        await db_session.flush()

        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_file)
        await db_session.refresh(imp_series)
        assert imp_file.status == ImportedFileStatus.MATCHED
        assert imp_file.matched_issue_cv_id == 483095
        assert imp_file.match_method == "issue_number"
        assert imp_series.files_matched == 1
        assert imp_series.files_no_match == 0

    @pytest.mark.asyncio
    async def test_provider_one_shot_file_matches_single_standard_issue_target(
        self,
        db_session: AsyncSession,
    ) -> None:
        provider = AsyncMock()
        provider.get_issues_for_series = AsyncMock(
            return_value=[
                IssueSummary(
                    provider_id="1202601",
                    issue_number=1.0,
                    title="Murder Drones: Home",
                    release_date=None,
                    cover_url=None,
                    issue_type="issue",
                )
            ]
        )
        metadata_service = AsyncMock()
        metadata_service._provider = provider
        svc = ImportService(
            series_service=AsyncMock(),
            metadata_service=metadata_service,
            event_bus=AsyncMock(),
        )
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()
        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Murder Drones - Home One Shot",
            raw_year=2026,
            status=ImportSeriesStatus.MATCHED,
            cv_id=172008,
            cv_title="Murder Drones: Home",
            cv_year=2026,
            cv_match_score=1.0,
            cv_match_method="exact_title_year",
            file_count=1,
            diagnostics={"source_issue_type": IssueType.ONE_SHOT.value},
        )
        db_session.add(imp_series)
        await db_session.flush()
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path=("/tmp/comics/Murder Drones - Home 001 (OS) (2026) (Digital Rip).cbz"),
            file_name="Murder Drones - Home 001 (OS) (2026) (Digital Rip).cbz",
            file_size=1024,
            file_format="cbz",
            parsed_series="Murder Drones - Home",
            parsed_issue_number=1.0,
            parsed_year=2026,
            status=ImportedFileStatus.PENDING,
            diagnostics={
                "source_issue_type": IssueType.ONE_SHOT.value,
                "source_metadata": {
                    "filename_parse": {
                        "series_name": "Murder Drones - Home",
                        "issue_number": 1.0,
                        "year": 2026,
                        "volume": None,
                        "issue_type": IssueType.ONE_SHOT.value,
                    }
                },
            },
        )
        db_session.add(imp_file)
        await db_session.flush()

        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_file)
        await db_session.refresh(imp_series)
        assert imp_file.status == ImportedFileStatus.MATCHED
        assert imp_file.matched_issue_cv_id == 1202601
        assert imp_file.match_method == "issue_number"
        assert imp_series.files_matched == 1
        assert imp_series.files_no_match == 0


class TestMetadataConflictStopsAutoMatch:
    """Conflicting strong source signals should block import auto-matching."""

    @pytest.mark.asyncio
    async def test_comicinfo_issue_id_conflict_downgrades_to_review(
        self,
        db_session: AsyncSession,
    ) -> None:
        series, _issues = await _setup_series_with_issues(
            db_session,
            title="Chicken Devils",
            year=2022,
            cv_id=145525,
            issues=[(4.0, 905404, "The Chicken is in the Details")],
        )
        job, imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Chicken Devil 004 (2022).cbz",
                    "parsed_issue_number": 4.0,
                    "parsed_series": "Chicken Devil",
                    "parsed_year": 2022,
                    "has_comicinfo": True,
                    "comicvine_issue_id": 905404,
                    "diagnostics": {
                        "metadata_signals": {
                            "series_name": "comicinfo",
                            "comicvine_issue_id": "comicinfo",
                        },
                        "source_metadata": {
                            "has_comicinfo": True,
                            "comicinfo": {
                                "series": "Chicken Devil",
                                "number": "4",
                                "year": 2022,
                                "title": "The Chicken is in the Details",
                                "web": "https://comicvine.gamespot.com/chicken-devil-4-the-chicken-is-in-the-details/4000-905404/",
                            },
                        },
                    },
                }
            ],
        )
        imp_series.raw_series_name = "Chicken Devil"
        imp_series.cv_title = "Chicken Devils"
        imp_series.cv_match_score = 0.9741
        imp_series.cv_match_method = "exact_title_year"
        await db_session.flush()

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_files[0])
        await db_session.refresh(imp_series)
        await db_session.refresh(job)

        assert imp_files[0].status == ImportedFileStatus.NO_MATCH
        assert imp_files[0].matched_issue_id is None
        assert imp_files[0].matched_issue_cv_id is None
        assert imp_files[0].diagnostics["kind"] == "metadata_conflict"
        assert imp_files[0].diagnostics["target_series"] == "Chicken Devils"
        assert imp_files[0].diagnostics["comicinfo"]["series"] == "Chicken Devil"
        assert imp_series.status == ImportSeriesStatus.NO_MATCH
        assert imp_series.diagnostics["reason"] == "file_metadata_conflict"
        assert job.series_no_match >= 1

    @pytest.mark.asyncio
    async def test_archive_page_issue_hint_blocks_wrong_filename_issue_match(
        self,
        db_session: AsyncSession,
    ) -> None:
        series, _issues = await _setup_series_with_issues(
            db_session,
            title="Hello Darkness",
            year=2024,
            cv_id=159025,
            issues=[
                (20.0, 1162482, "Away Message"),
                (21.0, 1166406, "Leading the Witness"),
            ],
        )
        job, imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Hello Darkness 020 (2026).cbz",
                    "parsed_issue_number": 20.0,
                    "parsed_series": "Hello Darkness",
                    "parsed_year": 2026,
                    "diagnostics": {
                        "source_metadata": {
                            "has_comicinfo": False,
                            "archive_entry_issue_hint": {
                                "series_name": "Hello Darkness",
                                "issue_number": 21.0,
                                "year": 2026,
                                "confidence": "strong",
                                "matching_entry_count": 47,
                                "total_image_entries": 48,
                                "parseable_image_entries": 47,
                            },
                        },
                    },
                }
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_files[0])
        await db_session.refresh(imp_series)

        assert imp_files[0].status == ImportedFileStatus.NO_MATCH
        assert imp_files[0].matched_issue_cv_id is None
        assert imp_files[0].diagnostics["kind"] == "metadata_conflict"
        assert imp_files[0].diagnostics["conflict_type"] == "archive_entry_issue_number_mismatch"
        assert imp_files[0].diagnostics["suggested_issue_number"] == 21.0
        assert imp_series.status == ImportSeriesStatus.NO_MATCH
        assert imp_series.cv_id == 159025
        assert imp_series.files_no_match == 1


class TestExplicitIssueSeriesSplit:
    """Collection-shaped files with an explicit issue URL to another series should split out."""

    @pytest.mark.asyncio
    async def test_collection_file_splits_to_own_matched_series(
        self,
        db_session: AsyncSession,
    ) -> None:
        provider = AsyncMock()

        async def _get_issues_for_series(series_provider_id: str) -> list[IssueSummary]:
            if str(series_provider_id) == "162966":
                return [
                    IssueSummary(
                        provider_id="1100110",
                        issue_number=1.0,
                        title="The Red Planet",
                        release_date=None,
                        cover_url=None,
                        issue_type="issue",
                    ),
                    IssueSummary(
                        provider_id="1100111",
                        issue_number=2.0,
                        title="The Green Thought",
                        release_date=None,
                        cover_url=None,
                        issue_type="issue",
                    ),
                ]
            if str(series_provider_id) == "168590":
                return [
                    IssueSummary(
                        provider_id="1144216",
                        issue_number=1.0,
                        title="Vol. 1: Martian Vision",
                        release_date=None,
                        cover_url=None,
                        issue_type="issue",
                    )
                ]
            return []

        provider.get_issues_for_series.side_effect = _get_issues_for_series
        provider.get_issue = AsyncMock(
            return_value=IssueMetadata(
                provider_id="1144216",
                series_provider_id="168590",
                issue_number=1.0,
                title="Vol. 1: Martian Vision",
                description=None,
                release_date=None,
                store_date=None,
                cover_url=None,
                page_count=None,
                comicvine_url="https://comicvine.gamespot.com/absolute-martian-manhunter-1-vol-1-martian-vision/4000-1144216/",
            )
        )
        provider.get_series = AsyncMock(
            return_value=SeriesMetadata(
                provider_id="168590",
                title="Absolute Martian Manhunter",
                sort_title="Absolute Martian Manhunter",
                year_start=2025,
                year_end=None,
                status=None,
                publisher="DC Comics",
                description=None,
                cover_url=None,
                issue_count=1,
                comicvine_url="https://comicvine.gamespot.com/absolute-martian-manhunter/4050-168590/",
            )
        )
        metadata_service = AsyncMock()
        metadata_service._provider = provider
        svc = ImportService(
            series_service=AsyncMock(),
            metadata_service=metadata_service,
            event_bus=AsyncMock(),
        )

        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()

        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Absolute Martian Manhunter",
            raw_year=2025,
            status=ImportSeriesStatus.MATCHED,
            cv_id=162966,
            cv_title="Absolute Martian Manhunter",
            cv_year=2025,
            cv_match_score=1.0,
            cv_match_method="exact_title_year",
            file_count=3,
            files_total=3,
            source_folder="/tmp/comics",
            sample_paths=[
                "/tmp/comics/Absolute Martian Manhunter (2025) #001.cbz",
                "/tmp/comics/Absolute Martian Manhunter (2025) #002.cbz",
                "/tmp/comics/Absolute Martian Manhunter (2025) Vol 01.cbz",
            ],
        )
        db_session.add(imp_series)
        await db_session.flush()

        standard_one = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/comics/Absolute Martian Manhunter (2025) #001.cbz",
            file_name="Absolute Martian Manhunter (2025) #001.cbz",
            file_size=36 * 1024 * 1024,
            file_format="cbz",
            parsed_series="Absolute Martian Manhunter",
            parsed_issue_number=1.0,
            parsed_year=2025,
            has_comicinfo=True,
            comicvine_issue_id=1100110,
            status=ImportedFileStatus.PENDING,
            diagnostics={
                "source_issue_type": IssueType.ISSUE.value,
                "metadata_signals": {
                    "comicvine_issue_id": MetadataSignal.COMICINFO.value,
                    "series_name": MetadataSignal.COMICINFO.value,
                },
                "source_metadata": {
                    "comicinfo": {
                        "series": "Absolute Martian Manhunter",
                        "number": "1",
                        "year": 2025,
                        "title": "The Red Planet",
                        "web": "https://comicvine.gamespot.com/absolute-martian-manhunter-1/4000-1100110/",
                    }
                },
            },
        )
        standard_two = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/comics/Absolute Martian Manhunter (2025) #002.cbz",
            file_name="Absolute Martian Manhunter (2025) #002.cbz",
            file_size=37 * 1024 * 1024,
            file_format="cbz",
            parsed_series="Absolute Martian Manhunter",
            parsed_issue_number=2.0,
            parsed_year=2025,
            has_comicinfo=True,
            comicvine_issue_id=1100111,
            status=ImportedFileStatus.PENDING,
            diagnostics={
                "source_issue_type": IssueType.ISSUE.value,
                "metadata_signals": {
                    "comicvine_issue_id": MetadataSignal.COMICINFO.value,
                    "series_name": MetadataSignal.COMICINFO.value,
                },
                "source_metadata": {
                    "comicinfo": {
                        "series": "Absolute Martian Manhunter",
                        "number": "2",
                        "year": 2025,
                        "title": "The Green Thought",
                        "web": "https://comicvine.gamespot.com/absolute-martian-manhunter-2/4000-1100111/",
                    }
                },
            },
        )
        volume_one = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/comics/Absolute Martian Manhunter (2025) Vol 01.cbz",
            file_name="Absolute Martian Manhunter (2025) Vol 01.cbz",
            file_size=389 * 1024 * 1024,
            file_format="cbz",
            parsed_series="Absolute Martian Manhunter",
            parsed_issue_number=None,
            parsed_year=2025,
            has_comicinfo=True,
            comicvine_issue_id=1144216,
            status=ImportedFileStatus.PENDING,
            diagnostics={
                "source_issue_type": IssueType.VOLUME.value,
                "metadata_signals": {
                    "comicvine_issue_id": MetadataSignal.COMICINFO.value,
                    "series_name": MetadataSignal.COMICINFO.value,
                    "issue_type": MetadataSignal.RELEASE_TITLE.value,
                },
                "source_metadata": {
                    "comicinfo": {
                        "series": "Absolute Martian Manhunter",
                        "number": "1",
                        "year": 2025,
                        "title": "Vol. 1: Martian Vision",
                        "web": "https://comicvine.gamespot.com/absolute-martian-manhunter-1-vol-1-martian-vision/4000-1144216/",
                    },
                    "filename_parse": {
                        "series_name": "Absolute Martian Manhunter",
                        "issue_number": None,
                        "year": 2025,
                        "volume": "Vol 01",
                        "issue_type": IssueType.VOLUME.value,
                    },
                },
            },
        )
        db_session.add_all([standard_one, standard_two, volume_one])
        await db_session.flush()

        await svc._run_file_matching(db_session, job)

        await db_session.refresh(job)
        await db_session.refresh(imp_series)
        await db_session.refresh(standard_one)
        await db_session.refresh(standard_two)
        await db_session.refresh(volume_one)

        series_rows = list(
            (
                await db_session.execute(
                    select(ImportedSeries)
                    .where(ImportedSeries.import_job_id == job.id)
                    .order_by(ImportedSeries.id.asc())
                )
            )
            .scalars()
            .all()
        )

        assert len(series_rows) == 2
        split_series = next(row for row in series_rows if row.id != imp_series.id)

        assert standard_one.status == ImportedFileStatus.MATCHED
        assert standard_one.matched_issue_cv_id == 1100110
        assert standard_two.status == ImportedFileStatus.MATCHED
        assert standard_two.matched_issue_cv_id == 1100111

        assert volume_one.status == ImportedFileStatus.MATCHED
        assert volume_one.matched_issue_cv_id == 1144216
        assert volume_one.import_series_id == split_series.id

        assert imp_series.cv_id == 162966
        assert imp_series.files_total == 2
        assert imp_series.files_matched == 2
        assert imp_series.files_no_match == 0

        assert split_series.status == ImportSeriesStatus.MATCHED
        assert split_series.cv_id == 168590
        assert split_series.cv_match_method == "explicit_issue_series_split"
        assert split_series.files_total == 1
        assert split_series.files_matched == 1
        assert split_series.files_no_match == 0

        assert job.total_files_matched == 3
        assert job.total_files_no_match == 0


class TestHighConfidenceIssueNumber:
    """Parsed issue number exactly matches an Issue → HIGH."""

    @pytest.mark.asyncio
    async def test_issue_number_match_high(self, db_session: AsyncSession) -> None:
        series, issues = await _setup_series_with_issues(db_session)
        job, _imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 002.cbz",
                    "parsed_issue_number": 2.0,
                    "parsed_series": "Batman",
                }
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_files[0])
        assert imp_files[0].status == ImportedFileStatus.MATCHED
        assert imp_files[0].matched_issue_id == issues[1].id
        assert imp_files[0].match_confidence == "high"
        assert imp_files[0].match_method == "issue_number"


class TestMediumConfidenceIssueNumber:
    """Parsed issue number matches but series confidence is MEDIUM → MEDIUM."""

    @pytest.mark.asyncio
    async def test_issue_number_match_medium(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(db_session)
        job, imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 001.cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                }
            ],
        )
        # Lower the series match score to indicate medium confidence
        imp_series.cv_match_score = 0.75
        await db_session.flush()

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_files[0])
        assert imp_files[0].status == ImportedFileStatus.MATCHED
        assert imp_files[0].match_confidence == "medium"
        assert imp_files[0].match_method == "issue_number"


class TestNoMatch:
    """File cannot be matched to any issue in the series."""

    @pytest.mark.asyncio
    async def test_no_matching_issue(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(db_session)
        job, _imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 099.cbz",
                    "parsed_issue_number": 99.0,
                    "parsed_series": "Batman",
                }
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_files[0])
        assert imp_files[0].status == ImportedFileStatus.NO_MATCH
        assert imp_files[0].matched_issue_id is None

    @pytest.mark.asyncio
    async def test_unparseable_filename(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(db_session)
        job, _imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "scan_0042.cbz",
                    "parsed_issue_number": None,
                    "parsed_series": None,
                }
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_files[0])
        assert imp_files[0].status == ImportedFileStatus.NO_MATCH


class TestFileMatchingCounters:
    """File matching updates counters on ImportedSeries and ImportJob."""

    @pytest.mark.asyncio
    async def test_series_counters_updated(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(db_session)
        job, imp_series, _imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 001.cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                },
                {
                    "file_name": "Batman 002.cbz",
                    "parsed_issue_number": 2.0,
                    "parsed_series": "Batman",
                },
                {"file_name": "scan_0042.cbz", "parsed_issue_number": None, "parsed_series": None},
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_series)
        assert imp_series.files_total == 3
        assert imp_series.files_matched == 2
        assert imp_series.files_no_match == 1

    @pytest.mark.asyncio
    async def test_job_counters_updated(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(db_session)
        job, _imp_series, _imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 001.cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                },
                {"file_name": "scan_0042.cbz", "parsed_issue_number": None, "parsed_series": None},
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(job)
        assert job.total_files_found == 2
        assert job.total_files_matched == 1
        assert job.total_files_no_match == 1


class TestFileMatchingJobStatus:
    """FILE_MATCHING status is used during file matching phase."""

    @pytest.mark.asyncio
    async def test_job_status_is_file_matching(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(db_session)
        job, _imp_series, _imp_files = await _setup_import_job(db_session, series)

        # Job starts in FILE_MATCHING
        assert job.status == ImportJobStatus.FILE_MATCHING

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        # Method doesn't change status — that's the caller's responsibility
        await db_session.refresh(job)
        assert job.status == ImportJobStatus.FILE_MATCHING


class TestProgressEvents:
    """Progress events are emitted during file matching."""

    @pytest.mark.asyncio
    async def test_progress_callback_called(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(db_session)
        job, _imp_series, _imp_files = await _setup_import_job(db_session, series)

        callback = AsyncMock()
        svc = _make_service()
        await svc._run_file_matching(db_session, job, progress_callback=callback)

        assert callback.call_count >= 1
        event = callback.call_args_list[-1][0][0]
        assert event.phase == "file_matching"

    @pytest.mark.asyncio
    async def test_provider_target_loading_emits_heartbeat(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            import_file_matching_progress,
            "_FILE_MATCH_PROGRESS_HEARTBEAT_SECONDS",
            0.01,
        )
        release_provider = asyncio.Event()
        progress_events: list[ImportProgressEvent] = []

        class ProviderDouble:
            async def get_issues_for_series_by_numbers(
                self,
                _series_provider_id: str,
                _issue_numbers: list[float],
            ) -> list[IssueSummary]:
                await release_provider.wait()
                return [
                    IssueSummary(
                        provider_id="100001",
                        issue_number=1.0,
                        title="I Am Gotham Part 1",
                        release_date=None,
                        cover_url=None,
                        issue_type="issue",
                    )
                ]

        metadata_service = AsyncMock()
        metadata_service._provider = ProviderDouble()
        svc = ImportService(
            series_service=AsyncMock(),
            metadata_service=metadata_service,
            event_bus=AsyncMock(),
        )
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
            scan_started_at=datetime.now(UTC),
        )
        db_session.add(job)
        await db_session.flush()
        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Batman",
            raw_year=2016,
            status=ImportSeriesStatus.MATCHED,
            cv_id=97508,
            cv_title="Batman",
            cv_year=2016,
            cv_issue_count=85,
            cv_match_score=0.95,
            cv_match_method="exact_title_year",
            file_count=1,
            files_total=1,
        )
        db_session.add(imp_series)
        await db_session.flush()
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/comics/Batman 001.cbz",
            file_name="Batman 001.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_series="Batman",
            parsed_issue_number=1.0,
            parsed_year=2016,
            has_comicinfo=False,
            status=ImportedFileStatus.PENDING,
            diagnostics={},
        )
        db_session.add(imp_file)
        await db_session.flush()

        async def progress_callback(event: ImportProgressEvent) -> None:
            progress_events.append(event)
            if str(event.message).startswith("Still loading issue targets for Batman"):
                release_provider.set()

        await asyncio.wait_for(
            svc._run_file_matching(
                db_session,
                job,
                progress_callback=progress_callback,
            ),
            timeout=1,
        )

        messages = [str(event.message) for event in progress_events]
        assert "Loading issue targets for Batman (series 1/1)..." in messages
        assert any(
            message.startswith("Still loading issue targets for Batman")
            and "Large series can take a few minutes." in message
            for message in messages
        )
        heartbeat = next(
            event
            for event in progress_events
            if str(event.message).startswith("Still loading issue targets for Batman")
        )
        assert heartbeat.phase == "file_matching"
        assert heartbeat.status == ImportJobStatus.FILE_MATCHING
        assert heartbeat.current_series == "Batman"

    @pytest.mark.asyncio
    async def test_file_matching_current_item_progress_is_series_local(
        self,
        db_session: AsyncSession,
    ) -> None:
        series, _issues = await _setup_series_with_issues(db_session)
        job, _imp_series, _imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 001.cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                },
                {
                    "file_name": "Batman 002.cbz",
                    "parsed_issue_number": 2.0,
                    "parsed_series": "Batman",
                },
            ],
        )
        progress_events: list[ImportProgressEvent] = []

        async def progress_callback(event: ImportProgressEvent) -> None:
            progress_events.append(event)

        svc = _make_service()
        await svc._run_file_matching(db_session, job, progress_callback=progress_callback)

        matching_started = next(
            event
            for event in progress_events
            if event.message == "Matching files to issues for Batman (2 files)..."
        )
        matching_completed = next(
            event for event in progress_events if event.message == "Prepared file review for Batman"
        )
        matched_first = next(
            event for event in progress_events if event.message == "Matched file 1/2 for Batman"
        )
        target_loading = next(
            event
            for event in progress_events
            if event.message == "Loading issue targets for Batman (series 1/1)..."
        )

        assert target_loading.current_item_progress_pct == 5
        assert matching_started.current_item_progress_pct == 50
        assert matched_first.current_item_progress_pct == 75
        assert matching_completed.progress < 99
        assert matching_completed.current_item_progress_pct == 100
        assert progress_events[-1].message == "File review summaries ready"
        assert progress_events[-1].progress == 99


class TestMultipleSeriesFileMatching:
    """File matching processes all matched series in the job."""

    @pytest.mark.asyncio
    async def test_matches_across_series(self, db_session: AsyncSession) -> None:
        series1, issues1 = await _setup_series_with_issues(
            db_session,
            title="Batman",
            cv_id=97508,
            issues=[(1.0, 100001, "Issue 1")],
        )
        series2, issues2 = await _setup_series_with_issues(
            db_session,
            title="Superman",
            cv_id=97509,
            issues=[(1.0, 200001, "Issue 1")],
            publisher_id=series1.publisher_id,
        )

        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()

        # Series 1
        imp_s1 = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Batman",
            raw_year=2016,
            status=ImportSeriesStatus.MATCHED,
            cv_id=97508,
            cv_match_score=0.95,
            series_id=series1.id,
            file_count=1,
        )
        db_session.add(imp_s1)
        await db_session.flush()
        f1 = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_s1.id,
            file_path="/tmp/Batman 001.cbz",
            file_name="Batman 001.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_issue_number=1.0,
            parsed_series="Batman",
        )
        db_session.add(f1)

        # Series 2
        imp_s2 = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Superman",
            raw_year=2016,
            status=ImportSeriesStatus.MATCHED,
            cv_id=97509,
            cv_match_score=0.90,
            series_id=series2.id,
            file_count=1,
        )
        db_session.add(imp_s2)
        await db_session.flush()
        f2 = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_s2.id,
            file_path="/tmp/Superman 001.cbz",
            file_name="Superman 001.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_issue_number=1.0,
            parsed_series="Superman",
        )
        db_session.add(f2)
        await db_session.flush()

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(f1)
        await db_session.refresh(f2)
        assert f1.status == ImportedFileStatus.MATCHED
        assert f2.status == ImportedFileStatus.MATCHED
        assert f1.matched_issue_id == issues1[0].id
        assert f2.matched_issue_id == issues2[0].id

        await db_session.refresh(job)
        assert job.total_files_found == 2
        assert job.total_files_matched == 2


class TestNewSeriesSingleIssueFallback:
    """Single-issue provider summaries can match collection files without issue numbers."""

    @pytest.mark.asyncio
    async def test_high_confidence_single_issue_series_matches_file_without_issue_number(
        self, db_session: AsyncSession
    ) -> None:
        provider = AsyncMock()
        provider.get_issues_for_series = AsyncMock(
            return_value=[
                IssueSummary(
                    provider_id="165113",
                    issue_number=1.0,
                    title="Wasted Space: The Cosmic Collection",
                    release_date=None,
                    cover_url=None,
                    issue_type="issue",
                )
            ]
        )
        metadata_service = AsyncMock()
        metadata_service._provider = provider
        svc = ImportService(
            series_service=AsyncMock(),
            metadata_service=metadata_service,
            event_bus=AsyncMock(),
        )
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()
        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Wasted Space The Cosmic Collection",
            raw_year=2023,
            status=ImportSeriesStatus.MATCHED,
            cv_id=165113,
            cv_title="Wasted Space: The Cosmic Collection",
            cv_year=2023,
            cv_match_score=1.0,
            cv_match_method="exact_title_year",
            file_count=1,
        )
        db_session.add(imp_series)
        await db_session.flush()
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/comics/Wasted.Space.The.Cosmic.Collection.2023.pdf",
            file_name="Wasted.Space.The.Cosmic.Collection.2023.pdf",
            file_size=301265442,
            file_format="pdf",
            parsed_series="Wasted Space The Cosmic Collection",
            parsed_issue_number=None,
            parsed_year=2023,
            status=ImportedFileStatus.PENDING,
        )
        db_session.add(imp_file)
        await db_session.flush()

        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_series)
        await db_session.refresh(imp_file)
        assert imp_series.status == ImportSeriesStatus.MATCHED
        assert imp_series.files_matched == 1
        assert imp_series.files_no_match == 0
        assert imp_file.status == ImportedFileStatus.MATCHED
        assert imp_file.matched_issue_cv_id == 165113
        assert imp_file.match_method == "single_issue_series"

    @pytest.mark.asyncio
    async def test_single_issue_annual_series_matches_file_without_issue_number(
        self, db_session: AsyncSession
    ) -> None:
        provider = AsyncMock()
        provider.get_issues_for_series = AsyncMock(
            return_value=[
                IssueSummary(
                    provider_id="1164378",
                    issue_number=1.0,
                    title=None,
                    release_date=None,
                    cover_url=None,
                    issue_type="issue",
                )
            ]
        )
        metadata_service = AsyncMock()
        metadata_service._provider = provider
        svc = ImportService(
            series_service=AsyncMock(),
            metadata_service=metadata_service,
            event_bus=AsyncMock(),
        )
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()
        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Street Sharks: Annual 2026",
            raw_year=2026,
            raw_publisher="IDW",
            status=ImportSeriesStatus.MATCHED,
            cv_id=171697,
            cv_title="Street Sharks Annual",
            cv_year=2026,
            cv_issue_count=1,
            cv_match_score=1.0,
            cv_match_method="alternate_release_candidate",
            file_count=1,
        )
        db_session.add(imp_series)
        await db_session.flush()
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/comics/Street Sharks - Annual 2026 (2026).cbz",
            file_name="Street Sharks - Annual 2026 (2026) (Digital) (Pyrate-DCP).cbz",
            file_size=1024,
            file_format="cbz",
            parsed_series="Street Sharks: Annual 2026",
            parsed_issue_number=None,
            parsed_year=2026,
            has_comicinfo=True,
            status=ImportedFileStatus.PENDING,
            diagnostics={
                "source_issue_type": IssueType.ANNUAL.value,
                "metadata_signals": {
                    "issue_type": MetadataSignal.RELEASE_TITLE.value,
                    "series_name": MetadataSignal.COMICINFO.value,
                    "publisher": MetadataSignal.COMICINFO.value,
                },
                "source_metadata": {
                    "filename_parse": {
                        "series_name": "Street Sharks",
                        "issue_number": None,
                        "year": 2026,
                        "volume": None,
                        "issue_type": IssueType.ANNUAL.value,
                    },
                    "has_comicinfo": True,
                    "comicinfo": {
                        "series": "Street Sharks: Annual 2026",
                        "number": None,
                        "volume": None,
                        "year": 2026,
                        "title": None,
                        "publisher": "IDW",
                        "web": None,
                    },
                },
            },
        )
        db_session.add(imp_file)
        await db_session.flush()

        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_series)
        await db_session.refresh(imp_file)
        assert imp_series.status == ImportSeriesStatus.MATCHED
        assert imp_series.files_matched == 1
        assert imp_series.files_no_match == 0
        assert imp_file.status == ImportedFileStatus.MATCHED
        assert imp_file.matched_issue_cv_id == 1164378
        assert imp_file.match_method == "single_issue_series"

    @pytest.mark.asyncio
    async def test_single_issue_collection_volume_subtitle_matches_only_issue(
        self, db_session: AsyncSession
    ) -> None:
        provider = AsyncMock()
        provider.get_issues_for_series = AsyncMock(
            return_value=[
                IssueSummary(
                    provider_id="1224101",
                    issue_number=1.0,
                    title="Spider-Chase",
                    release_date=None,
                    cover_url=None,
                    issue_type="issue",
                )
            ]
        )
        metadata_service = AsyncMock()
        metadata_service._provider = provider
        svc = ImportService(
            series_service=AsyncMock(),
            metadata_service=metadata_service,
            event_bus=AsyncMock(),
        )
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()
        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Marvel Action: Spider-Man: Spider-Chase",
            raw_year=2019,
            status=ImportSeriesStatus.MATCHED,
            cv_id=122410,
            cv_title="Marvel Action: Spider-Man: Spider-Chase",
            cv_year=2019,
            cv_issue_count=1,
            cv_match_score=1.0,
            cv_match_method="volume_subtitle_series_match",
            file_count=1,
        )
        db_session.add(imp_series)
        await db_session.flush()
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/comics/Marvel Action Spider-Man v02 - Spider-Chase.cbr",
            file_name="Marvel Action Spider-Man v02 - Spider-Chase (2019) (Digital).cbr",
            file_size=1024,
            file_format="cbr",
            parsed_series="Marvel Action Spider-Man",
            parsed_issue_number=2.0,
            parsed_year=2019,
            status=ImportedFileStatus.PENDING,
            diagnostics={"source_issue_type": IssueType.VOLUME.value},
        )
        db_session.add(imp_file)
        await db_session.flush()

        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_series)
        await db_session.refresh(imp_file)
        assert imp_series.status == ImportSeriesStatus.MATCHED
        assert imp_series.files_matched == 1
        assert imp_series.files_no_match == 0
        assert imp_file.status == ImportedFileStatus.MATCHED
        assert imp_file.matched_issue_cv_id == 1224101
        assert imp_file.match_method == "single_issue_collection_volume_subtitle"

    @pytest.mark.asyncio
    async def test_single_issue_collection_volume_subtitle_rejects_wrong_volume(
        self, db_session: AsyncSession
    ) -> None:
        provider = AsyncMock()
        provider.get_issues_for_series = AsyncMock(
            return_value=[
                IssueSummary(
                    provider_id="1249311",
                    issue_number=1.0,
                    title="Bad Luck",
                    release_date=None,
                    cover_url=None,
                    issue_type="issue",
                )
            ]
        )
        metadata_service = AsyncMock()
        metadata_service._provider = provider
        svc = ImportService(
            series_service=AsyncMock(),
            metadata_service=metadata_service,
            event_bus=AsyncMock(),
        )
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()
        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Marvel Action: Spider-Man: Bad Luck",
            raw_year=2020,
            status=ImportSeriesStatus.MATCHED,
            cv_id=124931,
            cv_title="Marvel Action: Spider-Man: Bad Luck",
            cv_year=2020,
            cv_issue_count=1,
            cv_match_score=1.0,
            cv_match_method="volume_subtitle_series_match",
            file_count=1,
        )
        db_session.add(imp_series)
        await db_session.flush()
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/comics/Marvel Action Spider-Man v02 - Spider-Chase.cbr",
            file_name="Marvel Action Spider-Man v02 - Spider-Chase (2019) (Digital).cbr",
            file_size=1024,
            file_format="cbr",
            parsed_series="Marvel Action Spider-Man",
            parsed_issue_number=2.0,
            parsed_year=2019,
            status=ImportedFileStatus.PENDING,
            diagnostics={"source_issue_type": IssueType.VOLUME.value},
        )
        db_session.add(imp_file)
        await db_session.flush()

        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_series)
        await db_session.refresh(imp_file)
        assert imp_series.status == ImportSeriesStatus.NO_MATCH
        assert imp_series.files_matched == 0
        assert imp_series.files_no_match == 1
        assert imp_file.status == ImportedFileStatus.NO_MATCH

    @pytest.mark.asyncio
    async def test_single_issue_embedded_number_title_matches_only_issue(
        self, db_session: AsyncSession
    ) -> None:
        provider = AsyncMock()
        provider.get_issues_for_series = AsyncMock(
            return_value=[
                IssueSummary(
                    provider_id="730001",
                    issue_number=1.0,
                    title="Issue 1",
                    release_date=None,
                    cover_url=None,
                    issue_type="issue",
                )
            ]
        )
        metadata_service = AsyncMock()
        metadata_service._provider = provider
        svc = ImportService(
            series_service=AsyncMock(),
            metadata_service=metadata_service,
            event_bus=AsyncMock(),
        )
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()
        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="He-Man and the Masters of the Universe - Episode - Captured",
            raw_year=2008,
            status=ImportSeriesStatus.MATCHED,
            cv_id=158984,
            cv_title="He-Man and the Masters of the Universe: Episode 40 - Captured",
            cv_year=2008,
            cv_issue_count=1,
            cv_match_score=0.9314,
            cv_match_method="fuzzy_title",
            file_count=1,
        )
        db_session.add(imp_series)
        await db_session.flush()
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/comics/He-Man Episode 40 Captured.cbr",
            file_name=(
                "He-Man and the Masters of the Universe - Episode 40 - Captured "
                "(2008) (digital) (atomicmass77-DCP).cbr"
            ),
            file_size=1024,
            file_format="cbr",
            parsed_series="He-Man and the Masters of the Universe - Episode - Captured",
            parsed_issue_number=40.0,
            parsed_year=2008,
            status=ImportedFileStatus.PENDING,
            diagnostics={"source_issue_type": IssueType.ISSUE.value},
        )
        db_session.add(imp_file)
        await db_session.flush()

        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_series)
        await db_session.refresh(imp_file)
        assert imp_series.status == ImportSeriesStatus.MATCHED
        assert imp_series.files_matched == 1
        assert imp_series.files_no_match == 0
        assert imp_file.status == ImportedFileStatus.MATCHED
        assert imp_file.matched_issue_cv_id == 730001
        assert imp_file.match_method == "single_issue_embedded_number_title"

    @pytest.mark.asyncio
    async def test_single_issue_embedded_number_title_rejects_plain_issue_number(
        self, db_session: AsyncSession
    ) -> None:
        provider = AsyncMock()
        provider.get_issues_for_series = AsyncMock(
            return_value=[
                IssueSummary(
                    provider_id="1900401",
                    issue_number=1.0,
                    title="Issue 1",
                    release_date=None,
                    cover_url=None,
                    issue_type="issue",
                )
            ]
        )
        metadata_service = AsyncMock()
        metadata_service._provider = provider
        svc = ImportService(
            series_service=AsyncMock(),
            metadata_service=metadata_service,
            event_bus=AsyncMock(),
        )
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()
        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Batman",
            raw_year=2026,
            status=ImportSeriesStatus.MATCHED,
            cv_id=190040,
            cv_title="Batman 40 - Anniversary",
            cv_year=2026,
            cv_issue_count=1,
            cv_match_score=0.95,
            cv_match_method="fuzzy_title",
            file_count=1,
        )
        db_session.add(imp_series)
        await db_session.flush()
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/comics/Batman 040.cbz",
            file_name="Batman 040 (2026) (Digital).cbz",
            file_size=1024,
            file_format="cbz",
            parsed_series="Batman",
            parsed_issue_number=40.0,
            parsed_year=2026,
            status=ImportedFileStatus.PENDING,
            diagnostics={"source_issue_type": IssueType.ISSUE.value},
        )
        db_session.add(imp_file)
        await db_session.flush()

        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_series)
        await db_session.refresh(imp_file)
        assert imp_series.status == ImportSeriesStatus.NO_MATCH
        assert imp_series.files_matched == 0
        assert imp_series.files_no_match == 1
        assert imp_file.status == ImportedFileStatus.NO_MATCH

    @pytest.mark.asyncio
    async def test_provider_backed_series_pauses_when_issue_targets_unavailable(
        self, db_session: AsyncSession
    ) -> None:
        provider = AsyncMock()
        provider.get_issues_for_series = AsyncMock(return_value=[])
        metadata_service = AsyncMock()
        metadata_service._provider = provider
        svc = ImportService(
            series_service=AsyncMock(),
            metadata_service=metadata_service,
            event_bus=AsyncMock(),
        )
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()
        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="The Banks",
            raw_year=2019,
            status=ImportSeriesStatus.MATCHED,
            cv_id=122775,
            cv_title="The Banks",
            cv_year=2019,
            cv_match_score=1.0,
            cv_match_method="exact_title_year",
            file_count=1,
        )
        db_session.add(imp_series)
        await db_session.flush()
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/comics/The Banks (2019) #001.cbr",
            file_name="The Banks (2019) #001.cbr",
            file_size=1024,
            file_format="cbr",
            parsed_series="The Banks",
            parsed_issue_number=1.0,
            parsed_year=2019,
            status=ImportedFileStatus.PENDING,
        )
        db_session.add(imp_file)
        await db_session.flush()

        with pytest.raises(JobPausedError, match="issue targets were unavailable"):
            await svc._run_file_matching(db_session, job)

        await db_session.refresh(job)
        await db_session.refresh(imp_series)
        await db_session.refresh(imp_file)
        assert job.error_message is not None
        assert "issue targets were unavailable" in job.error_message
        assert imp_series.status == ImportSeriesStatus.MATCHED
        assert imp_file.status == ImportedFileStatus.PENDING
        logs = (
            (
                await db_session.execute(
                    select(ImportJobLog).where(ImportJobLog.import_job_id == job.id)
                )
            )
            .scalars()
            .all()
        )
        assert any(log.event == "import_file_matching_provider_degraded" for log in logs)

    @pytest.mark.asyncio
    async def test_provider_backed_zero_issue_special_uses_placeholder_issue_target(
        self, db_session: AsyncSession
    ) -> None:
        class EmptyIssueProvider:
            async def get_issues_for_series(self, _series_provider_id: str) -> list[IssueSummary]:
                return []

        provider = EmptyIssueProvider()
        metadata_service = AsyncMock()
        metadata_service._provider = provider
        svc = ImportService(
            series_service=AsyncMock(),
            metadata_service=metadata_service,
            event_bus=AsyncMock(),
        )
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()
        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Flash Gordon - The 1995 Special",
            raw_year=2026,
            status=ImportSeriesStatus.MATCHED,
            cv_id=172262,
            cv_title="Flash Gordon: The 1995 Special",
            cv_year=None,
            cv_issue_count=0,
            cv_match_score=0.7,
            cv_match_method="exact_title_year",
            file_count=1,
            diagnostics={"source_issue_type": IssueType.SPECIAL.value},
        )
        db_session.add(imp_series)
        await db_session.flush()
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/comics/Flash Gordon - The 1995 Special (2026).cbr",
            file_name="Flash Gordon - The 1995 Special (2026).cbr",
            file_size=1024,
            file_format="cbr",
            parsed_series="Flash Gordon - The 1995",
            parsed_issue_number=None,
            parsed_year=2026,
            status=ImportedFileStatus.PENDING,
            diagnostics={
                "source_issue_type": IssueType.SPECIAL.value,
                "metadata_signals": {"issue_type": MetadataSignal.RELEASE_TITLE.value},
                "source_metadata": {
                    "filename_parse": {
                        "series_name": "Flash Gordon - The 1995",
                        "issue_number": None,
                        "year": 2026,
                        "volume": None,
                        "issue_type": IssueType.SPECIAL.value,
                    },
                },
            },
        )
        db_session.add(imp_file)
        await db_session.flush()

        await svc._run_file_matching(db_session, job)

        await db_session.refresh(job)
        await db_session.refresh(imp_series)
        await db_session.refresh(imp_file)
        assert job.error_message is None
        assert imp_file.status == ImportedFileStatus.MATCHED
        assert imp_file.include_in_import is False
        assert imp_file.matched_issue_id is None
        assert imp_file.matched_issue_cv_id is None
        assert imp_file.match_method == "provider_zero_issue_single_issue"
        assert imp_file.diagnostics["kind"] == "provider_zero_issue_placeholder"
        assert imp_file.diagnostics["target_series_issue_count"] == 0
        assert imp_file.diagnostics["target_issue_number"] == 1.0
        assert imp_file.diagnostics["target_issue_type"] == IssueType.SPECIAL.value
        assert imp_series.status == ImportSeriesStatus.MATCHED
        assert imp_series.files_matched == 1
        assert imp_series.files_no_match == 0
        assert job.total_files_matched == 1
        assert job.total_files_no_match == 0

    @pytest.mark.asyncio
    async def test_provider_backed_zero_issue_standard_series_still_marks_file_no_match(
        self, db_session: AsyncSession
    ) -> None:
        class EmptyIssueProvider:
            async def get_issues_for_series(self, _series_provider_id: str) -> list[IssueSummary]:
                return []

        metadata_service = AsyncMock()
        metadata_service._provider = EmptyIssueProvider()
        svc = ImportService(
            series_service=AsyncMock(),
            metadata_service=metadata_service,
            event_bus=AsyncMock(),
        )
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()
        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="The Banks",
            raw_year=2019,
            status=ImportSeriesStatus.MATCHED,
            cv_id=122775,
            cv_title="The Banks",
            cv_year=2019,
            cv_issue_count=0,
            cv_match_score=1.0,
            cv_match_method="exact_title_year",
            file_count=1,
        )
        db_session.add(imp_series)
        await db_session.flush()
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/comics/The Banks (2019).cbr",
            file_name="The Banks (2019).cbr",
            file_size=1024,
            file_format="cbr",
            parsed_series="The Banks",
            parsed_issue_number=None,
            parsed_year=2019,
            status=ImportedFileStatus.PENDING,
        )
        db_session.add(imp_file)
        await db_session.flush()

        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_series)
        await db_session.refresh(imp_file)
        assert imp_file.status == ImportedFileStatus.NO_MATCH
        assert imp_file.diagnostics["kind"] == "provider_issue_target_missing"
        assert imp_series.status == ImportSeriesStatus.NO_MATCH

    @pytest.mark.asyncio
    async def test_provider_backed_series_marks_files_no_match_when_requested_issue_absent(
        self, db_session: AsyncSession
    ) -> None:
        class TargetedEmptyProvider:
            requested_issue_numbers: list[float] | None = None

            async def get_issues_for_series_by_numbers(
                self, _series_provider_id: str, issue_numbers: list[float]
            ) -> list[IssueSummary]:
                self.requested_issue_numbers = issue_numbers
                return []

            async def get_issues_for_series(self, _series_provider_id: str) -> list[IssueSummary]:
                raise AssertionError("targeted issue lookup should avoid full series fetch")

        provider = TargetedEmptyProvider()
        metadata_service = AsyncMock()
        metadata_service._provider = provider
        svc = ImportService(
            series_service=AsyncMock(),
            metadata_service=metadata_service,
            event_bus=AsyncMock(),
        )
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()
        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="King Dracula",
            raw_year=2026,
            status=ImportSeriesStatus.MATCHED,
            cv_id=169964,
            cv_title="King Dracula",
            cv_year=2025,
            cv_issue_count=3,
            cv_match_score=0.85,
            cv_match_method="exact_title_year",
            file_count=1,
        )
        db_session.add(imp_series)
        await db_session.flush()
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/comics/King Dracula 04 (of 04) (2026).cbr",
            file_name="King Dracula 04 (of 04) (2026).cbr",
            file_size=1024,
            file_format="cbr",
            parsed_series="King Dracula",
            parsed_issue_number=4.0,
            parsed_year=2026,
            status=ImportedFileStatus.PENDING,
        )
        db_session.add(imp_file)
        await db_session.flush()

        await svc._run_file_matching(db_session, job)

        await db_session.refresh(job)
        await db_session.refresh(imp_series)
        await db_session.refresh(imp_file)
        assert imp_file.status == ImportedFileStatus.NO_MATCH
        assert imp_file.include_in_import is False
        assert imp_file.diagnostics["kind"] == "provider_issue_target_missing"
        assert imp_file.diagnostics["requested_issue_number"] == 4.0
        assert imp_series.status == ImportSeriesStatus.NO_MATCH
        assert imp_series.files_no_match == 1
        assert job.series_no_match >= 1
        assert provider.requested_issue_numbers == [4.0]


# ── R-4.2: Conflict Detection Tests ─────────────────────────────────────


class TestConflictDetection:
    """Two files matching the same issue are grouped into a conflict."""

    @pytest.mark.asyncio
    async def test_two_files_same_issue(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(db_session)
        job, _imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 001.cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                    "file_size": 2048,
                },
                {
                    "file_name": "Batman 001 (2).cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                    "file_size": 1024,
                },
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_files[0])
        await db_session.refresh(imp_files[1])
        assert imp_files[0].status == ImportedFileStatus.CONFLICT
        assert imp_files[1].status == ImportedFileStatus.CONFLICT
        assert imp_files[0].conflict_group_id is not None
        assert imp_files[0].conflict_group_id == imp_files[1].conflict_group_id
        assert imp_files[0].diagnostics["kind"] == "file_conflict"
        assert imp_files[0].diagnostics["preferred_file_id"] == imp_files[0].id
        assert imp_files[1].diagnostics["why_not_selected"]

        logs = (
            (
                await db_session.execute(
                    select(ImportJobLog).where(ImportJobLog.import_job_id == job.id)
                )
            )
            .scalars()
            .all()
        )
        detail_log = next(log for log in logs if log.event == "import_file_conflict_detail")
        assert detail_log.data["diagnostics"]["preferred_file_name"] == "Batman 001.cbz"

    @pytest.mark.asyncio
    async def test_cross_series_rows_same_issue_become_conflict(
        self,
        db_session: AsyncSession,
    ) -> None:
        series, _issues = await _setup_series_with_issues(
            db_session,
            title="Agent Alpha",
            year=1997,
            cv_id=144944,
            issues=[(10.0, 100010, "Fucking Patriot")],
        )
        job, canonical_series, canonical_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Agent Alpha (1997) #010.cbz",
                    "parsed_issue_number": 10.0,
                    "parsed_series": "Agent Alpha",
                    "file_size": 1024,
                }
            ],
        )
        override_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Agent Alpha - Fucking Patriot",
            raw_year=2010,
            status=ImportSeriesStatus.MATCHED,
            cv_id=series.comicvine_id,
            cv_match_score=0.92,
            cv_match_method="fuzzy_title",
            file_count=1,
            series_id=series.id,
        )
        db_session.add(override_series)
        await db_session.flush()
        override_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=override_series.id,
            file_path="/tmp/comics/Agent Alpha 10 - Fucking Patriot (2010).cbr",
            file_name="Agent Alpha 10 - Fucking Patriot (2010).cbr",
            file_size=2048,
            file_format="cbr",
            parsed_series="Agent Alpha",
            parsed_issue_number=10.0,
            parsed_year=2010,
            status=ImportedFileStatus.PENDING,
        )
        db_session.add(override_file)
        await db_session.flush()

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(canonical_series)
        await db_session.refresh(canonical_files[0])
        await db_session.refresh(override_series)
        await db_session.refresh(override_file)
        assert canonical_files[0].status == ImportedFileStatus.CONFLICT
        assert override_file.status == ImportedFileStatus.CONFLICT
        assert canonical_files[0].conflict_group_id == override_file.conflict_group_id
        assert canonical_files[0].diagnostics["scope"] == "cross_series"
        assert override_file.diagnostics["scope"] == "cross_series"
        assert canonical_series.files_conflict == 1
        assert override_series.files_conflict == 1

        logs = (
            (
                await db_session.execute(
                    select(ImportJobLog).where(ImportJobLog.import_job_id == job.id)
                )
            )
            .scalars()
            .all()
        )
        detail_log = next(
            log
            for log in logs
            if log.event == "import_file_conflict_detail"
            and "Cross-series conflict group" in log.message
        )
        assert detail_log.data["diagnostics"]["scope"] == "cross_series"

    @pytest.mark.asyncio
    async def test_three_files_same_issue(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(db_session)
        job, _imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 001.cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                },
                {
                    "file_name": "Batman 001 (2).cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                },
                {
                    "file_name": "Batman 001 (3).cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                },
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        for f in imp_files:
            await db_session.refresh(f)
        assert all(f.status == ImportedFileStatus.CONFLICT for f in imp_files)
        group_ids = {f.conflict_group_id for f in imp_files}
        assert len(group_ids) == 1
        assert None not in group_ids


class TestConflictTiebreakers:
    """Tiebreaker logic for conflict resolution."""

    @pytest.mark.asyncio
    async def test_comicinfo_preferred(self, db_session: AsyncSession) -> None:
        """File with ComicInfo.xml is preferred over file without."""
        series, _issues = await _setup_series_with_issues(db_session)
        job, _imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 001.cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                    "has_comicinfo": False,
                    "file_size": 2048,
                },
                {
                    "file_name": "Batman 001 (tagged).cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                    "has_comicinfo": True,
                    "file_size": 1024,
                },
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_files[0])
        await db_session.refresh(imp_files[1])
        # The one with comicinfo should be preferred
        assert imp_files[1].is_preferred is True
        assert imp_files[0].is_preferred is False
        assert "ComicInfo metadata present" in imp_files[1].diagnostics["preferred_reasons"]

    @pytest.mark.asyncio
    async def test_larger_file_preferred(self, db_session: AsyncSession) -> None:
        """Larger file size breaks ties when other tiebreakers are equal."""
        series, _issues = await _setup_series_with_issues(db_session)
        job, _imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 001.cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                    "file_size": 5000,
                },
                {
                    "file_name": "Batman 001 (2).cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                    "file_size": 1000,
                },
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_files[0])
        await db_session.refresh(imp_files[1])
        assert imp_files[0].is_preferred is True
        assert imp_files[1].is_preferred is False
        assert "Largest file size" in imp_files[0].diagnostics["preferred_reasons"]


class TestConflictCounters:
    """Conflict counters are updated on series and job."""

    @pytest.mark.asyncio
    async def test_conflict_counters(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(db_session)
        job, imp_series, _imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 001.cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                },
                {
                    "file_name": "Batman 001 (2).cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                },
                {
                    "file_name": "Batman 002.cbz",
                    "parsed_issue_number": 2.0,
                    "parsed_series": "Batman",
                },
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_series)
        assert imp_series.files_total == 3
        assert imp_series.files_conflict == 2
        assert imp_series.files_matched == 1

        await db_session.refresh(job)
        assert job.total_files_conflict == 2
        assert job.total_files_matched == 1


class TestNoConflictWhenDifferentIssues:
    """Files matching different issues are not in conflict."""

    @pytest.mark.asyncio
    async def test_different_issues_no_conflict(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(db_session)
        job, _imp_series, imp_files = await _setup_import_job(db_session, series)

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        for f in imp_files:
            await db_session.refresh(f)
        assert all(f.status == ImportedFileStatus.MATCHED for f in imp_files)
        assert all(f.conflict_group_id is None for f in imp_files)


# ── Edge Case Tests ──────────────────────────────────────────────────────


class TestCvIdPriorityOverIssueNumber:
    """CV issue ID match takes priority when both CV ID and issue number match."""

    @pytest.mark.asyncio
    async def test_cv_id_wins_over_issue_number(self, db_session: AsyncSession) -> None:
        """File has CV ID pointing to issue 2 but parsed_issue_number=1."""
        series, issues = await _setup_series_with_issues(db_session)
        job, _imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 001.cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                    "has_comicinfo": True,
                    "comicvine_issue_id": 100002,  # Points to issue #2
                }
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_files[0])
        assert imp_files[0].matched_issue_id == issues[1].id  # issue #2
        assert imp_files[0].match_method == "comicvine_id"


class TestDeferredComicInfoIdentity:
    """Deferred ComicInfo identity must participate in the retry match."""

    @pytest.mark.asyncio
    async def test_deferred_cv_issue_id_is_persisted_before_retry(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        series, issues = await _setup_series_with_issues(db_session)
        job, _imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman Volume 2.cbz",
                    "parsed_issue_number": 99.0,
                    "parsed_series": "Batman",
                    "diagnostics": {
                        "source_issue_type": IssueType.ISSUE.value,
                        "source_metadata": {"archive_metadata_deferred": True},
                    },
                }
            ],
        )

        async def load_deferred_metadata(
            _imp_series: ImportedSeries,
            imp_file: ImportedFile,
        ) -> SourceMetadata:
            return SourceMetadata(
                original_title=imp_file.file_name,
                source_path=imp_file.file_path,
                series_name="Batman",
                issue_number=2.0,
                issue_number_text="2",
                comicvine_issue_id=100002,
                signals={
                    "series_name": MetadataSignal.COMICINFO,
                    "issue_number": MetadataSignal.COMICINFO,
                    "comicvine_issue_id": MetadataSignal.COMICINFO,
                },
                diagnostics={
                    "has_comicinfo": True,
                    "comicinfo": {
                        "series": "Batman",
                        "number": "2",
                        "notes": "[cv_issue_id:100002]",
                    },
                },
            )

        svc = _make_service()
        monkeypatch.setattr(
            svc,
            "_load_deferred_source_metadata_for_import_file",
            load_deferred_metadata,
        )

        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_files[0])
        assert imp_files[0].comicvine_issue_id == 100002
        assert imp_files[0].matched_issue_id == issues[1].id
        assert imp_files[0].status == ImportedFileStatus.MATCHED
        assert imp_files[0].match_method == "comicvine_id"

    @pytest.mark.asyncio
    async def test_deferred_cv_issue_id_rebuilds_new_series_target_index(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        provider = AsyncMock()
        provider.get_issues_for_series = AsyncMock(
            return_value=[
                IssueSummary(
                    provider_id="100002",
                    issue_number=2.0,
                    title="I Am Gotham Part 2",
                    release_date=None,
                    cover_url=None,
                    issue_type="issue",
                )
            ]
        )
        metadata_service = AsyncMock()
        metadata_service._provider = provider
        svc = ImportService(
            series_service=AsyncMock(),
            metadata_service=metadata_service,
            event_bus=AsyncMock(),
        )

        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()
        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Batman",
            raw_year=2016,
            status=ImportSeriesStatus.MATCHED,
            cv_id=97508,
            cv_title="Batman",
            cv_year=2016,
            cv_issue_count=3,
            cv_match_score=0.95,
            cv_match_method="exact_title_year",
            file_count=1,
            files_total=1,
        )
        db_session.add(imp_series)
        await db_session.flush()
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/comics/Batman Volume 2.cbz",
            file_name="Batman Volume 2.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_series="Batman",
            parsed_issue_number=99.0,
            status=ImportedFileStatus.PENDING,
            diagnostics={
                "source_issue_type": IssueType.ISSUE.value,
                "source_metadata": {"archive_metadata_deferred": True},
            },
        )
        db_session.add(imp_file)
        await db_session.flush()

        async def load_deferred_metadata(
            _imp_series: ImportedSeries,
            source_file: ImportedFile,
        ) -> SourceMetadata:
            return SourceMetadata(
                original_title=source_file.file_name,
                source_path=source_file.file_path,
                series_name="Batman",
                issue_number=2.0,
                issue_number_text="2",
                comicvine_series_id=97508,
                comicvine_issue_id=100002,
                signals={
                    "series_name": MetadataSignal.COMICINFO,
                    "issue_number": MetadataSignal.COMICINFO,
                    "comicvine_series_id": MetadataSignal.COMICINFO,
                    "comicvine_issue_id": MetadataSignal.COMICINFO,
                },
                diagnostics={
                    "has_comicinfo": True,
                    "comicinfo": {
                        "series": "Batman",
                        "number": "2",
                        "notes": "[cv_vol_id:97508] [cv_issue_id:100002]",
                    },
                },
            )

        monkeypatch.setattr(
            svc,
            "_load_deferred_source_metadata_for_import_file",
            load_deferred_metadata,
        )

        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_file)
        assert imp_file.comicvine_issue_id == 100002
        assert imp_file.matched_issue_id is None
        assert imp_file.matched_issue_cv_id == 100002
        assert imp_file.status == ImportedFileStatus.MATCHED
        assert imp_file.match_method == "comicvine_id"
        assert provider.get_issues_for_series.await_count == 1


class TestCvIdNotInMapFallsThrough:
    """Unknown strong CV issue IDs block issue-number fallback."""

    @pytest.mark.asyncio
    async def test_unknown_cv_id_becomes_no_match(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(db_session)
        job, _imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 001.cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                    "has_comicinfo": True,
                    "comicvine_issue_id": 999999,  # Not in any issue
                }
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_files[0])
        assert imp_files[0].matched_issue_id is None
        assert imp_files[0].status == ImportedFileStatus.NO_MATCH


class TestSeriesWithNoFilesSkipped:
    """Matched series with zero ImportedFile records is handled gracefully."""

    @pytest.mark.asyncio
    async def test_no_files_zero_counters(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(db_session)
        job, imp_series, _imp_files = await _setup_import_job(
            db_session,
            series,
            files=[],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_series)
        assert imp_series.files_total == 0
        assert imp_series.files_matched == 0

        await db_session.refresh(job)
        assert job.total_files_found == 0


class TestUnmatchedSeriesExcluded:
    """Series with status != MATCHED/DUPLICATE are not file-matched."""

    @pytest.mark.asyncio
    async def test_no_match_series_skipped(self, db_session: AsyncSession) -> None:
        _series, _issues = await _setup_series_with_issues(db_session)
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()

        # Series with NO_MATCH status — should be skipped
        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Unknown",
            status=ImportSeriesStatus.NO_MATCH,
            file_count=1,
        )
        db_session.add(imp_series)
        await db_session.flush()

        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/Unknown 001.cbz",
            file_name="Unknown 001.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_issue_number=1.0,
        )
        db_session.add(imp_file)
        await db_session.flush()

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_file)
        assert imp_file.status == ImportedFileStatus.PENDING  # Untouched

        await db_session.refresh(job)
        assert job.total_files_found == 0


class TestDuplicateSeriesMergeReview:
    """Duplicate-series files are matched against existing library issues for merge review."""

    @pytest.mark.asyncio
    async def test_duplicate_series_importable_match(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(db_session)
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()

        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Batman",
            raw_year=2016,
            status=ImportSeriesStatus.DUPLICATE,
            cv_id=97508,
            cv_match_score=0.95,
            series_id=series.id,
            file_count=1,
        )
        db_session.add(imp_series)
        await db_session.flush()

        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/Batman 001.cbz",
            file_name="Batman 001.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_issue_number=1.0,
            parsed_series="Batman",
        )
        db_session.add(imp_file)
        await db_session.flush()

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_file)
        assert imp_file.status == ImportedFileStatus.MATCHED
        assert imp_file.include_in_import is False
        assert imp_file.diagnostics.get("target_state") == "wanted"

        await db_session.refresh(job)
        assert job.total_files_found == 1
        assert job.total_files_matched == 1

    @pytest.mark.asyncio
    async def test_duplicate_series_uses_filename_issue_when_comicinfo_number_was_bad(
        self,
        db_session: AsyncSession,
    ) -> None:
        series, issues = await _setup_series_with_issues(
            db_session,
            title="Necronomicon",
            year=2008,
            cv_id=22863,
            issues=[
                (1.0, 137318, None),
                (2.0, 141179, None),
                (3.0, 145240, None),
                (4.0, 148692, None),
            ],
        )
        # Simulate issues 1-3 already imported; issue 4 remains a valid merge target.
        root = LibraryRoot(name="Main", path="/library/main")
        db_session.add(root)
        await db_session.flush()
        for issue in issues[:3]:
            db_session.add(
                LibraryFile(
                    file_path=f"/library/main/Necronomicon {int(issue.issue_number):03d}.cbz",
                    file_name=f"Necronomicon {int(issue.issue_number):03d}.cbz",
                    file_size=2048,
                    file_format=FileFormat.CBZ,
                    file_modified_at=datetime.now(UTC),
                    match_confidence=MatchConfidence.HIGH,
                    issue_id=issue.id,
                    library_root_id=root.id,
                )
            )
        await db_session.flush()

        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()

        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Necronomicon",
            raw_year=2008,
            status=ImportSeriesStatus.DUPLICATE,
            cv_id=22863,
            cv_match_score=1.0,
            series_id=series.id,
            file_count=1,
        )
        db_session.add(imp_series)
        await db_session.flush()

        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/Necronomicon 04 (of 04) (2008) (Digital).cbr",
            file_name="Necronomicon 04 (of 04) (2008) (Digital).cbr",
            file_size=1024,
            file_format="cbr",
            parsed_series="Necronomicon",
            parsed_issue_number=None,
            parsed_year=2008,
            diagnostics={
                "metadata_signals": {
                    "series_name": MetadataSignal.COMICINFO.value,
                    "issue_number": MetadataSignal.COMICINFO.value,
                    "publisher": MetadataSignal.COMICINFO.value,
                },
                "source_metadata": {
                    "filename_parse": {
                        "series_name": "Necronomicon",
                        "issue_number": 4.0,
                        "year": 2008,
                        "volume": None,
                        "issue_type": IssueType.ISSUE.value,
                    },
                    "has_comicinfo": True,
                    "comicinfo": {
                        "series": "Necronomicon",
                        "number": "04 (of 04)",
                        "publisher": "BOOM! Studios",
                    },
                },
            },
        )
        db_session.add(imp_file)
        await db_session.flush()

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_file)
        assert imp_file.status == ImportedFileStatus.MATCHED
        assert imp_file.include_in_import is False
        assert imp_file.matched_issue_id == issues[3].id
        assert imp_file.diagnostics.get("target_state") == "wanted"

        await db_session.refresh(job)
        assert job.total_files_found == 1
        assert job.total_files_matched == 1


class TestCrossBucketDuplicateCopies:
    """Repeated same-issue files should collapse into duplicate-copy rows when appropriate."""

    @pytest.mark.asyncio
    async def test_exact_duplicate_copies_collapse_to_duplicate_file(
        self,
        db_session: AsyncSession,
    ) -> None:
        series, issues = await _setup_series_with_issues(db_session)
        job, _imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_path": "/tmp/set-a/Batman 001.cbz",
                    "file_name": "Batman 001.cbz",
                    "file_size": 2048,
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                },
                {
                    "file_path": "/tmp/set-b/Batman 001.cbz",
                    "file_name": "Batman 001.cbz",
                    "file_size": 2048,
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                },
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_files[0])
        await db_session.refresh(imp_files[1])

        representative = next(
            item for item in imp_files if item.status == ImportedFileStatus.MATCHED
        )
        duplicate = next(
            item for item in imp_files if item.status == ImportedFileStatus.DUPLICATE_FILE
        )
        assert representative.matched_issue_id == issues[0].id
        assert duplicate.duplicate_of_file_id == representative.id
        assert duplicate.include_in_import is False
        assert duplicate.diagnostics["duplicate_reason"] == "exact_duplicate"

        await db_session.refresh(job)
        assert job.total_files_found == 2
        assert job.total_files_matched == 1
        assert job.total_files_duplicate == 1
        assert job.total_files_conflict == 0

    @pytest.mark.asyncio
    async def test_variant_same_issue_candidates_stay_conflicts(
        self,
        db_session: AsyncSession,
    ) -> None:
        series, _issues = await _setup_series_with_issues(db_session)
        job, _imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_path": "/tmp/set-a/Batman 001.cbz",
                    "file_name": "Batman 001.cbz",
                    "file_size": 2048,
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                },
                {
                    "file_path": "/tmp/set-b/Batman 001 Deluxe.cbz",
                    "file_name": "Batman 001 Deluxe.cbz",
                    "file_size": 8192,
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                },
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_files[0])
        await db_session.refresh(imp_files[1])
        assert imp_files[0].status == ImportedFileStatus.CONFLICT
        assert imp_files[1].status == ImportedFileStatus.CONFLICT
        assert imp_files[0].conflict_group_id == imp_files[1].conflict_group_id

        await db_session.refresh(job)
        assert job.total_files_duplicate == 0
        assert job.total_files_conflict == 2

    @pytest.mark.asyncio
    async def test_informational_duplicate_series_copies_collapse(
        self,
        db_session: AsyncSession,
    ) -> None:
        series, issues = await _setup_series_with_issues(db_session)
        root = LibraryRoot(name="Main", path="/library/main")
        db_session.add(root)
        await db_session.flush()
        for issue in issues:
            db_session.add(
                LibraryFile(
                    file_path=f"/library/main/Batman {int(issue.issue_number):03}.cbz",
                    file_name=f"Batman {int(issue.issue_number):03}.cbz",
                    file_size=2048,
                    file_format=FileFormat.CBZ,
                    file_modified_at=datetime.now(UTC),
                    match_confidence=MatchConfidence.HIGH,
                    issue_id=issue.id,
                    library_root_id=root.id,
                )
            )
        await db_session.flush()

        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()

        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Batman",
            raw_year=2016,
            status=ImportSeriesStatus.DUPLICATE,
            cv_id=97508,
            cv_match_score=0.95,
            series_id=series.id,
            file_count=2,
        )
        db_session.add(imp_series)
        await db_session.flush()

        file_a = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/set-a/Batman GN.cbz",
            file_name="Batman GN.cbz",
            file_size=4096,
            file_format="cbz",
            parsed_series="Batman",
            parsed_issue_number=99.0,
        )
        file_b = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/set-b/Batman GN.cbz",
            file_name="Batman GN.cbz",
            file_size=4096,
            file_format="cbz",
            parsed_series="Batman",
            parsed_issue_number=99.0,
        )
        db_session.add_all([file_a, file_b])
        await db_session.flush()

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(file_a)
        await db_session.refresh(file_b)
        representative = next(
            item for item in (file_a, file_b) if item.status == ImportedFileStatus.NO_MATCH
        )
        duplicate = next(
            item for item in (file_a, file_b) if item.status == ImportedFileStatus.DUPLICATE_FILE
        )
        assert representative.diagnostics["target_state"] == "no_importable_targets"
        assert duplicate.diagnostics["duplicate_reason"] == "informational_duplicate"
        assert duplicate.duplicate_of_file_id == representative.id

        await db_session.refresh(job)
        assert job.total_files_duplicate == 1
        assert job.total_files_no_match == 1


class TestDuplicateAlreadyOwnedClassification:
    """Duplicate-series files already owned in the library stay informational."""

    @pytest.mark.asyncio
    async def test_duplicate_series_already_owned_file(self, db_session: AsyncSession) -> None:
        series, issues = await _setup_series_with_issues(db_session)
        root = LibraryRoot(name="Main", path="/library/main")
        db_session.add(root)
        await db_session.flush()
        db_session.add(
            LibraryFile(
                file_path="/library/main/Batman 001.cbz",
                file_name="Batman 001.cbz",
                file_size=2048,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(UTC),
                match_confidence=MatchConfidence.HIGH,
                issue_id=issues[0].id,
                library_root_id=root.id,
            )
        )
        await db_session.flush()

        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()

        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Batman",
            raw_year=2016,
            status=ImportSeriesStatus.DUPLICATE,
            cv_id=97508,
            cv_match_score=0.95,
            series_id=series.id,
            file_count=1,
        )
        db_session.add(imp_series)
        await db_session.flush()

        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/Batman 001.cbz",
            file_name="Batman 001.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_issue_number=1.0,
            parsed_series="Batman",
        )
        db_session.add(imp_file)
        await db_session.flush()

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_file)
        assert imp_file.status == ImportedFileStatus.ALREADY_OWNED
        assert imp_file.include_in_import is False
        assert imp_file.diagnostics.get("target_state") == "already_owned"

        await db_session.refresh(job)
        assert job.total_files_found == 1
        assert job.total_files_already_owned == 1

    @pytest.mark.asyncio
    async def test_duplicate_single_owned_gn_shortcut(self, db_session: AsyncSession) -> None:
        series, issues = await _setup_series_with_issues(
            db_session,
            title="About Betty's Boob",
            year=2018,
            cv_id=111396,
            issues=[(1.0, 672319, "GN")],
            series_status=SeriesStatus.ENDED,
            series_type=SeriesType.GRAPHIC_NOVEL,
        )
        issues[0].issue_type = IssueType.GN
        issues[0].status = IssueStatus.OWNED

        root = LibraryRoot(name="Main", path="/library/main")
        db_session.add(root)
        await db_session.flush()
        db_session.add(
            LibraryFile(
                file_path="/library/main/About Betty's Boob.cbr",
                file_name="About Betty's Boob.cbr",
                file_size=2048,
                file_format=FileFormat.CBR,
                file_modified_at=datetime.now(UTC),
                match_confidence=MatchConfidence.HIGH,
                issue_id=issues[0].id,
                library_root_id=root.id,
            )
        )
        await db_session.flush()

        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()

        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="About Betty's Boob",
            raw_year=2018,
            status=ImportSeriesStatus.DUPLICATE,
            cv_id=111396,
            cv_match_score=0.95,
            series_id=series.id,
            file_count=1,
        )
        db_session.add(imp_series)
        await db_session.flush()

        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/About Betty's Boob.cbr",
            file_name="About Betty's Boob (2018).cbr",
            file_size=1024,
            file_format="cbr",
            parsed_series="About Betty's Boob",
            parsed_issue_number=None,
        )
        db_session.add(imp_file)
        await db_session.flush()

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_file)
        await db_session.refresh(imp_series)
        assert imp_file.status == ImportedFileStatus.ALREADY_OWNED
        assert imp_file.include_in_import is False
        assert imp_file.match_method == "single_owned_shortcut"
        assert imp_file.matched_issue_id == issues[0].id
        assert imp_file.diagnostics.get("resolution_reason") == (
            "single_owned_non_standard_ended_series"
        )
        assert imp_series.diagnostics.get("actionable_duplicate_merge") is False
        assert imp_series.diagnostics.get("fully_owned_series") is True

    @pytest.mark.asyncio
    async def test_duplicate_fully_owned_multi_issue_is_informational_only(
        self, db_session: AsyncSession
    ) -> None:
        series, issues = await _setup_series_with_issues(
            db_session,
            title="AL15",
            year=2020,
            cv_id=555001,
            issues=[
                (1.0, 700001, "Issue 1"),
                (2.0, 700002, "Issue 2"),
                (3.0, 700003, "Issue 3"),
            ],
        )
        for issue in issues:
            issue.status = IssueStatus.OWNED

        root = LibraryRoot(name="Main", path="/library/main")
        db_session.add(root)
        await db_session.flush()
        for idx, issue in enumerate(issues, start=1):
            db_session.add(
                LibraryFile(
                    file_path=f"/library/main/AL15-{idx}.cbz",
                    file_name=f"AL15-{idx}.cbz",
                    file_size=2048 + idx,
                    file_format=FileFormat.CBZ,
                    file_modified_at=datetime.now(UTC),
                    match_confidence=MatchConfidence.HIGH,
                    issue_id=issue.id,
                    library_root_id=root.id,
                )
            )
        await db_session.flush()

        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()

        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="AL15",
            raw_year=2020,
            status=ImportSeriesStatus.DUPLICATE,
            cv_id=555001,
            cv_match_score=0.95,
            series_id=series.id,
            file_count=1,
        )
        db_session.add(imp_series)
        await db_session.flush()

        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/AL15-new.cbz",
            file_name="AL15 bonus story.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_series="AL15",
            parsed_issue_number=None,
        )
        db_session.add(imp_file)
        await db_session.flush()

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_file)
        await db_session.refresh(imp_series)
        assert imp_file.status == ImportedFileStatus.NO_MATCH
        assert imp_file.include_in_import is False
        assert imp_file.diagnostics.get("target_state") == "no_importable_targets"
        assert imp_series.diagnostics.get("actionable_duplicate_merge") is False
        assert imp_series.diagnostics.get("fully_owned_series") is True


class TestFractionalIssueNumber:
    """Fractional issue numbers (e.g. 1.5) match correctly."""

    @pytest.mark.asyncio
    async def test_half_issue_matched(self, db_session: AsyncSession) -> None:
        series, issues = await _setup_series_with_issues(
            db_session,
            issues=[(1.0, 100001, "Issue 1"), (1.5, 100002, "Issue 1.5")],
        )
        job, _imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 001.5.cbz",
                    "parsed_issue_number": 1.5,
                    "parsed_series": "Batman",
                }
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_files[0])
        assert imp_files[0].matched_issue_id == issues[1].id
        assert imp_files[0].status == ImportedFileStatus.MATCHED


class TestSeriesWithoutLinkedSeriesSkipped:
    """Matched series without series_id is not file-matched."""

    @pytest.mark.asyncio
    async def test_no_series_id_skipped(self, db_session: AsyncSession) -> None:
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()

        # Series with MATCHED status but series_id=None (not linked yet)
        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Orphan",
            status=ImportSeriesStatus.MATCHED,
            cv_id=12345,
            cv_match_score=0.95,
            series_id=None,
            file_count=1,
        )
        db_session.add(imp_series)
        await db_session.flush()

        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/Orphan 001.cbz",
            file_name="Orphan 001.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_issue_number=1.0,
        )
        db_session.add(imp_file)
        await db_session.flush()

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_file)
        assert imp_file.status == ImportedFileStatus.PENDING  # Untouched


# ── Additional Edge Case Tests ────────────────────────────────────────────


class TestMixedResultsInSameSeries:
    """One series with 3 files: CV ID match, issue number match, and no match."""

    @pytest.mark.asyncio
    async def test_mixed_match_results(self, db_session: AsyncSession) -> None:
        series, issues = await _setup_series_with_issues(
            db_session,
            issues=[
                (1.0, 100001, "Issue 1"),
                (2.0, 100002, "Issue 2"),
                (3.0, 100003, "Issue 3"),
            ],
        )
        job, imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 001.cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                    "has_comicinfo": True,
                    "comicvine_issue_id": 100001,  # CV ID match → high
                },
                {
                    "file_name": "Batman 002.cbz",
                    "parsed_issue_number": 2.0,
                    "parsed_series": "Batman",
                    # No CV ID → issue number match → high (score >= 0.90)
                },
                {
                    "file_name": "Batman 099.cbz",
                    "parsed_issue_number": 99.0,
                    "parsed_series": "Batman",
                    # Issue 99 doesn't exist → no match
                },
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        for f in imp_files:
            await db_session.refresh(f)

        # File 0: CV ID match
        assert imp_files[0].status == ImportedFileStatus.MATCHED
        assert imp_files[0].matched_issue_id == issues[0].id
        assert imp_files[0].match_confidence == "high"
        assert imp_files[0].match_method == "comicvine_id"

        # File 1: issue number match
        assert imp_files[1].status == ImportedFileStatus.MATCHED
        assert imp_files[1].matched_issue_id == issues[1].id
        assert imp_files[1].match_confidence == "high"
        assert imp_files[1].match_method == "issue_number"

        # File 2: no match
        assert imp_files[2].status == ImportedFileStatus.NO_MATCH
        assert imp_files[2].matched_issue_id is None

        # Counters
        await db_session.refresh(imp_series)
        assert imp_series.files_total == 3
        assert imp_series.files_matched == 2
        assert imp_series.files_no_match == 1

        await db_session.refresh(job)
        assert job.total_files_found == 3
        assert job.total_files_matched == 2
        assert job.total_files_no_match == 1


class TestIssueZero:
    """Series has issue #0; file with parsed_issue_number=0.0 matches it."""

    @pytest.mark.asyncio
    async def test_issue_zero_matched(self, db_session: AsyncSession) -> None:
        series, issues = await _setup_series_with_issues(
            db_session,
            issues=[(0.0, 100000, "Issue 0"), (1.0, 100001, "Issue 1")],
        )
        job, _imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 000.cbz",
                    "parsed_issue_number": 0.0,
                    "parsed_series": "Batman",
                }
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_files[0])
        assert imp_files[0].status == ImportedFileStatus.MATCHED
        assert imp_files[0].matched_issue_id == issues[0].id
        assert imp_files[0].match_method == "issue_number"


class TestLargeIssueNumber:
    """Series with issue_number=999.0 matches file with parsed_issue_number=999.0."""

    @pytest.mark.asyncio
    async def test_large_issue_number_matched(self, db_session: AsyncSession) -> None:
        series, issues = await _setup_series_with_issues(
            db_session,
            issues=[(999.0, 109999, "Issue 999")],
        )
        job, _imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 999.cbz",
                    "parsed_issue_number": 999.0,
                    "parsed_series": "Batman",
                }
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_files[0])
        assert imp_files[0].status == ImportedFileStatus.MATCHED
        assert imp_files[0].matched_issue_id == issues[0].id


class TestSkippedSeriesExcluded:
    """ImportedSeries with status=SKIPPED should not have files processed."""

    @pytest.mark.asyncio
    async def test_skipped_series_files_stay_pending(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(db_session)
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()

        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Batman",
            raw_year=2016,
            status=ImportSeriesStatus.SKIPPED,
            cv_id=97508,
            cv_match_score=0.95,
            series_id=series.id,
            file_count=1,
        )
        db_session.add(imp_series)
        await db_session.flush()

        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/Batman 001.cbz",
            file_name="Batman 001.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_issue_number=1.0,
            parsed_series="Batman",
        )
        db_session.add(imp_file)
        await db_session.flush()

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_file)
        assert imp_file.status == ImportedFileStatus.PENDING

        await db_session.refresh(job)
        assert job.total_files_found == 0


class TestConfirmedSeriesExcluded:
    """ImportedSeries with status=CONFIRMED is not in MATCHED/DUPLICATE, so excluded."""

    @pytest.mark.asyncio
    async def test_confirmed_series_files_stay_pending(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(db_session)
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()

        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Batman",
            raw_year=2016,
            status=ImportSeriesStatus.CONFIRMED,
            cv_id=97508,
            cv_match_score=0.95,
            series_id=series.id,
            file_count=1,
        )
        db_session.add(imp_series)
        await db_session.flush()

        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/Batman 001.cbz",
            file_name="Batman 001.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_issue_number=1.0,
            parsed_series="Batman",
        )
        db_session.add(imp_file)
        await db_session.flush()

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(imp_file)
        assert imp_file.status == ImportedFileStatus.PENDING

        await db_session.refresh(job)
        assert job.total_files_found == 0


class TestThreeWayConflictTiebreaker:
    """3 files match same issue; tiebreaker: comicinfo > confidence > file_size."""

    @pytest.mark.asyncio
    async def test_comicinfo_wins_three_way(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(
            db_session,
            issues=[(1.0, 100001, "Issue 1")],
        )
        job, _imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 001.cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                    "has_comicinfo": False,
                    "file_size": 3000,
                },
                {
                    "file_name": "Batman 001 (tagged).cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                    "has_comicinfo": True,
                    "file_size": 1000,
                },
                {
                    "file_name": "Batman 001 (hq).cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                    "has_comicinfo": False,
                    "file_size": 5000,
                },
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        for f in imp_files:
            await db_session.refresh(f)

        # All three should be conflicts
        assert all(f.status == ImportedFileStatus.CONFLICT for f in imp_files)

        # File B (has_comicinfo=True) should be preferred despite smallest size
        assert imp_files[1].is_preferred is True
        assert imp_files[0].is_preferred is False
        assert imp_files[2].is_preferred is False


class TestSequentialConflictGroupIds:
    """Two different issues each with 2 files → group IDs are sequential (1, 2)."""

    @pytest.mark.asyncio
    async def test_sequential_group_ids(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(
            db_session,
            issues=[
                (1.0, 100001, "Issue 1"),
                (2.0, 100002, "Issue 2"),
            ],
        )
        job, _imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 001.cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                },
                {
                    "file_name": "Batman 001 (2).cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                },
                {
                    "file_name": "Batman 002.cbz",
                    "parsed_issue_number": 2.0,
                    "parsed_series": "Batman",
                },
                {
                    "file_name": "Batman 002 (2).cbz",
                    "parsed_issue_number": 2.0,
                    "parsed_series": "Batman",
                },
            ],
        )

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        for f in imp_files:
            await db_session.refresh(f)

        # All four should be conflicts
        assert all(f.status == ImportedFileStatus.CONFLICT for f in imp_files)

        # Files for issue 1 share one group, files for issue 2 share another
        group_issue1 = {imp_files[0].conflict_group_id, imp_files[1].conflict_group_id}
        group_issue2 = {imp_files[2].conflict_group_id, imp_files[3].conflict_group_id}
        assert len(group_issue1) == 1  # Same group ID within issue 1
        assert len(group_issue2) == 1  # Same group ID within issue 2

        gid1 = imp_files[0].conflict_group_id
        gid2 = imp_files[2].conflict_group_id
        assert gid1 != gid2
        # Sequential: one is 1, other is 2
        assert {gid1, gid2} == {1, 2}


class TestConflictWithDifferentConfidences:
    """Two files match same issue: one high (CV ID), one medium (issue number). High wins."""

    @pytest.mark.asyncio
    async def test_high_confidence_preferred(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(
            db_session,
            issues=[(1.0, 100001, "Issue 1")],
        )
        job, imp_series, imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 001 (tagged).cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                    "has_comicinfo": True,
                    "comicvine_issue_id": 100001,  # CV ID → high confidence
                    "file_size": 1000,
                },
                {
                    "file_name": "Batman 001.cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                    "has_comicinfo": False,
                    "file_size": 5000,  # Bigger file but no CV ID
                },
            ],
        )
        # Lower series score to make issue number match medium confidence
        imp_series.cv_match_score = 0.80
        await db_session.flush()

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        for f in imp_files:
            await db_session.refresh(f)

        # Both should be conflicts
        assert imp_files[0].status == ImportedFileStatus.CONFLICT
        assert imp_files[1].status == ImportedFileStatus.CONFLICT

        # File 0 has comicinfo=True AND high confidence → preferred
        assert imp_files[0].match_confidence == "high"
        assert imp_files[1].match_confidence == "medium"
        assert imp_files[0].is_preferred is True
        assert imp_files[1].is_preferred is False


class TestAlreadyProcessedFilesSkipped:
    """File with status != PENDING should not be re-processed by file matching."""

    @pytest.mark.asyncio
    async def test_matched_file_stays_unchanged(self, db_session: AsyncSession) -> None:
        series, issues = await _setup_series_with_issues(db_session)
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()

        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Batman",
            raw_year=2016,
            status=ImportSeriesStatus.MATCHED,
            cv_id=97508,
            cv_match_score=0.95,
            series_id=series.id,
            file_count=2,
        )
        db_session.add(imp_series)
        await db_session.flush()

        # File already matched (not PENDING) — should be skipped
        already_matched = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/Batman 001.cbz",
            file_name="Batman 001.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_issue_number=1.0,
            parsed_series="Batman",
            status=ImportedFileStatus.MATCHED,
            matched_issue_id=issues[0].id,
            match_confidence="high",
            match_method="comicvine_id",
        )
        db_session.add(already_matched)

        # File still PENDING — should be processed
        pending_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/Batman 002.cbz",
            file_name="Batman 002.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_issue_number=2.0,
            parsed_series="Batman",
        )
        db_session.add(pending_file)
        await db_session.flush()

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        await db_session.refresh(already_matched)
        await db_session.refresh(pending_file)

        # Already-matched file unchanged
        assert already_matched.status == ImportedFileStatus.MATCHED
        assert already_matched.matched_issue_id == issues[0].id
        assert already_matched.match_method == "comicvine_id"

        # Pending file was processed
        assert pending_file.status == ImportedFileStatus.MATCHED
        assert pending_file.matched_issue_id == issues[1].id

        # Counters reflect all files with persisted matching outcomes after recompute.
        await db_session.refresh(job)
        assert job.total_files_found == 2
        assert job.total_files_matched == 2


class TestProgressEventContainsFileCounters:
    """Progress callback event has total_files_found/matched/no_match/conflict populated."""

    @pytest.mark.asyncio
    async def test_progress_event_file_counters(self, db_session: AsyncSession) -> None:
        series, _issues = await _setup_series_with_issues(db_session)
        job, _imp_series, _imp_files = await _setup_import_job(
            db_session,
            series,
            files=[
                {
                    "file_name": "Batman 001.cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                },
                {
                    "file_name": "Batman 001 (2).cbz",
                    "parsed_issue_number": 1.0,
                    "parsed_series": "Batman",
                },
                {
                    "file_name": "Batman 099.cbz",
                    "parsed_issue_number": 99.0,
                    "parsed_series": "Batman",
                },
            ],
        )

        callback = AsyncMock()
        svc = _make_service()
        await svc._run_file_matching(db_session, job, progress_callback=callback)

        assert callback.call_count >= 1
        event = callback.call_args_list[-1][0][0]
        assert event.total_files_found == 3
        # 2 conflict + 1 no_match = 0 matched
        assert event.total_files_matched == 0
        assert event.total_files_no_match == 1
        assert event.total_files_conflict == 2


class TestMultipleConflictGroupsAcrossSeries:
    """Two series each with a conflict pair → group IDs are globally sequential."""

    @pytest.mark.asyncio
    async def test_global_sequential_conflict_groups(self, db_session: AsyncSession) -> None:
        series1, _issues1 = await _setup_series_with_issues(
            db_session,
            title="Batman",
            cv_id=97508,
            issues=[(1.0, 100001, "Issue 1")],
        )
        series2, _issues2 = await _setup_series_with_issues(
            db_session,
            title="Superman",
            cv_id=97509,
            issues=[(1.0, 200001, "Issue 1")],
            publisher_id=series1.publisher_id,
        )

        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.FILE_MATCHING,
        )
        db_session.add(job)
        await db_session.flush()

        # Series 1 with conflict pair
        imp_s1 = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Batman",
            raw_year=2016,
            status=ImportSeriesStatus.MATCHED,
            cv_id=97508,
            cv_match_score=0.95,
            series_id=series1.id,
            file_count=2,
        )
        db_session.add(imp_s1)
        await db_session.flush()

        f1a = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_s1.id,
            file_path="/tmp/Batman 001.cbz",
            file_name="Batman 001.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_issue_number=1.0,
            parsed_series="Batman",
        )
        f1b = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_s1.id,
            file_path="/tmp/Batman 001 (2).cbz",
            file_name="Batman 001 (2).cbz",
            file_size=2048,
            file_format="cbz",
            parsed_issue_number=1.0,
            parsed_series="Batman",
        )
        db_session.add_all([f1a, f1b])

        # Series 2 with conflict pair
        imp_s2 = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Superman",
            raw_year=2016,
            status=ImportSeriesStatus.MATCHED,
            cv_id=97509,
            cv_match_score=0.95,
            series_id=series2.id,
            file_count=2,
        )
        db_session.add(imp_s2)
        await db_session.flush()

        f2a = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_s2.id,
            file_path="/tmp/Superman 001.cbz",
            file_name="Superman 001.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_issue_number=1.0,
            parsed_series="Superman",
        )
        f2b = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_s2.id,
            file_path="/tmp/Superman 001 (2).cbz",
            file_name="Superman 001 (2).cbz",
            file_size=2048,
            file_format="cbz",
            parsed_issue_number=1.0,
            parsed_series="Superman",
        )
        db_session.add_all([f2a, f2b])
        await db_session.flush()

        svc = _make_service()
        await svc._run_file_matching(db_session, job)

        for f in [f1a, f1b, f2a, f2b]:
            await db_session.refresh(f)

        # All four should be conflicts
        assert all(f.status == ImportedFileStatus.CONFLICT for f in [f1a, f1b, f2a, f2b])

        # Series 1 pair shares one group, series 2 pair shares another
        assert f1a.conflict_group_id == f1b.conflict_group_id
        assert f2a.conflict_group_id == f2b.conflict_group_id

        # Groups are globally sequential: {1, 2}
        gid1 = f1a.conflict_group_id
        gid2 = f2a.conflict_group_id
        assert gid1 != gid2
        assert {gid1, gid2} == {1, 2}

        # Job-level counters
        await db_session.refresh(job)
        assert job.total_files_conflict == 4
        assert job.total_files_matched == 0
