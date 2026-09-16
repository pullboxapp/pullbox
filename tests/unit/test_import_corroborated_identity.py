"""Contradictory file evidence must survive trusted parent Mylar identity."""

from unittest.mock import Mock

import pytest

from pullbox.core.source_metadata import SourceMetadata
from pullbox.models.import_job import ImportedFile, ImportedSeries, ImportSeriesStatus
from pullbox.models.issue import IssueType
from pullbox.services.import_file_match_targets import FileMatchTargetIndex
from pullbox.services.import_file_matching import _evaluate_file_match_candidate
from pullbox.services.import_source_metadata import (
    build_import_metadata_conflict,
    corroborated_import_title_conflict,
)
from pullbox.services.semantic_matching import ImportPolicy, SemanticMatchEngine


@pytest.mark.parametrize("recorded", [False, True])
@pytest.mark.parametrize("file_title", ["New Avengers Finale", "New Avengers - Ultron Forever"])
def test_corroborated_distinct_title_blocks_id_and_number_shortcuts(recorded, file_title):
    parent = ImportedSeries(
        raw_series_name="New Avengers",
        cv_title="New Avengers",
        cv_id=100,
        cv_match_method="mylar3_cv_id",
        status=ImportSeriesStatus.MATCHED,
    )
    file = ImportedFile(
        file_name=f"{file_title} 001 (2015).cbr",
        parsed_series="New Avengers",
        parsed_issue_number=1,
        parsed_year=2013,
        comicvine_issue_id=200 if recorded else None,
        diagnostics={
            "comicvine_series_id": 100,
            "metadata_signals": {
                "comicvine_issue_id": "mylar3",
                "comicvine_series_id": "mylar3",
            },
        },
    )
    metadata = SourceMetadata(
        original_title=file.file_name,
        series_name="New Avengers",
        issue_number=1,
        issue_type=IssueType.ISSUE,
        comicvine_issue_id=file.comicvine_issue_id,
        diagnostics={
            "filename_parse": {"series_name": file_title, "issue_number": 1, "year": 2015},
            "archive_entry_issue_hint": {
                "series_name": file_title,
                "issue_number": 1,
                "confidence": "strong",
            },
        },
    )
    entry = (None, 200, False, None, "Breakout")
    candidate, conflict = _evaluate_file_match_candidate(
        imp_series=parent,
        imp_file=file,
        target_index=FileMatchTargetIndex(cv_id_map={200: entry}, number_map={1: entry}),
        target_series=None,
        file_metadata=metadata,
        semantic_match_engine=SemanticMatchEngine(policy=ImportPolicy()),
        build_import_metadata_conflict=build_import_metadata_conflict,
        series_high_confidence=True,
    )
    assert candidate is None
    assert conflict["conflict_type"] == "corroborated_file_series_mismatch"
    assert conflict["preserve_series_match"] is True
    assert parent.cv_id == 100


def test_uncontradicted_mylar_identity_keeps_local_fast_path():
    parent = ImportedSeries(raw_series_name="New Avengers", cv_id=100)
    file = ImportedFile(
        file_name="New Avengers 001 (2013).cbz",
        comicvine_issue_id=200,
        parsed_issue_number=1,
        diagnostics={
            "comicvine_series_id": 100,
            "metadata_signals": {
                "comicvine_issue_id": "mylar3",
                "comicvine_series_id": "mylar3",
            },
        },
    )
    engine = Mock()
    entry = (None, 200, False, None, "Breakout")
    candidate, conflict = _evaluate_file_match_candidate(
        imp_series=parent,
        imp_file=file,
        target_index=FileMatchTargetIndex(cv_id_map={200: entry}),
        target_series=None,
        file_metadata=SourceMetadata(original_title=file.file_name),
        semantic_match_engine=engine,
        build_import_metadata_conflict=build_import_metadata_conflict,
        series_high_confidence=True,
    )
    assert candidate is not None
    assert conflict is None
    engine.match_against_issue.assert_not_called()


@pytest.mark.parametrize("corroboration", ["comicinfo", "archive_entry_issue_hint"])
def test_local_title_guard_is_shared_by_source_types(corroboration):
    metadata = SourceMetadata(
        original_title="New Avengers Finale 01 (2010).cbz",
        diagnostics={
            "filename_parse": {"series_name": "New Avengers Finale"},
            corroboration: {
                "series": "New Avengers Finale",
                "series_name": "New Avengers Finale",
                "issue_number": 1,
                "confidence": "strong",
            },
        },
    )
    assert corroborated_import_title_conflict(metadata, "New Avengers") is not None


@pytest.mark.parametrize(
    "issue_type,confidence,title",
    [
        (IssueType.ISSUE, "weak", "New Avengers Finale"),
        (IssueType.TPB, "strong", "New Avengers Finale"),
        (IssueType.ISSUE, "strong", "The New Avengers"),
    ],
)
def test_title_guard_preserves_weak_evidence_collections_and_article_variants(
    issue_type, confidence, title
):
    metadata = SourceMetadata(
        original_title=f"{title} 01 (2010).cbz",
        issue_type=issue_type,
        diagnostics={
            "filename_parse": {"series_name": title},
            "archive_entry_issue_hint": {
                "series_name": title,
                "issue_number": 1,
                "confidence": confidence,
            },
        },
    )
    assert corroborated_import_title_conflict(metadata, "New Avengers") is None


@pytest.mark.parametrize(
    "source_title,target_title,issue_type",
    [
        ("X-Men Red", "X-Men: Red Annual", IssueType.ANNUAL),
        ("Justice League", "Justice League 2022 Annual", IssueType.ANNUAL),
        ("Justice League - Darkseid War", "Justice League Darkseid War Special", IssueType.SPECIAL),
        ("Cataclysm - Ultimate Comics Ultimates", "Cataclysm: The Ultimates", IssueType.ISSUE),
    ],
)
def test_subtitle_guard_does_not_replace_type_or_alternate_title_matching(
    source_title, target_title, issue_type
):
    metadata = SourceMetadata(
        original_title=f"{source_title} 001 (2015).cbr",
        issue_type=issue_type,
        diagnostics={
            "filename_parse": {"series_name": source_title},
            "archive_entry_issue_hint": {
                "series_name": source_title,
                "issue_number": 1,
                "confidence": "strong",
            },
        },
    )
    assert corroborated_import_title_conflict(metadata, target_title) is None
