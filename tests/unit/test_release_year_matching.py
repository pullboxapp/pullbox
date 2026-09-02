"""Publication dates strengthen search matches without changing query identity."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from pullbox.models.issue import IssueType
from pullbox.models.library import MatchConfidence
from pullbox.services.release_validator import ReleaseValidator
from pullbox.services.search_targets import IssueSearchTarget
from tests.conftest import make_release


def _target(**changes):
    return replace(
        IssueSearchTarget(
            issue_id=1,
            series_id=1,
            series_title="2000 AD",
            issue_number=2487,
            issue_type=IssueType.ISSUE,
            series_year=1977,
            release_year=2026,
        ),
        **changes,
    )


def _validate(title, target):
    matched, rejected = ReleaseValidator().validate_all_results(
        [make_release(title)],
        wanted_series=target.series_title,
        wanted_issue=target.issue_number,
        wanted_issue_type=target.issue_type,
        wanted_year=target.search_year,
        year_context=target.year_context,
    )
    assert not rejected
    assert len(matched) == 1
    return matched[0]


@pytest.mark.parametrize("year", [2025, 2026, 2027])
def test_known_issue_date_matches_with_existing_tolerance(year):
    target = _target()
    result = _validate(f"2000AD #{target.issue_number} [{year}]", target)
    assert target.search_year == 1977
    assert result.confidence is MatchConfidence.HIGH
    assert result.year_match is True
    assert result.year_match_basis == "publication_year"


def test_store_and_cover_years_are_both_supported():
    target = _target(release_year=2027, store_year=2026)
    validator = ReleaseValidator(year_tolerance=0)
    matched, rejected = validator.validate_all_results(
        [make_release("2000AD #2487 [2026]"), make_release("2000AD #2487 [2027]")],
        wanted_series=target.series_title,
        wanted_issue=target.issue_number,
        wanted_year=target.search_year,
        year_context=target.year_context,
    )
    assert len(matched) == 2
    assert not rejected
    assert all(item.confidence is MatchConfidence.HIGH for item in matched)


@pytest.mark.parametrize("year", [1977, 2005, 2023, 2030])
def test_known_issue_date_does_not_accept_arbitrary_lifetime_year(year):
    target = _target(series_continuing=True)
    result = _validate(f"2000AD #2487 [{year}]", target)
    assert result.confidence is MatchConfidence.LOW
    assert result.year_match is False
    assert result.year_match_basis == "publication_year_mismatch"


@pytest.mark.parametrize("year", [1977, 2005, datetime.now(UTC).year])
def test_undated_continuing_issue_uses_medium_confidence_window(year):
    target = _target(release_year=None, series_continuing=True)
    result = _validate(f"2000AD #2487 [{year}]", target)
    assert result.confidence is MatchConfidence.MEDIUM
    assert result.year_match is True
    assert result.year_match_basis == "series_window"


@pytest.mark.parametrize("year", [1976, datetime.now(UTC).year + 1])
def test_undated_continuing_issue_outside_window_is_not_promoted(year):
    result = _validate(f"2000AD #2487 [{year}]", _target(release_year=None, series_continuing=True))
    assert result.confidence is MatchConfidence.LOW
    assert result.year_match is False


def test_ended_or_unknown_series_does_not_get_open_ended_window():
    result = _validate("2000AD #2487 [2026]", _target(release_year=None))
    assert result.confidence is MatchConfidence.LOW


def test_explicit_series_year_does_not_need_to_be_issue_publication_year():
    result = _validate("2000AD v1977 #2487 [Digital-Empire]", _target())
    assert result.confidence is MatchConfidence.HIGH
    assert result.year_match_basis == "explicit_series_year"


def test_wrong_explicit_volume_cannot_be_rescued_by_matching_publication_year():
    result = _validate("2000AD v2011 #2487 [2026]", _target())
    assert result.confidence is MatchConfidence.LOW
    assert result.year_match_basis == "explicit_series_year_mismatch"


@pytest.mark.parametrize("issue_type", [IssueType.TPB, IssueType.ANNUAL, IssueType.ONE_SHOT])
def test_series_window_is_only_for_regular_serial_issues(issue_type):
    target = _target(release_year=None, series_continuing=True, issue_type=issue_type)
    suffix = {IssueType.TPB: "TPB", IssueType.ANNUAL: "Annual", IssueType.ONE_SHOT: "One Shot"}[
        issue_type
    ]
    result = _validate(f"2000AD {suffix} #2487 [2026]", target)
    assert result.confidence is MatchConfidence.LOW


def test_correct_year_does_not_bypass_identity_checks():
    target = _target()
    matched, rejected = ReleaseValidator().validate_all_results(
        [
            make_release("2000AD #2488 [2026]"),
            make_release("Another Series #2487 [2026]"),
            make_release("2000AD Annual #2487 [2026]"),
        ],
        wanted_series=target.series_title,
        wanted_issue=target.issue_number,
        wanted_year=target.search_year,
        year_context=target.year_context,
    )
    assert not matched
    assert len(rejected) == 3
