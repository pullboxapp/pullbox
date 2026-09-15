"""Duplicate suggestions must distinguish mismatched comic identities."""

from pullbox.models.import_job import ImportedFile, ImportedFileStatus
from pullbox.services.import_file_conflicts import classify_conflict_group, detect_conflicts


def _file(name: str, year: int, series: str, number: int) -> ImportedFile:
    return ImportedFile(
        id=number,
        import_job_id=1,
        import_series_id=1,
        file_name=name,
        file_path="/fixture/" + name,
        file_format="cbz",
        file_size=100,
        parsed_series=series,
        parsed_year=year,
        matched_issue_cv_id=123,
        status=ImportedFileStatus.MATCHED,
        match_confidence="high",
        has_comicinfo=False,
    )


def test_different_titles_do_not_get_a_keeper_recommendation() -> None:
    files = [
        _file("New Avengers 001.cbz", 2015, "New Avengers", 1),
        _file("New Avengers Finale 001.cbz", 2015, "New Avengers Finale", 2),
    ]
    assert classify_conflict_group(files) == "series_mismatch"
    detect_conflicts(files, 0)
    assert not any(file.is_preferred for file in files)
    assert all(file.diagnostics["conflict_class"] == "series_mismatch" for file in files)


def test_year_disagreement_is_reviewed_without_rejecting_either_file() -> None:
    files = [
        _file("Excalibur 002 (1988).cbz", 1988, "Excalibur", 1),
        _file("Excalibur 002 (1994).cbz", 1994, "Excalibur", 2),
    ]
    assert classify_conflict_group(files) == "year_disagreement"
    detect_conflicts(files, 0)
    assert all(file.status == ImportedFileStatus.CONFLICT for file in files)
    assert not any(file.is_preferred for file in files)


def test_normal_copies_keep_the_existing_recommendation() -> None:
    files = [
        _file("Young X-Men 005 scan1.cbz", 2008, "Young X-Men", 1),
        _file("Young X-Men 005 scan2.cbz", 2008, "Young X-Men", 2),
    ]
    assert classify_conflict_group(files) == "duplicate_copy"
    detect_conflicts(files, 0)
    assert sum(bool(file.is_preferred) for file in files) == 1
