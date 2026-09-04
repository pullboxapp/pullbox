"""Tests for import file-match outcome mutation helpers."""

from __future__ import annotations

import pytest

from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.services.import_duplicates import DuplicateMergeProfile, duplicate_target_state
from pullbox.services.import_file_match_candidates import FileMatchCandidate
from pullbox.services.import_file_match_outcomes import (
    apply_and_log_file_match_outcome,
    apply_matched_file_outcome,
    apply_unmatched_file_outcome,
)


def _candidate(
    issue: Issue | None = None,
    *,
    has_library_file: bool = False,
    confidence: str = "high",
    method: str = "comicvine_id",
) -> FileMatchCandidate:
    return FileMatchCandidate(
        matched_issue_id=issue.id if issue is not None else None,
        matched_issue_cv_id=issue.comicvine_id if issue is not None else 1001,
        target_issue_number=issue.issue_number if issue is not None else None,
        has_library_file=has_library_file,
        matched_issue=issue,
        target_issue_title=issue.title if issue is not None else None,
        confidence=confidence,
        method=method,
    )


def test_apply_matched_file_outcome_marks_new_series_file_matched() -> None:
    imp_file = ImportedFile(file_name="Absolute Wonder Woman 001.cbz", parsed_issue_number=1.0)
    imp_series = ImportedSeries(raw_series_name="Absolute Wonder Woman")
    candidate = _candidate(None, confidence="medium", method="issue_number")

    event = apply_matched_file_outcome(
        imp_file,
        imp_series,
        candidate,
        duplicate_series=False,
        duplicate_target_state=duplicate_target_state,
    )

    assert imp_file.status == ImportedFileStatus.MATCHED
    assert imp_file.include_in_import is False
    assert imp_file.matched_issue_id is None
    assert imp_file.matched_issue_cv_id == 1001
    assert imp_file.match_confidence == "medium"
    assert imp_file.match_method == "issue_number"
    assert imp_file.diagnostics == {
        "target_issue_summary": {
            "provider_id": "1001",
            "issue_number": 1.0,
            "title": None,
            "release_date": None,
            "cover_url": None,
            "issue_type": IssueType.ISSUE.value,
        }
    }
    assert event.name == "import_file_match_detail"
    assert event.message == "File matched: Absolute Wonder Woman 001.cbz"
    assert event.data["method"] == "issue_number"
    assert event.data["confidence"] == "medium"


def test_apply_matched_file_outcome_preserves_exact_suffix_issue_number() -> None:
    imp_file = ImportedFile(
        file_name="The Amazing Spider-Man 54.LR.cbz",
        parsed_issue_number=54.0,
        issue_number_raw="54.LR",
    )
    imp_series = ImportedSeries(raw_series_name="The Amazing Spider-Man")
    candidate = FileMatchCandidate(
        matched_issue_id=None,
        matched_issue_cv_id=123456,
        target_issue_number=54.0,
        has_library_file=False,
        matched_issue=None,
        target_issue_title="Last Remains",
        confidence="high",
        method="comicvine_id",
    )

    apply_matched_file_outcome(
        imp_file,
        imp_series,
        candidate,
        duplicate_series=False,
        duplicate_target_state=duplicate_target_state,
    )

    assert imp_file.diagnostics["target_issue_summary"]["issue_number_text"] == "54LR"


def test_apply_unmatched_file_outcome_always_records_specific_reason() -> None:
    imp_file = ImportedFile(
        file_name="Unknown Special.cbz",
        parsed_issue_number=None,
        diagnostics={"source_metadata": {"filename_parse": {"series": "Unknown Special"}}},
    )
    imp_series = ImportedSeries(raw_series_name="Unknown Special")

    apply_unmatched_file_outcome(
        imp_file,
        imp_series,
        duplicate_series=False,
        duplicate_merge_profile=None,
        metadata_conflict=None,
    )

    assert imp_file.diagnostics["reason"] == "issue_target_not_found"
    assert imp_file.diagnostics["rejection_reason"] == (
        "No issue target matched the available file name and metadata evidence."
    )


def test_apply_matched_file_outcome_marks_multi_entry_graphic_novel_as_volume() -> None:
    imp_file = ImportedFile(
        file_name="AL15 002 (2022) (Graphic Novel) (AAM-Markosia) (Digital-HD).cbz",
        parsed_issue_number=2.0,
        diagnostics={"source_metadata": {"filename_parse": {"issue_type": IssueType.GN.value}}},
    )
    imp_series = ImportedSeries(raw_series_name="AL15", file_count=3)
    candidate = _candidate(None, confidence="high", method="issue_number")

    apply_matched_file_outcome(
        imp_file,
        imp_series,
        candidate,
        duplicate_series=False,
        duplicate_target_state=duplicate_target_state,
    )

    assert imp_file.diagnostics == {
        "source_metadata": {"filename_parse": {"issue_type": IssueType.GN.value}},
        "target_issue_summary": {
            "provider_id": "1001",
            "issue_number": 2.0,
            "title": None,
            "release_date": None,
            "cover_url": None,
            "issue_type": IssueType.VOLUME.value,
        },
    }


def test_apply_matched_file_outcome_marks_duplicate_already_owned() -> None:
    issue = Issue(
        id=7,
        issue_number=19.0,
        comicvine_id=1019,
        title="Issue 19",
        status=IssueStatus.OWNED,
    )
    imp_file = ImportedFile(file_name="Absolute Batman 019.cbz")
    imp_series = ImportedSeries(raw_series_name="Absolute Batman")

    event = apply_matched_file_outcome(
        imp_file,
        imp_series,
        _candidate(issue, has_library_file=True),
        duplicate_series=True,
        duplicate_target_state=duplicate_target_state,
    )

    assert imp_file.status == ImportedFileStatus.ALREADY_OWNED
    assert imp_file.include_in_import is False
    assert imp_file.diagnostics == {
        "kind": "duplicate_series_file",
        "target_state": "already_owned",
        "target_issue_id": 7,
        "target_issue_cv_id": 1019,
        "target_issue_number": 19.0,
        "target_issue_title": "Issue 19",
    }
    assert event.name == "import_duplicate_file_already_owned"
    assert event.data["issue_id"] == 7


def test_apply_matched_file_outcome_marks_duplicate_importable_target() -> None:
    issue = Issue(
        id=8,
        issue_number=20.0,
        comicvine_id=1020,
        title="Issue 20",
        status=IssueStatus.WANTED,
    )
    imp_file = ImportedFile(file_name="Absolute Batman 020.cbz")
    imp_series = ImportedSeries(raw_series_name="Absolute Batman")

    event = apply_matched_file_outcome(
        imp_file,
        imp_series,
        _candidate(issue, has_library_file=False),
        duplicate_series=True,
        duplicate_target_state=duplicate_target_state,
    )

    assert imp_file.status == ImportedFileStatus.MATCHED
    assert imp_file.include_in_import is False
    assert imp_file.diagnostics["target_state"] == "wanted"
    assert event.name == "import_duplicate_file_importable_match"
    assert event.data["target_state"] == "wanted"


def test_apply_unmatched_file_outcome_marks_new_series_no_match() -> None:
    imp_file = ImportedFile(file_name="Absolute Flash 001.cbz", parsed_issue_number=1.0)
    imp_series = ImportedSeries(raw_series_name="Absolute Flash")

    event = apply_unmatched_file_outcome(
        imp_file,
        imp_series,
        duplicate_series=False,
        duplicate_merge_profile=None,
        metadata_conflict=None,
    )

    assert imp_file.status == ImportedFileStatus.NO_MATCH
    assert imp_file.include_in_import is False
    assert imp_file.diagnostics == {
        "reason": "issue_target_not_found",
        "rejection_reason": (
            "No issue target matched the available file name and metadata evidence."
        ),
    }
    assert event.name == "import_file_no_match_detail"
    assert event.data["diagnostics"] is None


def test_apply_unmatched_file_outcome_preserves_source_metadata_diagnostics() -> None:
    imp_file = ImportedFile(
        file_name="Fearscape.Vol.02.A.Dark.Interlude.2023.pdf",
        parsed_issue_number=None,
        diagnostics={
            "source_issue_type": "volume",
            "source_metadata": {"filename_parse": {"volume": "Vol 02"}},
        },
    )
    imp_series = ImportedSeries(raw_series_name="Fearscape A Dark Interlude")

    apply_unmatched_file_outcome(
        imp_file,
        imp_series,
        duplicate_series=False,
        duplicate_merge_profile=None,
        metadata_conflict=None,
    )

    assert imp_file.diagnostics == {
        "source_issue_type": "volume",
        "source_metadata": {"filename_parse": {"volume": "Vol 02"}},
        "reason": "issue_target_not_found",
        "rejection_reason": (
            "No issue target matched the available file name and metadata evidence."
        ),
    }


def test_apply_unmatched_file_outcome_uses_metadata_conflict() -> None:
    imp_file = ImportedFile(file_name="Absolute Flash 001.cbz", parsed_issue_number=1.0)
    imp_series = ImportedSeries(raw_series_name="Absolute Flash")
    conflict = {"kind": "metadata_conflict", "rejection_reason": "Series mismatch"}

    event = apply_unmatched_file_outcome(
        imp_file,
        imp_series,
        duplicate_series=False,
        duplicate_merge_profile=None,
        metadata_conflict=conflict,
    )

    assert imp_file.status == ImportedFileStatus.NO_MATCH
    assert imp_file.diagnostics == {**conflict, "reason": "metadata_conflict"}
    assert event.name == "import_file_metadata_conflict"
    assert event.data["diagnostics"] == conflict


def test_apply_unmatched_file_outcome_marks_duplicate_informational_only() -> None:
    imp_file = ImportedFile(file_name="Absolute Batman 001.cbz", parsed_issue_number=1.0)
    imp_series = ImportedSeries(raw_series_name="Absolute Batman")
    profile = DuplicateMergeProfile(
        actionable=False,
        fully_owned=True,
        existing_issue_count=3,
        owned_issue_count=3,
    )

    event = apply_unmatched_file_outcome(
        imp_file,
        imp_series,
        duplicate_series=True,
        duplicate_merge_profile=profile,
        metadata_conflict=None,
    )

    assert imp_file.status == ImportedFileStatus.NO_MATCH
    assert imp_file.diagnostics == {
        "kind": "duplicate_series_file",
        "reason": "duplicate_series_no_importable_target",
        "target_state": "no_importable_targets",
        "actionable_duplicate_merge": False,
        "existing_issue_count": 3,
        "owned_issue_count": 3,
    }
    assert event.name == "import_duplicate_file_informational_only"


def test_apply_unmatched_file_outcome_applies_single_owned_shortcut() -> None:
    issue = Issue(
        id=9,
        issue_number=1.0,
        comicvine_id=1001,
        title="One Shot",
        status=IssueStatus.OWNED,
    )
    imp_file = ImportedFile(file_name="Absolute Special.cbz", parsed_issue_number=1.0)
    imp_series = ImportedSeries(raw_series_name="Absolute Special")
    profile = DuplicateMergeProfile(
        actionable=False,
        fully_owned=True,
        existing_issue_count=1,
        owned_issue_count=1,
        single_owned_shortcut_issue=issue,
    )

    event = apply_unmatched_file_outcome(
        imp_file,
        imp_series,
        duplicate_series=True,
        duplicate_merge_profile=profile,
        metadata_conflict=None,
    )

    assert imp_file.status == ImportedFileStatus.ALREADY_OWNED
    assert imp_file.include_in_import is False
    assert imp_file.matched_issue_id == 9
    assert imp_file.matched_issue_cv_id == 1001
    assert imp_file.match_confidence == "medium"
    assert imp_file.match_method == "single_owned_shortcut"
    assert imp_file.diagnostics["resolution_reason"] == "single_owned_non_standard_ended_series"
    assert event.name == "import_duplicate_file_single_issue_already_owned"


@pytest.mark.asyncio
async def test_apply_and_log_file_match_outcome_logs_matched_event() -> None:
    imp_file = ImportedFile(file_name="Absolute Wonder Woman 001.cbz", parsed_issue_number=1.0)
    imp_series = ImportedSeries(raw_series_name="Absolute Wonder Woman")
    candidate = _candidate(None, confidence="medium", method="issue_number")
    session = object()
    log_calls: list[dict[str, object]] = []

    async def log_event(*args: object, **kwargs: object) -> None:
        log_calls.append({"args": args, "kwargs": kwargs})

    event = await apply_and_log_file_match_outcome(
        session=session,
        job_id=42,
        imp_file=imp_file,
        imp_series=imp_series,
        match_candidate=candidate,
        duplicate_series=False,
        duplicate_target_state=duplicate_target_state,
        duplicate_merge_profile=None,
        metadata_conflict=None,
        log_event=log_event,
    )

    assert imp_file.status == ImportedFileStatus.MATCHED
    assert event.name == "import_file_match_detail"
    assert log_calls == [
        {
            "args": (session, 42, "DEBUG", "import_file_match_detail"),
            "kwargs": {
                "message": "File matched: Absolute Wonder Woman 001.cbz",
                "file_name": "Absolute Wonder Woman 001.cbz",
                "parsed_issue_number": 1.0,
                "method": "issue_number",
                "confidence": "medium",
                "series": "Absolute Wonder Woman",
            },
        }
    ]


@pytest.mark.asyncio
async def test_apply_and_log_file_match_outcome_logs_unmatched_event() -> None:
    imp_file = ImportedFile(file_name="Absolute Flash 001.cbz", parsed_issue_number=1.0)
    imp_series = ImportedSeries(raw_series_name="Absolute Flash")
    log_calls: list[dict[str, object]] = []

    async def log_event(*args: object, **kwargs: object) -> None:
        log_calls.append({"args": args, "kwargs": kwargs})

    event = await apply_and_log_file_match_outcome(
        session=object(),
        job_id=42,
        imp_file=imp_file,
        imp_series=imp_series,
        match_candidate=None,
        duplicate_series=False,
        duplicate_target_state=duplicate_target_state,
        duplicate_merge_profile=None,
        metadata_conflict={"kind": "metadata_conflict"},
        log_event=log_event,
    )

    assert imp_file.status == ImportedFileStatus.NO_MATCH
    assert event.name == "import_file_metadata_conflict"
    assert log_calls[0]["args"][1:] == (42, "DEBUG", "import_file_metadata_conflict")
    assert (
        log_calls[0]["kwargs"]["message"]
        == "Conflicting metadata blocked auto-match: Absolute Flash 001.cbz"
    )
