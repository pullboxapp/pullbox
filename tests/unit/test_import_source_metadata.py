"""Tests for import source metadata reconstruction helpers."""

from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING

import pytest

from pullbox.core.source_metadata import MetadataSignal, SourceMetadata
from pullbox.models.import_job import ImportedFile, ImportedSeries, ImportJob, ImportSourceType
from pullbox.models.issue import IssueType
from pullbox.services.import_source_metadata import (
    build_import_metadata_conflict,
    load_deferred_source_metadata_for_import_file,
    source_metadata_for_import_file,
    source_metadata_for_matching_series,
    sync_import_file_source_metadata,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_import_job(session: AsyncSession) -> ImportJob:
    job = ImportJob(source_path="/tmp/comics", source_type=ImportSourceType.FILESYSTEM)
    session.add(job)
    await session.flush()
    return job


def test_source_metadata_for_import_file_restores_persisted_signals() -> None:
    imp_series = ImportedSeries(
        raw_series_name="Chicken Devils",
        raw_year=2022,
        diagnostics={"source_issue_type": "issue"},
    )
    imp_file = ImportedFile(
        file_path="/tmp/Chicken Devil 004 (2022).cbz",
        file_name="Chicken Devil 004 (2022).cbz",
        parsed_series="Chicken Devil",
        parsed_issue_number=4.0,
        parsed_year=2022,
        comicvine_issue_id=905404,
        diagnostics={
            "metadata_signals": {
                "series_name": MetadataSignal.COMICINFO.value,
                "unknown": "not-a-real-signal",
            },
            "source_metadata": {"has_comicinfo": True},
        },
    )

    metadata = source_metadata_for_import_file(imp_series, imp_file)

    assert metadata.series_name == "Chicken Devil"
    assert metadata.issue_number == 4.0
    assert metadata.issue_type == IssueType.ISSUE
    assert metadata.signals == {"series_name": MetadataSignal.COMICINFO}
    assert metadata.diagnostics == {"has_comicinfo": True}


def test_source_metadata_for_import_file_uses_persisted_filename_issue_fallback() -> None:
    imp_series = ImportedSeries(
        raw_series_name="Necronomicon",
        raw_year=2008,
        diagnostics={"source_issue_type": "issue"},
    )
    imp_file = ImportedFile(
        file_path="/tmp/Necronomicon 04 (of 04) (2008) (Digital).cbr",
        file_name="Necronomicon 04 (of 04) (2008) (Digital).cbr",
        parsed_series="Necronomicon",
        parsed_issue_number=None,
        parsed_year=2008,
        diagnostics={
            "metadata_signals": {
                "series_name": MetadataSignal.COMICINFO.value,
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

    metadata = source_metadata_for_import_file(imp_series, imp_file)

    assert metadata.series_name == "Necronomicon"
    assert metadata.issue_number == 4.0
    assert metadata.issue_type == IssueType.ISSUE


def test_source_metadata_for_import_file_uses_volume_base_series_hint() -> None:
    imp_series = ImportedSeries(
        raw_series_name="Fearscape A Dark Interlude",
        raw_year=2023,
        diagnostics={"source_issue_type": "volume"},
    )
    imp_file = ImportedFile(
        file_path="/tmp/Fearscape.Vol.02.A.Dark.Interlude.2023.pdf",
        file_name="Fearscape.Vol.02.A.Dark.Interlude.2023.pdf",
        parsed_series="Fearscape A Dark Interlude",
        parsed_issue_number=None,
        parsed_year=2023,
        diagnostics={
            "source_metadata": {
                "filename_parse": {
                    "series_name": "Fearscape A Dark Interlude",
                    "issue_number": None,
                    "year": 2023,
                    "volume": "Vol 02",
                    "issue_type": IssueType.VOLUME.value,
                }
            }
        },
    )

    metadata = source_metadata_for_import_file(imp_series, imp_file)

    assert metadata.series_name == "Fearscape"
    assert metadata.issue_number == 2.0
    assert metadata.volume == "Vol 02"
    assert metadata.issue_type == IssueType.VOLUME
    assert metadata.diagnostics["volume_subtitle_hint"] == {
        "base_series": "Fearscape",
        "subtitle": "A Dark Interlude",
        "issue_number": 2.0,
    }


def test_source_metadata_for_import_file_recovers_volume_hint_from_filename() -> None:
    imp_series = ImportedSeries(
        raw_series_name="Fearscape A Dark Interlude",
        raw_year=2023,
        diagnostics={},
    )
    imp_file = ImportedFile(
        file_path="/tmp/Fearscape.Vol.02.A.Dark.Interlude.2023.pdf",
        file_name="Fearscape.Vol.02.A.Dark.Interlude.2023.pdf",
        parsed_series="Fearscape A Dark Interlude",
        parsed_issue_number=None,
        parsed_year=2023,
        diagnostics={},
    )

    metadata = source_metadata_for_import_file(imp_series, imp_file)

    assert metadata.series_name == "Fearscape"
    assert metadata.issue_number == 2.0
    assert metadata.volume == "Vol 02"
    assert metadata.issue_type == IssueType.VOLUME
    assert metadata.diagnostics["volume_subtitle_hint"] == {
        "base_series": "Fearscape",
        "subtitle": "A Dark Interlude",
        "issue_number": 2.0,
    }


def test_source_metadata_for_import_file_preserves_explicit_issue_after_volume_marker() -> None:
    imp_series = ImportedSeries(
        raw_series_name="Grimm Tales of Terror - Unhappy Endings",
        raw_year=2026,
        diagnostics={"source_issue_type": IssueType.ISSUE.value},
    )
    imp_file = ImportedFile(
        file_path="/tmp/Grimm Tales of Terror v5 013 - Unhappy Endings (2026).cbr",
        file_name="Grimm Tales of Terror v5 013 - Unhappy Endings (2026).cbr",
        parsed_series="Grimm Tales of Terror - Unhappy Endings",
        parsed_issue_number=13.0,
        parsed_year=2026,
        diagnostics={
            "source_issue_type": IssueType.ISSUE.value,
            "source_metadata": {
                "filename_parse": {
                    "series_name": "Grimm Tales of Terror - Unhappy Endings",
                    "issue_number": 13.0,
                    "year": 2026,
                    "volume": "v5",
                    "issue_type": IssueType.ISSUE.value,
                }
            },
        },
    )

    metadata = source_metadata_for_import_file(imp_series, imp_file)

    assert metadata.series_name == "Grimm Tales of Terror - Unhappy Endings"
    assert metadata.issue_number == 13.0
    assert metadata.volume == "v5"
    assert metadata.issue_type == IssueType.ISSUE
    assert "volume_subtitle_hint" not in metadata.diagnostics


@pytest.mark.parametrize(
    (
        "file_name",
        "volume",
        "parsed_series",
        "expected_issue_number",
        "expected_hint",
    ),
    [
        (
            "Series v01 (2018) (Digital).cbz",
            "v01",
            "Series",
            1.0,
            None,
        ),
        (
            "Series Vol 02 [Digital].cbz",
            "Vol 02",
            "Series",
            2.0,
            None,
        ),
        (
            "Series v03 - Cradle (2020).cbz",
            "v03",
            "Series",
            3.0,
            {
                "base_series": "Series",
                "subtitle": "Cradle",
                "issue_number": 3.0,
            },
        ),
        (
            "Series Vol. 4: Grave [Digital].cbz",
            "Vol. 4",
            "Series",
            4.0,
            {
                "base_series": "Series",
                "subtitle": "Grave",
                "issue_number": 4.0,
            },
        ),
    ],
)
def test_source_metadata_for_import_file_ignores_metadata_only_volume_tails(
    file_name: str,
    volume: str,
    parsed_series: str,
    expected_issue_number: float,
    expected_hint: dict[str, object] | None,
) -> None:
    imp_series = ImportedSeries(
        raw_series_name=parsed_series,
        raw_year=2018,
        diagnostics={"source_issue_type": IssueType.VOLUME.value},
    )
    imp_file = ImportedFile(
        file_path=f"/tmp/{file_name}",
        file_name=file_name,
        parsed_series=parsed_series,
        parsed_issue_number=None,
        parsed_year=2018,
        diagnostics={
            "source_issue_type": IssueType.VOLUME.value,
            "source_metadata": {
                "filename_parse": {
                    "series_name": parsed_series,
                    "issue_number": None,
                    "year": 2018,
                    "volume": volume,
                    "issue_type": IssueType.VOLUME.value,
                }
            },
        },
    )

    metadata = source_metadata_for_import_file(imp_series, imp_file)

    assert metadata.issue_number == expected_issue_number
    assert metadata.volume == volume
    if expected_hint is None:
        assert metadata.series_name == parsed_series
        assert "volume_subtitle_hint" not in metadata.diagnostics
    else:
        assert metadata.series_name == expected_hint["base_series"]
        assert metadata.diagnostics["volume_subtitle_hint"] == expected_hint


@pytest.mark.asyncio
async def test_source_metadata_for_matching_series_adds_alternate_release_candidates(
    db_session: AsyncSession,
) -> None:
    job = await _create_import_job(db_session)
    imp_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Chicken Devils",
        raw_year=2022,
        source_folder="/tmp/test",
        file_count=1,
        files_total=1,
        diagnostics={},
    )
    db_session.add(imp_series)
    await db_session.flush()
    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/test/Chicken Devil 004 (2022).cbz",
            file_name="Chicken Devil 004 (2022).cbz",
            file_format="cbz",
            parsed_series="Chicken Devils",
            parsed_issue_number=None,
            parsed_year=2022,
            diagnostics={
                "metadata_signals": {
                    "series_name": MetadataSignal.COMICINFO.value,
                    "year": MetadataSignal.COMICINFO.value,
                },
                "source_metadata": {
                    "comicinfo": {
                        "series": "Chicken Devils",
                        "number": "4",
                    },
                    "filename_parse": {
                        "series_name": "Chicken Devil",
                        "issue_number": None,
                        "year": 2022,
                        "volume": "v04",
                        "issue_type": IssueType.VOLUME.value,
                    },
                },
            },
        )
    )
    await db_session.flush()

    metadata = await source_metadata_for_matching_series(db_session, imp_series)

    assert metadata.signals["series_name"] == MetadataSignal.COMICINFO
    assert metadata.issue_number == 4.0
    assert metadata.diagnostics["issue_numbers"] == [4.0]
    assert metadata.diagnostics["comicinfo"]["series"] == "Chicken Devils"
    assert metadata.diagnostics["alternate_release_candidates"] == [
        {
            "series_name": "Chicken Devil",
            "year": 2022,
            "file_name": "Chicken Devil 004 (2022).cbz",
            "signal": MetadataSignal.RELEASE_TITLE.value,
        }
    ]


@pytest.mark.asyncio
async def test_source_metadata_for_matching_series_adds_volume_base_candidate(
    db_session: AsyncSession,
) -> None:
    job = await _create_import_job(db_session)
    imp_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Fearscape A Dark Interlude",
        raw_year=2023,
        source_folder="/tmp/test",
        file_count=1,
        files_total=1,
        diagnostics={"source_issue_type": IssueType.VOLUME.value},
    )
    db_session.add(imp_series)
    await db_session.flush()
    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/test/Fearscape.Vol.02.A.Dark.Interlude.2023.pdf",
            file_name="Fearscape.Vol.02.A.Dark.Interlude.2023.pdf",
            file_format="pdf",
            parsed_series="Fearscape A Dark Interlude",
            parsed_issue_number=None,
            parsed_year=2023,
            diagnostics={
                "source_issue_type": IssueType.VOLUME.value,
                "source_metadata": {
                    "filename_parse": {
                        "series_name": "Fearscape A Dark Interlude",
                        "issue_number": None,
                        "year": 2023,
                        "volume": "Vol 02",
                        "issue_type": IssueType.VOLUME.value,
                    }
                },
            },
        )
    )
    await db_session.flush()

    metadata = await source_metadata_for_matching_series(db_session, imp_series)

    assert metadata.issue_number == 2.0
    assert metadata.diagnostics["alternate_release_candidates"] == [
        {
            "series_name": "Fearscape",
            "year": 2023,
            "file_name": "Fearscape.Vol.02.A.Dark.Interlude.2023.pdf",
            "signal": MetadataSignal.RELEASE_TITLE.value,
            "issue_title_hint": "A Dark Interlude",
            "volume_issue_number": 2.0,
        }
    ]


@pytest.mark.asyncio
async def test_source_metadata_for_matching_series_preserves_same_base_volume_subtitle(
    db_session: AsyncSession,
) -> None:
    job = await _create_import_job(db_session)
    imp_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="The United States of Murder Inc",
        raw_year=2015,
        source_folder="/tmp/test",
        file_count=1,
        files_total=1,
        diagnostics={"source_issue_type": IssueType.VOLUME.value},
    )
    db_session.add(imp_series)
    await db_session.flush()
    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/test/The United States of Murder Inc. v01 - Truth (2015).cbz",
            file_name="The United States of Murder Inc. v01 - Truth (2015).cbz",
            file_format="cbz",
            parsed_series="The United States of Murder Inc",
            parsed_issue_number=1.0,
            parsed_year=2015,
            diagnostics={
                "source_issue_type": IssueType.VOLUME.value,
                "source_metadata": {
                    "filename_parse": {
                        "series_name": "The United States of Murder Inc. - Truth",
                        "issue_number": None,
                        "year": 2015,
                        "volume": "v01",
                        "issue_type": IssueType.VOLUME.value,
                    }
                },
            },
        )
    )
    await db_session.flush()

    metadata = await source_metadata_for_matching_series(db_session, imp_series)

    assert metadata.issue_type == IssueType.VOLUME
    assert metadata.issue_number == 1.0
    assert metadata.diagnostics["volume_subtitle_hint"] == {
        "base_series": "The United States of Murder Inc",
        "subtitle": "Truth",
        "issue_number": 1.0,
    }


@pytest.mark.asyncio
async def test_source_metadata_for_matching_series_adds_annual_release_candidate(
    db_session: AsyncSession,
) -> None:
    job = await _create_import_job(db_session)
    imp_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Absolute Wonder Woman",
        raw_year=2026,
        source_folder="/tmp/test",
        file_count=1,
        files_total=1,
        diagnostics={"source_issue_type": IssueType.ANNUAL.value},
    )
    db_session.add(imp_series)
    await db_session.flush()
    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/test/Absolute Wonder Woman 2026 Annual (2026) #001.cbz",
            file_name="Absolute Wonder Woman 2026 Annual (2026) #001.cbz",
            file_format="cbz",
            parsed_series="Absolute Wonder Woman",
            parsed_issue_number=1.0,
            parsed_year=2026,
            diagnostics={
                "source_issue_type": IssueType.ANNUAL.value,
                "source_metadata": {
                    "filename_parse": {
                        "series_name": "Absolute Wonder Woman",
                        "issue_number": 1.0,
                        "year": 2026,
                        "volume": None,
                        "issue_type": IssueType.ANNUAL.value,
                    }
                },
            },
        )
    )
    await db_session.flush()

    metadata = await source_metadata_for_matching_series(db_session, imp_series)

    assert metadata.diagnostics["alternate_release_candidates"] == [
        {
            "series_name": "Absolute Wonder Woman 2026 Annual",
            "year": 2026,
            "file_name": "Absolute Wonder Woman 2026 Annual (2026) #001.cbz",
            "signal": MetadataSignal.RELEASE_TITLE.value,
            "issue_type": IssueType.ANNUAL.value,
            "issue_type_qualified": True,
        },
        {
            "series_name": "Absolute Wonder Woman Annual",
            "year": 2026,
            "file_name": "Absolute Wonder Woman 2026 Annual (2026) #001.cbz",
            "signal": MetadataSignal.RELEASE_TITLE.value,
            "issue_type": IssueType.ANNUAL.value,
            "issue_type_qualified": True,
        },
    ]


@pytest.mark.asyncio
async def test_source_metadata_for_matching_series_uses_clean_one_shot_title(
    db_session: AsyncSession,
) -> None:
    job = await _create_import_job(db_session)
    imp_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Murder Drones - Home One Shot",
        raw_year=2026,
        source_folder="/tmp/test",
        file_count=1,
        files_total=1,
        diagnostics={},
    )
    db_session.add(imp_series)
    await db_session.flush()
    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/test/Murder Drones - Home 001 (OS) (2026) (Digital Rip).cbr",
            file_name="Murder Drones - Home 001 (OS) (2026) (Digital Rip).cbr",
            file_format="cbr",
            parsed_series="Murder Drones - Home",
            parsed_issue_number=1.0,
            parsed_year=2026,
            diagnostics={
                "source_issue_type": IssueType.ONE_SHOT.value,
                "metadata_signals": {"issue_type": MetadataSignal.RELEASE_TITLE.value},
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
    )
    await db_session.flush()

    metadata = await source_metadata_for_matching_series(db_session, imp_series)

    assert metadata.series_name == "Murder Drones - Home"
    assert metadata.issue_type == IssueType.ONE_SHOT
    assert metadata.issue_number == 1.0


@pytest.mark.asyncio
async def test_source_metadata_for_matching_series_adds_type_qualified_release_candidate(
    db_session: AsyncSession,
) -> None:
    job = await _create_import_job(db_session)
    imp_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Black Science",
        raw_year=2023,
        source_folder="/tmp/test",
        file_count=1,
        files_total=1,
        diagnostics={"source_issue_type": IssueType.COMPENDIUM.value},
    )
    db_session.add(imp_series)
    await db_session.flush()
    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path="/tmp/test/Black Science Compendium (2023) #001.cbz",
            file_name="Black Science Compendium (2023) #001.cbz",
            file_format="cbz",
            parsed_series="Black Science",
            parsed_issue_number=1.0,
            parsed_year=2023,
            diagnostics={
                "source_issue_type": IssueType.COMPENDIUM.value,
                "source_metadata": {
                    "filename_parse": {
                        "series_name": "Black Science",
                        "issue_number": 1.0,
                        "year": 2023,
                        "volume": None,
                        "issue_type": IssueType.COMPENDIUM.value,
                    }
                },
            },
        )
    )
    await db_session.flush()

    metadata = await source_metadata_for_matching_series(db_session, imp_series)

    assert metadata.diagnostics["alternate_release_candidates"] == [
        {
            "series_name": "Black Science Compendium",
            "year": 2023,
            "file_name": "Black Science Compendium (2023) #001.cbz",
            "signal": MetadataSignal.RELEASE_TITLE.value,
            "issue_type": IssueType.COMPENDIUM.value,
            "issue_type_qualified": True,
        }
    ]


@pytest.mark.asyncio
async def test_load_deferred_source_metadata_for_import_file_reads_archive_metadata(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "Batman 001.cbz"
    with zipfile.ZipFile(file_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "ComicInfo.xml",
            (
                "<?xml version='1.0'?>"
                "<ComicInfo>"
                "<Series>Batman</Series>"
                "<Number>1</Number>"
                "<Volume>2016</Volume>"
                "<Publisher>DC Comics</Publisher>"
                "<Notes>[cvid:97508]</Notes>"
                "</ComicInfo>"
            ),
        )

    imp_series = ImportedSeries(raw_series_name="Batman", raw_year=2016)
    imp_file = ImportedFile(
        file_path=str(file_path),
        file_name=file_path.name,
        parsed_series="Batman",
        parsed_issue_number=1.0,
        parsed_year=2016,
        diagnostics={
            "metadata_signals": {"series_name": MetadataSignal.RELEASE_TITLE.value},
            "source_metadata": {
                "archive_metadata_loaded": False,
                "archive_metadata_deferred": True,
                "has_comicinfo": False,
            },
        },
    )

    metadata = await load_deferred_source_metadata_for_import_file(imp_series, imp_file)

    assert metadata.series_name == "Batman"
    assert metadata.issue_number == 1.0
    assert metadata.publisher == "DC Comics"
    assert metadata.comicvine_series_id == 97508
    assert metadata.diagnostics["archive_metadata_loaded"] is True
    assert metadata.diagnostics["archive_metadata_deferred"] is False


@pytest.mark.asyncio
async def test_source_metadata_for_matching_series_can_load_deferred_archive_metadata(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "Batman 001.cbz"
    with zipfile.ZipFile(file_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "ComicInfo.xml",
            (
                "<?xml version='1.0'?>"
                "<ComicInfo>"
                "<Series>Batman</Series>"
                "<Number>1</Number>"
                "<Volume>2016</Volume>"
                "<Publisher>DC Comics</Publisher>"
                "</ComicInfo>"
            ),
        )

    job = await _create_import_job(db_session)
    imp_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Batman",
        raw_year=2016,
        source_folder=str(tmp_path),
        file_count=1,
        files_total=1,
        diagnostics={},
    )
    db_session.add(imp_series)
    await db_session.flush()
    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path=str(file_path),
            file_name=file_path.name,
            file_format="cbz",
            parsed_series="Batman",
            parsed_issue_number=1.0,
            parsed_year=2016,
            diagnostics={
                "metadata_signals": {"series_name": MetadataSignal.RELEASE_TITLE.value},
                "source_metadata": {
                    "archive_metadata_loaded": False,
                    "archive_metadata_deferred": True,
                    "has_comicinfo": False,
                },
            },
        )
    )
    await db_session.flush()

    metadata = await source_metadata_for_matching_series(
        db_session,
        imp_series,
        load_deferred_archive_metadata=True,
    )

    assert metadata.signals["series_name"] == MetadataSignal.COMICINFO
    assert metadata.issue_number == 1.0
    assert metadata.diagnostics["comicinfo"]["publisher"] == "DC Comics"


@pytest.mark.asyncio
async def test_source_metadata_for_matching_series_preserves_scanned_sidecar_identity(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "Batman 001.cbz"
    file_path.write_bytes(b"already scanned")
    job = await _create_import_job(db_session)
    imp_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Batman",
        raw_year=2016,
        source_folder=str(tmp_path),
        file_count=1,
        files_total=1,
        cv_id=97508,
        cv_match_method="comicinfo_cv_id",
        diagnostics={},
    )
    db_session.add(imp_series)
    await db_session.flush()
    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path=str(file_path),
            file_name=file_path.name,
            file_format="cbz",
            parsed_series="Batman",
            parsed_issue_number=1.0,
            parsed_year=2016,
            diagnostics={
                "comicvine_series_id": 97508,
                "metadata_signals": {
                    "comicvine_series_id": MetadataSignal.SIDECAR.value,
                },
                "source_metadata": {
                    "archive_metadata_loaded": False,
                    "archive_metadata_deferred": True,
                },
            },
        )
    )
    await db_session.flush()

    metadata = await source_metadata_for_matching_series(db_session, imp_series)

    assert metadata.comicvine_series_id == 97508
    assert metadata.signals["comicvine_series_id"] == MetadataSignal.SIDECAR
    assert metadata.diagnostics["comicvine_series_id_source"] == "sidecar"


def test_build_import_metadata_conflict_requires_strong_title_mismatch() -> None:
    metadata = SourceMetadata(
        original_title="Chicken Devil 004 (2022).cbz",
        source_path="/tmp/Chicken Devil 004 (2022).cbz",
        series_name="Chicken Devil",
        year=2022,
        issue_number=4.0,
        comicvine_issue_id=905404,
        signals={"series_name": MetadataSignal.COMICINFO},
        diagnostics={"comicinfo": {"series": "Chicken Devil"}},
    )

    conflict = build_import_metadata_conflict(
        metadata=metadata,
        target_series_title="Chicken Devils",
        target_series_year=2022,
        target_issue_number=4.0,
        target_issue_cv_id=905404,
        target_issue_title="The Chicken is in the Details",
    )

    assert conflict is not None
    assert conflict["kind"] == "metadata_conflict"
    assert conflict["target_series"] == "Chicken Devils"
    assert conflict["source_series"] == "Chicken Devil"
    assert conflict["comicinfo"] == {"series": "Chicken Devil"}


def test_build_import_metadata_conflict_blocks_archive_entry_issue_mismatch() -> None:
    metadata = SourceMetadata(
        original_title="Hello Darkness 020 (2026).cbz",
        series_name="Hello Darkness",
        issue_number=20.0,
        year=2026,
        diagnostics={
            "archive_entry_issue_hint": {
                "series_name": "Hello Darkness",
                "issue_number": 21.0,
                "year": 2026,
                "confidence": "strong",
                "matching_entry_count": 47,
                "total_image_entries": 48,
                "parseable_image_entries": 47,
            }
        },
    )

    conflict = build_import_metadata_conflict(
        metadata=metadata,
        target_series_title="Hello Darkness",
        target_series_year=2024,
        target_issue_number=20.0,
        target_issue_cv_id=1162482,
        target_issue_title="Away Message",
    )

    assert conflict == {
        "kind": "metadata_conflict",
        "conflict_type": "archive_entry_issue_number_mismatch",
        "preserve_series_match": True,
        "rejection_reason": (
            "Filename parsed as issue #20, but archive page names strongly suggest issue #21."
        ),
        "source_series": "Hello Darkness",
        "source_year": 2026,
        "source_issue_number": 20.0,
        "target_series": "Hello Darkness",
        "target_series_year": 2024,
        "target_issue_number": 20.0,
        "target_issue_title": "Away Message",
        "target_issue_cv_id": 1162482,
        "suggested_issue_number": 21.0,
        "archive_entry_issue_hint": {
            "series_name": "Hello Darkness",
            "issue_number": 21.0,
            "year": 2026,
            "confidence": "strong",
            "matching_entry_count": 47,
            "total_image_entries": 48,
            "parseable_image_entries": 47,
        },
    }


def test_sync_import_file_source_metadata_refreshes_repaired_file(tmp_path: Path) -> None:
    source_path = tmp_path / "Chicken Devil 004 (2022).cbz"
    source_path.write_bytes(b"fake archive bytes")
    imp_file = ImportedFile(
        file_path="/tmp/original.cbz",
        file_name="Chicken Devil 004 (2022).cbz",
        diagnostics={},
    )
    metadata = SourceMetadata(
        original_title=source_path.name,
        source_path=str(source_path),
        series_name="Chicken Devil",
        year=2022,
        issue_number=4.0,
        issue_type=IssueType.ISSUE,
        comicvine_series_id=97508,
        comicvine_issue_id=905404,
        signals={
            "series_name": MetadataSignal.COMICINFO,
            "comicvine_series_id": MetadataSignal.COMICINFO,
        },
        diagnostics={"has_comicinfo": True, "comicinfo": {"number": "4"}},
    )

    diagnostics = sync_import_file_source_metadata(imp_file, source_path, metadata)

    assert imp_file.file_path == str(source_path)
    assert imp_file.file_name == source_path.name
    assert imp_file.file_format == "cbz"
    assert imp_file.has_comicinfo is True
    assert imp_file.issue_number_raw == "4"
    assert diagnostics["source_issue_type"] == "issue"
    assert diagnostics["comicvine_series_id"] == 97508
    assert diagnostics["metadata_signals"] == {
        "series_name": "comicinfo",
        "comicvine_series_id": "comicinfo",
    }
