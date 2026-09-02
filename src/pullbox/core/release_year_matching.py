"""Date evidence for release matching, separate from provider query years."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from pullbox.models.issue import IssueType


@dataclass(frozen=True, slots=True)
class ReleaseYearContext:
    series_year: int | None = None
    publication_years: tuple[int, ...] = ()
    series_continuing: bool = False


@dataclass(frozen=True, slots=True)
class ReleaseYearEvidence:
    matches: bool | None
    basis: str
    weak: bool = False


def match_release_year(
    year: int | None,
    *,
    wanted_year: int | None,
    volume_year: int | None = None,
    context: ReleaseYearContext | None = None,
    issue_type: IssueType = IssueType.ISSUE,
    tolerance: int = 1,
) -> ReleaseYearEvidence:
    if context is not None:
        explicit_series_match = volume_year is not None and context.series_year is not None
        if explicit_series_match and volume_year != context.series_year:
            return ReleaseYearEvidence(False, "explicit_series_year_mismatch")
        if year is not None and context.publication_years:
            matches = any(
                abs(year - candidate) <= tolerance for candidate in context.publication_years
            )
            return ReleaseYearEvidence(
                matches, "publication_year" if matches else "publication_year_mismatch"
            )
        if year is None and explicit_series_match:
            return ReleaseYearEvidence(True, "explicit_series_year")
        if (
            year is not None
            and issue_type is IssueType.ISSUE
            and context.series_continuing
            and context.series_year is not None
        ):
            plausible = context.series_year <= year <= datetime.now(UTC).year
            return ReleaseYearEvidence(
                plausible, "series_window" if plausible else "outside_series_window", weak=True
            )
    if year is None or wanted_year is None:
        return ReleaseYearEvidence(None, "unknown")
    return ReleaseYearEvidence(abs(year - wanted_year) <= tolerance, "target_year")
