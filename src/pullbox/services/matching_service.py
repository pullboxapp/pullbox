"""Matching service — links library files to series/issues.

Provides automated matching via ComicInfo.xml and filename parsing,
with confidence levels based on match quality.  High-confidence matches
are auto-accepted; medium and low are placed in a review queue.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from pullbox.core.archive import ArchiveError
from pullbox.core.events import EventBus, FileMatched
from pullbox.core.issue_numbers import (
    issue_number_text_matches_numeric,
    normalize_issue_number_text,
)
from pullbox.core.name_matcher import NameMatcher
from pullbox.core.naming import (
    detect_series_type,
    extract_base_series_title,
)
from pullbox.core.source_metadata import SourceMetadata, SourceMetadataExtractor
from pullbox.models.issue import Issue, IssueType
from pullbox.models.library import LibraryFile, MatchConfidence
from pullbox.models.matching_suggestion import MatchingSuggestion, SuggestionStatus
from pullbox.models.series import Series
from pullbox.services.semantic_matching import LibraryMatchPolicy, SemanticMatchEngine

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.release_parser import ParsedRelease

logger = structlog.get_logger(__name__)

_COMICVINE_URL_RE = re.compile(r"comicvine\.gamespot\.com/.*?/4\d+-(\d+)", re.IGNORECASE)


# ── Service ───────────────────────────────────────────────────────────


class MatchingService:
    """Matches library files to known series and issues.

    Args:
        event_bus: For emitting match-related events.
    """

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._name_matcher = NameMatcher()
        self._extractor = SourceMetadataExtractor()
        self._semantic_engine = SemanticMatchEngine(policy=LibraryMatchPolicy())

    async def match_file(
        self,
        session: AsyncSession,
        library_file: LibraryFile,
    ) -> MatchConfidence:
        """Attempt automated matching of a library file to an issue.

        Strategy (highest confidence first):
        1. ComicInfo.xml with ComicVine ID -> HIGH
        2. ComicInfo.xml series+issue+year -> HIGH/MEDIUM
        3. Filename parsing with exact match -> HIGH/MEDIUM
        4. Fuzzy series name matching -> LOW
        """
        log = logger.bind(file_id=library_file.id, file_name=library_file.file_name)
        log.info("matching_attempt")

        # Strategy 1 & 2: ComicInfo.xml
        confidence = await self._match_from_comicinfo(session, library_file)
        if confidence != MatchConfidence.UNMATCHED:
            log.info("matching_comicinfo_success", confidence=confidence)
            return confidence

        # Strategy 3 & 4: Filename parsing
        confidence = await self._match_from_filename(session, library_file)
        if confidence != MatchConfidence.UNMATCHED:
            log.info("matching_filename_success", confidence=confidence)
            return confidence

        log.info("matching_no_match")
        return MatchConfidence.UNMATCHED

    async def manual_match(
        self,
        session: AsyncSession,
        library_file_id: int,
        issue_id: int,
    ) -> LibraryFile:
        """Manually match a library file to a specific issue."""
        log = logger.bind(file_id=library_file_id, issue_id=issue_id)

        library_file = await session.get(LibraryFile, library_file_id)
        if not library_file:
            from pullbox.core.exceptions import NotFoundError

            raise NotFoundError("LibraryFile", library_file_id)

        issue = await session.get(Issue, issue_id)
        if not issue:
            from pullbox.core.exceptions import NotFoundError

            raise NotFoundError("Issue", issue_id)

        library_file.issue_id = issue_id
        library_file.match_confidence = MatchConfidence.MANUAL

        await self._event_bus.emit(
            FileMatched(
                library_file_id=library_file_id,
                issue_id=issue_id,
                confidence=MatchConfidence.MANUAL,
            )
        )

        log.info("matching_manual", confidence=MatchConfidence.MANUAL)
        # noinspection PyTypeChecker
        return library_file

    @staticmethod
    async def unmatch_file(
        session: AsyncSession,
        library_file_id: int,
    ) -> LibraryFile:
        """Remove the match from a library file."""
        library_file = await session.get(LibraryFile, library_file_id)
        if not library_file:
            from pullbox.core.exceptions import NotFoundError

            raise NotFoundError("LibraryFile", library_file_id)

        library_file.issue_id = None
        library_file.match_confidence = MatchConfidence.UNMATCHED

        logger.info("matching_removed", file_id=library_file_id)
        # noinspection PyTypeChecker
        return library_file

    async def _match_from_comicinfo(
        self,
        session: AsyncSession,
        library_file: LibraryFile,
    ) -> MatchConfidence:
        """Try to match using embedded ComicInfo.xml metadata."""
        path = Path(library_file.file_path)
        if not path.exists():
            return MatchConfidence.UNMATCHED

        try:
            metadata = self._extractor.from_archive_path(path)
        except (ArchiveError, OSError):
            return MatchConfidence.UNMATCHED

        if not metadata.diagnostics.get("has_comicinfo"):
            return MatchConfidence.UNMATCHED

        library_file.has_comicinfo = True
        self._apply_metadata(library_file, metadata)

        # Check for ComicVine ID in the web field
        comicvine_id = metadata.comicvine_issue_id
        if comicvine_id:
            issue = await _find_issue_by_comicvine_id(session, comicvine_id)
            if issue:
                return await self._accept_match(library_file, issue, MatchConfidence.HIGH)

        if not metadata.series_name:
            return MatchConfidence.UNMATCHED
        return await self._match_metadata_to_candidates(
            session,
            library_file,
            metadata,
            allow_series_only_medium=True,
        )

    async def _match_from_filename(
        self,
        session: AsyncSession,
        library_file: LibraryFile,
    ) -> MatchConfidence:
        """Try to match using parsed filename data."""
        metadata = self._extractor.from_release_title(library_file.file_name)
        parsed = metadata.parsed_release
        if parsed is None or metadata.series_name is None:
            return MatchConfidence.UNMATCHED

        self._apply_metadata(library_file, metadata)

        # IssueType routing: non-standard types get variant handling
        if metadata.issue_type not in (IssueType.ISSUE, IssueType.ANNUAL):
            logger.info(
                "matching_non_issue_type",
                file_id=library_file.id,
                issue_type=metadata.issue_type.value,
                series=metadata.series_name,
            )
            await self._create_suggestion_if_variant(
                session,
                library_file,
                parsed,
            )
            return MatchConfidence.UNMATCHED

        confidence = await self._match_metadata_to_candidates(
            session,
            library_file,
            metadata,
            allow_series_only_medium=False,
        )
        if confidence != MatchConfidence.UNMATCHED:
            return confidence

        # No match found — check if this is a type variant and suggest
        await self._create_suggestion_if_variant(
            session,
            library_file,
            parsed,
        )

        return MatchConfidence.UNMATCHED

    async def _create_suggestion_if_variant(
        self,
        session: AsyncSession,
        library_file: LibraryFile,
        parsed: ParsedRelease,
    ) -> None:
        """If the file looks like a variant type, suggest the related series.

        Detects whether the filename indicates an annual, TPB, etc. then
        checks if a matching base (standard) series exists in the DB.  If
        so, creates a :class:`MatchingSuggestion` for one-click resolution.
        """
        parsed_title = parsed.series_name or ""
        parsed_year = parsed.year

        # Use the ParsedRelease issue_type if available; fall back to title detection
        detected_type = parsed.issue_type.value
        if detected_type == "issue":
            title_type = detect_series_type(parsed_title)
            if title_type == "standard":
                return  # Not a variant, nothing to suggest
            detected_type = title_type

        if detected_type in ("issue", "standard"):
            return  # Not a variant, nothing to suggest

        # Extract the base series name
        base_title = extract_base_series_title(parsed_title)
        if base_title == parsed_title:
            return  # Couldn't extract a different base title

        # Check if a parent series exists locally
        candidates = await _get_candidate_series(session, base_title)
        parent: Series | None = None
        for series in candidates:
            match_result = self._name_matcher.match(base_title, str(series.title))
            if match_result.is_match and match_result.similarity >= 0.80:
                parent = series
                break

        if not parent:
            return  # No good parent candidate

        # Don't duplicate existing pending suggestions for the same file
        existing = await session.execute(
            select(MatchingSuggestion).where(
                MatchingSuggestion.library_file_id == library_file.id,
                MatchingSuggestion.status == SuggestionStatus.PENDING,
            )
        )
        if existing.scalar_one_or_none():
            return

        suggestion = MatchingSuggestion(
            library_file_id=library_file.id,
            parent_series_id=parent.id,
            suggested_title=parsed_title,
            suggested_year=parsed_year or parent.year_start,
            suggested_publisher=(
                parent.publisher.name if hasattr(parent, "publisher") and parent.publisher else None
            ),
            suggested_series_type=detected_type,
            detection_source="filename",
            confidence_score=0.8,
            reason=(
                f"File appears to be a {detected_type} of '{parent.title}' "
                f"but no matching series exists."
            ),
            status=SuggestionStatus.PENDING,
        )
        session.add(suggestion)
        logger.info(
            "matching_suggestion_created",
            file_id=library_file.id,
            parent_series_id=parent.id,
            suggested_type=detected_type,
            suggested_title=parsed_title,
        )

    async def _accept_match(
        self,
        library_file: LibraryFile,
        issue: Issue,
        confidence: MatchConfidence,
    ) -> MatchConfidence:
        """Record a match and emit the event."""
        library_file.issue_id = issue.id
        library_file.match_confidence = confidence

        await self._event_bus.emit(
            FileMatched(
                library_file_id=library_file.id,
                issue_id=issue.id,
                confidence=confidence,
            )
        )
        return confidence

    @staticmethod
    def _apply_metadata(library_file: LibraryFile, metadata: SourceMetadata) -> None:
        """Persist parsed metadata onto the library file row."""
        if metadata.series_name:
            library_file.parsed_series = metadata.series_name
        if metadata.publisher:
            library_file.parsed_publisher = metadata.publisher
        if metadata.year:
            library_file.parsed_year = metadata.year
        if metadata.issue_number is not None:
            library_file.parsed_issue_number = metadata.issue_number

    async def _match_metadata_to_candidates(
        self,
        session: AsyncSession,
        library_file: LibraryFile,
        metadata: SourceMetadata,
        *,
        allow_series_only_medium: bool,
    ) -> MatchConfidence:
        """Use the shared semantic engine to match parsed metadata against issues."""
        if not metadata.series_name:
            return MatchConfidence.UNMATCHED

        candidates = await _get_candidate_series(session, metadata.series_name)
        logger.debug(
            "matching_semantic_candidates",
            search_term=metadata.series_name,
            candidate_count=len(candidates),
            parsed_issue=metadata.issue_number,
            parsed_year=metadata.year,
            allow_series_only_medium=allow_series_only_medium,
        )

        matched_series_only = False
        for series in candidates:
            alternates = list(series.alternate_names) if series.alternate_names else None
            name_match = self._name_matcher.match(
                metadata.series_name,
                str(series.title),
                alternate_names=alternates,
            )
            if not name_match.is_match:
                continue

            if metadata.issue_number is None:
                matched_series_only = True
                continue

            issue = await _find_issue(
                session,
                series.id,
                metadata.issue_number,
                issue_number_text=metadata.issue_number_text,
            )
            if issue is None:
                logger.debug(
                    "matching_semantic_series_hit_no_issue",
                    series=series.title,
                    issue_number=metadata.issue_number,
                )
                continue

            decision = self._semantic_engine.match_against_issue(
                metadata=metadata,
                wanted_series=str(series.title),
                wanted_issue=issue.issue_number,
                wanted_year=series.year_start,
                wanted_issue_type=issue.issue_type,
                alternate_names=alternates,
                wanted_issue_cv_id=issue.comicvine_id,
                wanted_issue_title=issue.title,
            )
            if not decision.is_match:
                continue

            logger.debug(
                "matching_semantic_accepted",
                series=series.title,
                issue_number=issue.issue_number,
                match_method=decision.match_method,
                confidence=str(decision.confidence),
            )
            return await self._accept_match(library_file, issue, decision.confidence)

        if allow_series_only_medium and matched_series_only:
            library_file.match_confidence = MatchConfidence.MEDIUM
            return MatchConfidence.MEDIUM

        return MatchConfidence.UNMATCHED


# ── Helpers ───────────────────────────────────────────────────────────


def _extract_comicvine_id(web: str | None) -> int | None:
    """Extract a ComicVine issue ID from a URL string."""
    if not web:
        return None
    match = _COMICVINE_URL_RE.search(web)
    if match:
        with contextlib.suppress(ValueError):
            return int(match.group(1))
    return None


def _year_close(a: int | None, b: int | None) -> bool:
    """Return True if two years are within ±1 of each other."""
    if a is None or b is None:
        return False
    return abs(a - b) <= 1


async def _find_issue_by_comicvine_id(
    session: AsyncSession,
    comicvine_id: int,
) -> Issue | None:
    """Find an issue by its ComicVine ID."""
    result = await session.execute(select(Issue).where(Issue.comicvine_id == comicvine_id))
    return result.scalar_one_or_none()


async def _get_candidate_series(
    session: AsyncSession,
    title: str,
) -> list[Series]:
    """Load candidate series from the database for name matching.

    Uses a broad ILIKE filter to narrow the DB query, then returns
    all candidates for the caller to evaluate via NameMatcher.
    """
    # Use first word of the normalized title for a broad DB filter
    first_word = title.split()[0] if title.strip() else title
    result = await session.execute(select(Series).where(Series.title.ilike(f"%{first_word}%")))
    candidates = list(result.scalars().all())

    # If no candidates from the broad filter, fall back to all series
    if not candidates:
        result = await session.execute(select(Series))
        candidates = list(result.scalars().all())

    return candidates


def _confidence_from_match(
    match_type: str,
    similarity: float,
    parsed_year: int | None,
    series_year: int | None,
) -> MatchConfidence:
    """Compute match confidence from NameMatcher result and year data."""
    year_close = _year_close(parsed_year, series_year) if parsed_year and series_year else False

    if match_type in ("exact", "alternate"):
        confidence = MatchConfidence.HIGH if year_close else MatchConfidence.MEDIUM
    elif match_type == "token_set":
        confidence = MatchConfidence.MEDIUM
    elif similarity >= 0.85:
        confidence = MatchConfidence.MEDIUM if year_close else MatchConfidence.LOW
    else:
        confidence = MatchConfidence.LOW

    logger.debug(
        "matching_confidence_computed",
        match_type=match_type,
        similarity=round(similarity, 3),
        year_close=year_close,
        confidence=str(confidence),
    )
    return confidence


async def _find_issue(
    session: AsyncSession,
    series_id: int,
    issue_number: float,
    *,
    issue_number_text: str | None = None,
) -> Issue | None:
    """Find one exact issue, failing closed on ambiguous legacy numeric identity."""
    if issue_number_text is not None:
        try:
            normalized_text = normalize_issue_number_text(issue_number_text)
        except ValueError:
            return None
        if not issue_number_text_matches_numeric(issue_number, normalized_text):
            return None
        result = await session.execute(
            select(Issue).where(
                Issue.series_id == series_id,
                Issue.issue_number_text == normalized_text,
            )
        )
        return result.scalar_one_or_none()

    result = await session.execute(
        select(Issue)
        .where(
            Issue.series_id == series_id,
            Issue.issue_number == issue_number,
        )
        .limit(2)
    )
    candidates = list(result.scalars().all())
    return candidates[0] if len(candidates) == 1 else None
