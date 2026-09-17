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


def test_mylar_parent_identity_does_not_hide_different_file_titles() -> None:
    files = [
        _file("New Avengers 001 (2005).cbr", 2004, "New Avengers", 1),
        _file("New Avengers Finale 01 (2010).cbr", 2004, "New Avengers", 2),
    ]
    files[1].file_size = 900
    assert classify_conflict_group(files) == "series_mismatch"
    detect_conflicts(files, 0)
    assert not any(file.is_preferred for file in files)


def test_mylar_start_year_does_not_hide_file_year_disagreement() -> None:
    files = [
        _file("Excalibur 002 (1988).cbz", 1988, "Excalibur", 1),
        _file("Excalibur 002 (1994).cbz", 1988, "Excalibur", 2),
    ]
    assert classify_conflict_group(files) == "year_disagreement"


def test_series_start_year_is_not_compared_to_annual_publication_year() -> None:
    files = [
        _file("Justice League Dark Annual 002 (2014).cbz", 2012, "Justice League Dark Annual", 1),
        _file("Justice League Dark Annual 002 (2014).cbr", 2014, "Justice League Dark Annual", 2),
    ]
    assert classify_conflict_group(files) == "duplicate_copy"


def test_identical_bytes_still_prove_a_duplicate_despite_filename_drift() -> None:
    files = [
        _file("Excalibur 002 (1988).cbz", 1988, "Excalibur", 1),
        _file("Excalibur 002 (1994).cbz", 1988, "Excalibur", 2),
    ]
    for file in files:
        file.content_hash = "same-content-digest"
    assert classify_conflict_group(files) == "identical_copy"


def test_release_site_suffix_is_not_a_different_comic() -> None:
    files = [
        _file("X-Men 026 (2015) GetComics.INFO.cbz", 2013, "X-Men", 1),
        _file("X-Men 026 (2015).cbz", 2013, "X-Men", 2),
    ]
    assert classify_conflict_group(files) == "duplicate_copy"


def test_scan_label_is_not_a_different_comic_but_a_subtitle_still_is() -> None:
    first = _file("Batman 001 (2016).cbz", 2016, "Batman", 1)
    scan = _file("Batman 001 (2016) (scan).cbz", 2016, "Batman", 2)
    assert classify_conflict_group([first, scan]) == "duplicate_copy"
    subtitle = _file("Batman Finale 001 (2016).cbz", 2016, "Batman", 3)
    assert classify_conflict_group([first, subtitle]) == "series_mismatch"
