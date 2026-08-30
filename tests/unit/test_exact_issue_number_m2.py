"""M2 contracts for exact issue identities that share one numeric sort value."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.api.v1.series import list_series_issues
from pullbox.core.events import EventBus
from pullbox.models import Base
from pullbox.models.import_job import ImportedFile
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.models.user import User
from pullbox.providers.base import IssueSummary
from pullbox.services.airdcpp_search_coordinator import _query_pattern
from pullbox.services.db_check_service import register_stale_library_file
from pullbox.services.direct_search_coordinator import _build_intent
from pullbox.services.import_file_execution import _ensure_placeholder_issue_targets
from pullbox.services.import_file_resolution import (
    load_issue_lookup_for_series,
    resolve_import_file_issue,
)
from pullbox.services.matching_service import MatchingService, _find_issue
from pullbox.services.metadata_service import MetadataService
from pullbox.services.search_query_helpers import (
    build_auto_fallback_queries,
    build_issue_queries,
)
from pullbox.services.search_targets import load_issue_search_target

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Create the current ORM schema in an isolated SQLite database."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _series() -> Series:
    return Series(
        title="Suffix Siblings",
        sort_title="suffix siblings",
        comicvine_id=170_100,
        status=SeriesStatus.CONTINUING,
        series_type=SeriesType.STANDARD,
        monitored=True,
        issue_count=2,
    )


def test_issue_model_uses_exact_text_as_its_only_per_series_identity() -> None:
    """The numeric column remains a compatibility sort key, not a unique key."""
    unique_constraints = {
        constraint.name
        for constraint in Issue.__table__.constraints
        if getattr(constraint, "unique", False)
    }
    indexes = {index.name: index for index in Issue.__table__.indexes}

    assert "uq_series_issue" not in unique_constraints
    assert indexes["uq_series_issue_number_text"].unique is True
    assert [column.name for column in indexes["uq_series_issue_number_text"].columns] == [
        "series_id",
        "issue_number_text",
    ]


@pytest.mark.asyncio
async def test_suffix_siblings_coexist_and_search_by_exact_identity(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Equal numeric sort keys must not merge exact issue identities or queries."""
    async with db_factory() as session:
        series = _series()
        session.add(series)
        await session.flush()
        after_universe = Issue(
            series_id=series.id,
            comicvine_id=1_701_001,
            issue_number=1.0,
            issue_number_text="1AU",
            status=IssueStatus.WANTED,
            issue_type=IssueType.ISSUE,
        )
        variant_b = Issue(
            series_id=series.id,
            comicvine_id=1_701_002,
            issue_number=1.0,
            issue_number_text="1B",
            status=IssueStatus.WANTED,
            issue_type=IssueType.ISSUE,
        )
        session.add_all([after_universe, variant_b])
        await session.commit()

        response = await list_series_issues(
            series.id,
            User(username="apiuser", password_hash="unused"),
            session,
            limit=100,
            offset=0,
        )
        first_target = await load_issue_search_target(session, after_universe.id)
        second_target = await load_issue_search_target(session, variant_b.id)

    assert [item.issue_number_text for item in response.items] == ["1AU", "1B"]
    assert first_target is not None
    assert second_target is not None
    first_queries = [query.series_title for query in build_issue_queries(first_target, mode="fast")]
    second_queries = [
        query.series_title for query in build_issue_queries(second_target, mode="fast")
    ]
    assert first_queries == ["Suffix Siblings 1AU"]
    assert second_queries == ["Suffix Siblings 1B"]
    assert [query.series_title for query in build_auto_fallback_queries(first_target)] == [
        "Suffix Siblings 1AU"
    ]
    assert [query.series_title for query in build_auto_fallback_queries(second_target)] == [
        "Suffix Siblings 1B"
    ]
    assert _build_intent(first_target).issue_number == "1AU"
    assert _build_intent(second_target).issue_number == "1B"
    assert _query_pattern(first_target) == "Suffix Siblings 1AU"
    assert _query_pattern(second_target) == "Suffix Siblings 1B"


@pytest.mark.asyncio
async def test_provider_refresh_creates_and_updates_suffix_siblings_independently(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Exact provider identities outrank the shared numeric compatibility value."""
    service = MetadataService(provider=MagicMock(), covers_dir=MagicMock())
    summaries = [
        IssueSummary(
            provider_id="1701001",
            issue_number=1.0,
            issue_number_text="1au",
            title="After Universe",
            release_date=None,
            cover_url=None,
            issue_type="issue",
        ),
        IssueSummary(
            provider_id="1701002",
            issue_number=1.0,
            issue_number_text="1b",
            title="Variant B",
            release_date=None,
            cover_url=None,
            issue_type="issue",
        ),
    ]

    async with db_factory() as session:
        series = _series()
        session.add(series)
        await session.flush()

        created = await service.upsert_issue_summaries(session, series, summaries)
        await session.flush()
        refreshed_summaries = [
            replace(summaries[0], title="After Universe Updated"),
            summaries[1],
        ]
        recreated = await service.upsert_issue_summaries(
            session,
            series,
            refreshed_summaries,
        )
        await session.flush()
        issues = list(
            (
                await session.execute(
                    select(Issue)
                    .where(Issue.series_id == series.id)
                    .order_by(Issue.issue_number, Issue.issue_number_text, Issue.id)
                )
            )
            .scalars()
            .all()
        )

    assert len(created) == 2
    assert recreated == []
    assert [(issue.issue_number_text, issue.title) for issue in issues] == [
        ("1AU", "After Universe Updated"),
        ("1B", "Variant B"),
    ]


@pytest.mark.asyncio
async def test_placeholder_creation_and_import_resolution_preserve_suffix_siblings(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Placeholder creation and later resolution must use the exact designation."""
    async with db_factory() as session:
        series = _series()
        session.add(series)
        await session.flush()
        after_universe = Issue(
            series_id=series.id,
            issue_number=1.0,
            issue_number_text="1AU",
            status=IssueStatus.SKIPPED,
            issue_type=IssueType.ISSUE,
        )
        session.add(after_universe)
        await session.flush()
        variant_file = ImportedFile(
            file_path="/source/Suffix Siblings 1B.cbz",
            file_name="Suffix Siblings 1B.cbz",
            file_size=1,
            file_format="cbz",
            parsed_issue_number=1.0,
            issue_number_raw="1B",
            diagnostics={
                "kind": "provider_missing_issue_placeholder",
                "target_issue_number": 1.0,
                "target_issue_type": IssueType.ISSUE.value,
                "target_issue_title": "Variant B",
            },
        )

        changed = await _ensure_placeholder_issue_targets(
            session,
            series_id=series.id,
            importable_files=[variant_file],
        )
        await session.flush()
        issues = list(
            (
                await session.execute(
                    select(Issue)
                    .where(Issue.series_id == series.id)
                    .order_by(Issue.issue_number_text)
                )
            )
            .scalars()
            .all()
        )
        cv_lookup, exact_lookup, numeric_lookup = await load_issue_lookup_for_series(
            session,
            series.id,
        )
        resolved = await resolve_import_file_issue(
            session,
            variant_file,
            cv_id_to_issue=cv_lookup,
            exact_number_to_issue=exact_lookup,
            number_to_issue=numeric_lookup,
        )

    assert changed is True
    assert [issue.issue_number_text for issue in issues] == ["1AU", "1B"]
    assert resolved is issues[1]
    assert numeric_lookup == {}


@pytest.mark.asyncio
async def test_matching_exact_suffix_first_and_ambiguous_numeric_fallback_fails_closed(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Filename matching chooses 1B exactly and never guesses between siblings."""
    async with db_factory() as session:
        series = _series()
        root = LibraryRoot(name="Suffix library", path=str(tmp_path), enabled=True)
        session.add_all([series, root])
        await session.flush()
        after_universe = Issue(
            series_id=series.id,
            issue_number=1.0,
            issue_number_text="1AU",
            status=IssueStatus.WANTED,
            issue_type=IssueType.ISSUE,
        )
        variant_b = Issue(
            series_id=series.id,
            issue_number=1.0,
            issue_number_text="1B",
            status=IssueStatus.WANTED,
            issue_type=IssueType.ISSUE,
        )
        session.add_all([after_universe, variant_b])
        await session.flush()
        source = tmp_path / "Suffix Siblings #1B.cbz"
        source.write_bytes(b"")
        library_file = LibraryFile(
            file_path=str(source),
            file_name=source.name,
            file_size=0,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.UNMATCHED,
            library_root_id=root.id,
        )
        session.add(library_file)
        await session.flush()

        confidence = await MatchingService(EventBus()).match_file(session, library_file)
        ambiguous = await _find_issue(
            session,
            series.id,
            1.0,
            issue_number_text=None,
        )

    assert confidence in {MatchConfidence.HIGH, MatchConfidence.MEDIUM}
    assert library_file.issue_id == variant_b.id
    assert ambiguous is None


@pytest.mark.asyncio
async def test_db_check_registration_uses_exact_suffix_identity(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Stale-file recovery must not raise or collapse equal numeric siblings."""
    source_root = tmp_path / "library"
    series_folder = source_root / "Suffix Siblings"
    series_folder.mkdir(parents=True)
    source = series_folder / "Suffix Siblings #1B.cbz"
    source.write_bytes(b"comic")

    async with db_factory() as session:
        root = LibraryRoot(name="Suffix library", path=str(source_root), enabled=True)
        session.add(root)
        await session.flush()
        series = _series()
        series.path = str(series_folder)
        series.library_root_id = root.id
        session.add(series)
        await session.flush()
        after_universe = Issue(
            series_id=series.id,
            issue_number=1.0,
            issue_number_text="1AU",
            status=IssueStatus.OWNED,
            issue_type=IssueType.ISSUE,
        )
        variant_b = Issue(
            series_id=series.id,
            issue_number=1.0,
            issue_number_text="1B",
            status=IssueStatus.WANTED,
            issue_type=IssueType.ISSUE,
        )
        session.add_all([after_universe, variant_b])
        await session.flush()

        finding = await register_stale_library_file(session, file_path_str=str(source))
        registered = (
            await session.execute(select(LibraryFile).where(LibraryFile.file_path == str(source)))
        ).scalar_one()

    assert finding is None
    assert registered.issue_id == variant_b.id
    assert registered.match_confidence == MatchConfidence.HIGH
