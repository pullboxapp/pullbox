"""Local metadata must resolve volumes and annuals without borrowing stale issue IDs."""

import zipfile
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.services import import_file_matching
from pullbox.services.import_known_annuals import reassign_to_known_annual_series
from pullbox.services.import_service import ImportService
from pullbox.services.import_source_metadata import load_deferred_source_metadata_for_import_file


async def _job_series(session, path, source_type, mode, *, title, year, cv_id):
    job = ImportJob(
        source_path=str(path),
        source_type=source_type,
        file_handling_mode=mode,
        status=ImportJobStatus.FILE_MATCHING,
    )
    session.add(job)
    await session.flush()
    series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=title,
        raw_year=year,
        source_folder=str(path),
        cv_title=title,
        cv_year=year,
        cv_id=cv_id,
        cv_match_method="mylar3_cv_id"
        if source_type == ImportSourceType.MYLAR3
        else "folder_cv_id",
        cv_match_score=1,
        status=ImportSeriesStatus.MATCHED,
    )
    session.add(series)
    await session.flush()
    return job, series


def _file(job, series, path, *, number=None, issue_type="issue", xml=None):
    if xml is not None:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("ComicInfo.xml", xml)
            archive.writestr("001.jpg", b"page one")
            archive.writestr("002.jpg", b"page two")
    return ImportedFile(
        import_job_id=job.id,
        import_series_id=series.id,
        file_path=str(path),
        file_name=path.name,
        file_format="cbz",
        file_size=path.stat().st_size if path.exists() else 0,
        parsed_series=series.raw_series_name,
        parsed_year=series.raw_year,
        parsed_issue_number=number,
        issue_number_raw=str(number) if number is not None else None,
        status=ImportedFileStatus.PENDING,
        diagnostics={
            "source_issue_type": issue_type,
            "comicvine_series_id": series.cv_id,
            "metadata_signals": {
                "comicvine_series_id": "mylar3"
                if job.source_type == ImportSourceType.MYLAR3
                else "sidecar"
            },
            "source_metadata": {
                "archive_metadata_deferred": True,
                "mylar3_unrecorded_file": {"expected_series": series.raw_series_name},
            },
        },
    )


async def _match(session, job):
    await session.commit()
    provider = Mock()
    provider.cache_metrics.return_value = {}
    for name in (
        "get_series",
        "get_issue",
        "get_issues_for_series",
        "get_issues_for_series_by_numbers",
    ):
        setattr(provider, name, AsyncMock(side_effect=AssertionError("Unexpected provider call")))
    service = ImportService(
        series_service=AsyncMock(),
        metadata_service=SimpleNamespace(_provider=provider),
        event_bus=AsyncMock(),
    )
    await service._run_file_matching(session, job)
    assert not [call for call in provider.mock_calls if "get_" in call[0]]


@pytest.mark.parametrize("source_type", list(ImportSourceType))
@pytest.mark.parametrize("mode", list(ImportFileHandlingMode))
@pytest.mark.parametrize("number,year", [(9, 2020), (15, 2021), (16, 2021)])
@pytest.mark.parametrize("copies", [1, 2])
async def test_deferred_volume_evidence_matches_without_stale_mylar_identity(
    db_session, tmp_path, source_type, mode, number, year, copies
):
    job, series = await _job_series(
        db_session, tmp_path, source_type, mode, title="Dawn of X", year=2020, cv_id=124996
    )
    file = _file(
        job,
        series,
        tmp_path / f"Dawn of X v{number:02d} ({year}).cbz",
        xml=f"<ComicInfo><Series>Dawn Of X</Series><Title>v{number:02d}</Title>"
        f"<Year>{year}</Year></ComicInfo>",
    )
    db_session.add(file)
    before = (tmp_path / file.file_name).read_bytes()
    duplicate = None
    if copies == 2:
        duplicate = _file(
            job,
            series,
            tmp_path / f"Dawn of X v{number:02d} ({year}) (Digital).cbz",
            xml=f"<ComicInfo><Series>Dawn Of X</Series><Title>Alternate cover</Title>"
            f"<Year>{year}</Year></ComicInfo>",
        )
        db_session.add(duplicate)

    await _match(db_session, job)

    assert file.status == (
        ImportedFileStatus.MATCHED if copies == 1 else ImportedFileStatus.CONFLICT
    )
    assert file.matched_issue_cv_id is None
    assert file.parsed_issue_number == number
    details = file.diagnostics.get("previous_diagnostics", file.diagnostics)
    assert details["target_issue_number"] == number
    assert details["target_issue_type"] == "volume"
    if duplicate is not None:
        assert duplicate.status == ImportedFileStatus.CONFLICT
        assert duplicate.conflict_group_id == file.conflict_group_id
    assert (tmp_path / file.file_name).read_bytes() == before


@pytest.mark.parametrize("source_type", list(ImportSourceType))
@pytest.mark.parametrize("mode", list(ImportFileHandlingMode))
@pytest.mark.parametrize("page_size", [1, 250])
@pytest.mark.parametrize("copies", [1, 2])
@pytest.mark.parametrize(
    "title,parent_year,year,parent_cv,annual_cv,embedded_title",
    [
        ("X-Men", 2021, 2023, 137402, 146988, "X-Men (2021-) Annual"),
        ("Marauders", 2022, 2022, 142135, 141459, "Marauders Annual (2022)"),
    ],
)
async def test_corroborated_annual_uses_known_annual_series_without_consuming_missing_reference(
    db_session,
    tmp_path,
    source_type,
    mode,
    title,
    parent_year,
    year,
    parent_cv,
    annual_cv,
    embedded_title,
    monkeypatch,
    page_size,
    copies,
):
    job, parent = await _job_series(
        db_session, tmp_path, source_type, mode, title=title, year=parent_year, cv_id=parent_cv
    )
    annual = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=f"{title} Annual",
        raw_year=year,
        source_folder=str(tmp_path),
        cv_title=f"{title} Annual",
        cv_year=year,
        cv_id=annual_cv,
        cv_match_method=parent.cv_match_method,
        cv_match_score=1,
        status=ImportSeriesStatus.MATCHED,
        diagnostics={"source_issue_type": "annual"},
    )
    db_session.add(annual)
    await db_session.flush()
    actual = _file(
        job,
        parent,
        tmp_path / f"{title} Annual 001 ({year}).cbz",
        number=1,
        issue_type="annual",
        xml=f"<ComicInfo><Series>{embedded_title}</Series><Number>1</Number></ComicInfo>",
    )
    missing = _file(
        job,
        annual,
        tmp_path / f"{title} Annual 001 ({year}) (Digital) (Group).cbz",
        number=1,
        issue_type="annual",
    )
    missing.status = ImportedFileStatus.SAFETY_BLOCKED
    missing.comicvine_issue_id = 999999
    missing.diagnostics = {"safety_block": {"code": "source_missing", "category": "source_missing"}}
    db_session.add_all([actual, missing])
    regular = _file(
        job,
        parent,
        tmp_path / f"{title} 001 ({parent_year}).cbz",
        number=1,
        xml=f"<ComicInfo><Series>{title}</Series><Number>1</Number></ComicInfo>",
    )
    db_session.add(regular)
    duplicate = None
    if copies == 2:
        duplicate = _file(
            job,
            parent,
            tmp_path / f"{title} Annual 01 ({year}).cbz",
            number=1,
            issue_type="annual",
            xml=f"<ComicInfo><Series>{embedded_title}</Series><Number>1</Number>"
            "<Title>Alternate cover</Title></ComicInfo>",
        )
        db_session.add(duplicate)
    before = (tmp_path / actual.file_name).read_bytes()
    monkeypatch.setattr(import_file_matching, "_FILE_PAGE_SIZE", page_size)

    await _match(db_session, job)

    assert actual.import_series_id == annual.id
    expected_status = ImportedFileStatus.MATCHED if copies == 1 else ImportedFileStatus.CONFLICT
    assert actual.status == expected_status
    assert actual.matched_issue_cv_id is None
    assert actual.comicvine_issue_id is None
    details = actual.diagnostics.get("previous_diagnostics", actual.diagnostics)
    assert details["target_series_cv_id"] == annual_cv
    assert details["target_issue_type"] == "annual"
    assert details["source_metadata"]["known_annual_series"]["target_series_cv_id"] == annual_cv
    if duplicate is not None:
        assert duplicate.import_series_id == annual.id
        assert duplicate.status == ImportedFileStatus.CONFLICT
        assert duplicate.conflict_group_id == actual.conflict_group_id
    assert missing.status == ImportedFileStatus.SAFETY_BLOCKED
    assert missing.comicvine_issue_id == 999999
    assert regular.import_series_id == parent.id
    assert regular.status == ImportedFileStatus.MATCHED
    assert regular.conflict_group_id is None
    assert regular.diagnostics["target_issue_type"] == "issue"
    assert job.total_files_matched == (2 if copies == 1 else 1)
    assert job.total_files_conflict == (0 if copies == 1 else 2)
    assert (tmp_path / actual.file_name).read_bytes() == before


@pytest.mark.parametrize(
    "case",
    [
        "wrong_title",
        "wrong_number",
        "wrong_year",
        "wrong_title_year",
        "other_type",
        "no_comicinfo",
        "source_issue_id",
        "source_series_id",
        "identity_conflict",
        "blocked",
        "approved",
        "manual_file",
        "selected_file",
        "registered_file",
        "manual_parent",
        "selected_parent",
        "manual_target",
        "selected_target",
        "skipped_target",
        "uncertain_target",
        "other_folder",
        "other_job",
        "ambiguous_target",
        "no_target_year",
    ],
)
async def test_known_annual_reassignment_preserves_uncertain_or_reviewed_sources(
    db_session, tmp_path, case
):
    job, parent = await _job_series(
        db_session,
        tmp_path,
        ImportSourceType.MYLAR3,
        ImportFileHandlingMode.IN_PLACE,
        title="Marauders",
        year=2022,
        cv_id=142135,
    )
    target = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Marauders Annual",
        raw_year=2022,
        source_folder=str(tmp_path),
        cv_title="Marauders Annual",
        cv_year=2022,
        cv_id=141459,
        cv_match_method="mylar3_cv_id",
        cv_match_score=1,
        status=ImportSeriesStatus.MATCHED,
    )
    db_session.add(target)
    await db_session.flush()
    actual = _file(
        job,
        parent,
        tmp_path / "Marauders Annual 001 (2022).cbz",
        number=1,
        issue_type="annual",
        xml="<ComicInfo><Series>Marauders Annual (2022)</Series><Number>1</Number></ComicInfo>",
    )
    metadata = await load_deferred_source_metadata_for_import_file(parent, actual)
    source = deepcopy(metadata.diagnostics)
    if case in {"wrong_title", "wrong_number", "wrong_year", "wrong_title_year"}:
        field, value = {
            "wrong_title": ("series", "X-Men Annual"),
            "wrong_number": ("number", "2"),
            "wrong_year": ("year", 2023),
            "wrong_title_year": ("series", "Marauders Annual (2023)"),
        }[case]
        source["comicinfo"][field] = value
    elif case == "other_type":
        from pullbox.models.issue import IssueType

        metadata = metadata.model_copy(update={"issue_type": IssueType.ISSUE})
    elif case == "no_comicinfo":
        source.pop("comicinfo")
    elif case == "source_issue_id":
        actual.comicvine_issue_id = 555
    elif case == "source_series_id":
        from pullbox.core.source_metadata import MetadataSignal

        metadata = metadata.model_copy(
            update={
                "signals": {**metadata.signals, "comicvine_series_id": MetadataSignal.COMICINFO}
            }
        )
    elif case == "identity_conflict":
        source["identity_conflicts"] = [{"field": "comicvine_issue_id"}]
    elif case in {"blocked", "approved"}:
        actual.status = (
            ImportedFileStatus.SAFETY_BLOCKED
            if case == "blocked"
            else ImportedFileStatus.SAFETY_APPROVED
        )
    elif case == "manual_file":
        actual.match_method = "manual"
    elif case == "selected_file":
        actual.include_in_import = True
    elif case == "registered_file":
        actual.library_file_id = 42
    elif case == "manual_parent":
        parent.user_selected_cv_id = parent.cv_id
    elif case == "selected_parent":
        parent.selected_for_import = True
    elif case == "manual_target":
        target.user_selected_cv_id = target.cv_id
    elif case == "selected_target":
        target.selected_for_import = True
    elif case == "skipped_target":
        target.status = ImportSeriesStatus.SKIPPED
    elif case == "uncertain_target":
        target.cv_match_method = "search"
    elif case == "other_folder":
        target.source_folder = str(tmp_path / "other")
    elif case == "other_job":
        other_job = ImportJob(source_path="/elsewhere", source_type=ImportSourceType.MYLAR3)
        db_session.add(other_job)
        await db_session.flush()
        target.import_job_id = other_job.id
    elif case == "ambiguous_target":
        db_session.add(
            ImportedSeries(
                import_job_id=job.id,
                raw_series_name="Marauders Annual",
                raw_year=2022,
                cv_title="Marauders Annual",
                cv_year=2022,
                source_folder=str(tmp_path),
                cv_id=999,
                cv_match_method="mylar3_cv_id",
                cv_match_score=1,
                status=ImportSeriesStatus.MATCHED,
            )
        )
    elif case == "no_target_year":
        target.cv_year = None
    await db_session.flush()
    metadata = metadata.model_copy(update={"diagnostics": source})
    before = deepcopy(actual.diagnostics)

    result = await reassign_to_known_annual_series(db_session, parent, actual, metadata)

    assert result is None
    assert actual.import_series_id == parent.id
    assert actual.diagnostics == before
