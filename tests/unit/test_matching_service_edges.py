"""Failure and fallback coverage for local library matching."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pullbox.core.archive import ArchiveError
from pullbox.core.events import EventBus
from pullbox.core.exceptions import NotFoundError
from pullbox.core.release_parser import parse_release_title
from pullbox.core.source_metadata import SourceMetadata
from pullbox.models.issue import IssueType
from pullbox.models.library import LibraryFile, MatchConfidence
from pullbox.services.matching_service import MatchingService, _find_issue, _get_candidate_series


def _library_file(path: str, *, name: str = "Unknown.cbz") -> LibraryFile:
    return LibraryFile(
        id=7,
        file_path=path,
        file_name=name,
        file_size=1,
        file_format="cbz",
        match_confidence=MatchConfidence.UNMATCHED,
        library_root_id=1,
    )


@pytest.mark.asyncio
async def test_match_file_reports_unmatched_after_both_strategies_fail() -> None:
    service = MatchingService(EventBus())
    service._match_from_comicinfo = AsyncMock(return_value=MatchConfidence.UNMATCHED)  # type: ignore[method-assign]
    service._match_from_filename = AsyncMock(return_value=MatchConfidence.UNMATCHED)  # type: ignore[method-assign]

    result = await service.match_file(AsyncMock(), _library_file("/missing.cbz"))

    assert result is MatchConfidence.UNMATCHED


@pytest.mark.asyncio
async def test_manual_match_and_unmatch_reject_missing_rows() -> None:
    session = AsyncMock()
    session.get.return_value = None
    service = MatchingService(EventBus())

    with pytest.raises(NotFoundError, match="LibraryFile"):
        await service.manual_match(session, 1, 2)
    with pytest.raises(NotFoundError, match="LibraryFile"):
        await service.unmatch_file(session, 1)

    library_file = _library_file("/missing.cbz")
    session.get.side_effect = [library_file, None]
    with pytest.raises(NotFoundError, match="Issue"):
        await service.manual_match(session, 1, 2)


@pytest.mark.asyncio
async def test_comicinfo_matching_handles_missing_unreadable_and_sparse_sources(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    service = MatchingService(EventBus())
    session = AsyncMock()

    assert (
        await service._match_from_comicinfo(session, _library_file(str(tmp_path / "missing.cbz")))
        is MatchConfidence.UNMATCHED
    )

    source = tmp_path / "source.cbz"
    source.touch()
    service._extractor.from_archive_path = MagicMock(side_effect=ArchiveError("broken"))
    assert (
        await service._match_from_comicinfo(session, _library_file(str(source)))
        is MatchConfidence.UNMATCHED
    )

    service._extractor.from_archive_path = MagicMock(
        return_value=SourceMetadata(original_title=source.name)
    )
    assert (
        await service._match_from_comicinfo(session, _library_file(str(source)))
        is MatchConfidence.UNMATCHED
    )

    service._extractor.from_archive_path = MagicMock(
        return_value=SourceMetadata(
            original_title=source.name,
            diagnostics={"has_comicinfo": True},
        )
    )
    assert (
        await service._match_from_comicinfo(session, _library_file(str(source)))
        is MatchConfidence.UNMATCHED
    )


@pytest.mark.asyncio
async def test_filename_matching_handles_unparsed_and_nonstandard_releases() -> None:
    service = MatchingService(EventBus())
    service._extractor.from_release_title = MagicMock(
        return_value=SourceMetadata(original_title="unknown")
    )
    assert (
        await service._match_from_filename(AsyncMock(), _library_file("/source.cbz"))
        is MatchConfidence.UNMATCHED
    )

    parsed = parse_release_title("Batman Omnibus (2025).cbz")
    assert parsed is not None
    service._extractor.from_release_title = MagicMock(
        return_value=SourceMetadata(
            original_title="Batman Omnibus (2025).cbz",
            series_name="Batman",
            issue_type=IssueType.OMNIBUS,
            parsed_release=parsed,
        )
    )
    service._create_suggestion_if_variant = AsyncMock()  # type: ignore[method-assign]

    result = await service._match_from_filename(AsyncMock(), _library_file("/source.cbz"))

    assert result is MatchConfidence.UNMATCHED
    service._create_suggestion_if_variant.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_variant_suggestion_stops_for_standard_same_base_and_missing_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MatchingService(EventBus())
    session = AsyncMock()
    library_file = _library_file("/source.cbz")
    standard = parse_release_title("Batman 001.cbz")
    same_base = parse_release_title("Batman Omnibus.cbz")
    no_parent = parse_release_title("Batman Annual 001.cbz")
    assert standard is not None and same_base is not None and no_parent is not None

    await service._create_suggestion_if_variant(session, library_file, standard)
    await service._create_suggestion_if_variant(session, library_file, same_base)
    monkeypatch.setattr(
        "pullbox.services.matching_service._get_candidate_series",
        AsyncMock(return_value=[]),
    )
    await service._create_suggestion_if_variant(session, library_file, no_parent)

    session.add.assert_not_called()


def test_apply_metadata_only_overwrites_present_signals() -> None:
    library_file = _library_file("/source.cbz")

    MatchingService._apply_metadata(
        library_file,
        SourceMetadata(
            original_title="Batman 001.cbz",
            series_name="Batman",
            publisher="DC Comics",
            year=2025,
            issue_number=1,
        ),
    )

    assert library_file.parsed_series == "Batman"
    assert library_file.parsed_publisher == "DC Comics"
    assert library_file.parsed_year == 2025
    assert library_file.parsed_issue_number == 1


@pytest.mark.asyncio
async def test_metadata_candidate_matching_handles_missing_series_and_candidate_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MatchingService(EventBus())
    session = AsyncMock()
    library_file = _library_file("/source.cbz")

    assert (
        await service._match_metadata_to_candidates(
            session,
            library_file,
            SourceMetadata(original_title="unknown"),
            allow_series_only_medium=False,
        )
        is MatchConfidence.UNMATCHED
    )

    candidate = SimpleNamespace(id=4, title="Superman", alternate_names=[])
    monkeypatch.setattr(
        "pullbox.services.matching_service._get_candidate_series",
        AsyncMock(return_value=[candidate]),
    )
    service._name_matcher = MagicMock(
        match=MagicMock(return_value=SimpleNamespace(is_match=False, similarity=0.0))
    )
    assert (
        await service._match_metadata_to_candidates(
            session,
            library_file,
            SourceMetadata(original_title="Batman 001", series_name="Batman", issue_number=1),
            allow_series_only_medium=False,
        )
        is MatchConfidence.UNMATCHED
    )


@pytest.mark.asyncio
async def test_candidate_loader_falls_back_to_all_series() -> None:
    first_result = MagicMock()
    first_result.scalars.return_value.all.return_value = []
    fallback = [SimpleNamespace(title="Batman")]
    second_result = MagicMock()
    second_result.scalars.return_value.all.return_value = fallback
    session = AsyncMock()
    session.execute.side_effect = [first_result, second_result]

    assert await _get_candidate_series(session, "Batman") == fallback
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_exact_issue_lookup_fails_closed_on_bad_or_conflicting_text() -> None:
    session = AsyncMock()

    assert await _find_issue(session, 1, 1, issue_number_text="invalid") is None
    assert await _find_issue(session, 1, 1, issue_number_text="2") is None
    session.execute.assert_not_awaited()
