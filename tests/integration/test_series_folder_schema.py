"""Tests for Phase 2 schema changes — series path/library_root_id and issue_type.

Uses an in-memory SQLite database to verify model relationships and column
behavior without touching the real database or Alembic migrations.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import LibraryRoot
from pullbox.models.series import Series, SeriesStatus

# ── Series.path and Series.library_root ────────────────────────────


class TestSeriesPath:
    """Series model path and library_root_id columns."""

    async def test_series_path_nullable(self, db_session) -> None:
        """Series can be created without a path (legacy behavior)."""
        series = Series(
            title="Batman",
            sort_title="Batman",
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        assert series.id is not None
        assert series.path is None
        assert series.library_root_id is None

    async def test_series_with_path(self, db_session) -> None:
        """Series can store an absolute path to its folder."""
        series = Series(
            title="Invincible",
            sort_title="Invincible",
            path="/comics/Invincible (2003)",
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        assert series.path == "/comics/Invincible (2003)"

    async def test_series_library_root_relationship(self, db_session) -> None:
        """Series.library_root links to the LibraryRoot it belongs to."""
        root = LibraryRoot(name="Comics", path="/comics", enabled=True)
        db_session.add(root)
        await db_session.flush()

        series = Series(
            title="Saga",
            sort_title="Saga",
            path="/comics/Saga (2012)",
            library_root_id=root.id,
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        assert series.library_root_id == root.id
        assert series.library_root is root

    async def test_series_preferred_library_root_relationship(self, db_session) -> None:
        """A series can choose a future managed destination independently."""
        current_root = LibraryRoot(name="Existing", path="/existing", enabled=True)
        preferred_root = LibraryRoot(name="Future", path="/future", enabled=True)
        db_session.add_all([current_root, preferred_root])
        await db_session.flush()

        series = Series(
            title="Saga",
            sort_title="Saga",
            path="/existing/Saga (2012)",
            library_root_id=current_root.id,
            preferred_library_root_id=preferred_root.id,
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        assert series.library_root is current_root
        assert series.preferred_library_root is preferred_root

        await db_session.refresh(preferred_root, attribute_names=["preferred_series"])
        assert preferred_root.preferred_series == [series]

    async def test_library_root_series_backref(self, db_session) -> None:
        """LibraryRoot.series returns all series in that root."""
        root = LibraryRoot(name="Comics", path="/comics", enabled=True)
        db_session.add(root)
        await db_session.flush()

        for title in ("Batman", "Saga", "Invincible"):
            series = Series(
                title=title,
                sort_title=title,
                library_root_id=root.id,
                status=SeriesStatus.CONTINUING,
                issue_count=0,
            )
            db_session.add(series)
        await db_session.flush()

        # Refresh to load the relationship
        await db_session.refresh(root, attribute_names=["series"])
        assert len(root.series) == 3
        titles = {s.title for s in root.series}
        assert titles == {"Batman", "Saga", "Invincible"}

    async def test_series_library_root_set_null_on_delete(self, db_session) -> None:
        """Deleting a library root sets series.library_root_id to NULL."""
        root = LibraryRoot(name="Comics", path="/comics", enabled=True)
        db_session.add(root)
        await db_session.flush()

        series = Series(
            title="X-Men",
            sort_title="X-Men",
            library_root_id=root.id,
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        series_id = series.id
        await db_session.delete(root)
        await db_session.flush()

        # Re-fetch the series
        reloaded = await db_session.get(Series, series_id)
        assert reloaded is not None
        assert reloaded.library_root_id is None

    async def test_multiple_series_in_same_root(self, db_session) -> None:
        """Multiple series can reference the same library root."""
        root = LibraryRoot(name="Comics", path="/comics", enabled=True)
        db_session.add(root)
        await db_session.flush()

        s1 = Series(
            title="Batman",
            sort_title="Batman",
            library_root_id=root.id,
            path="/comics/Batman (2024)",
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        s2 = Series(
            title="Superman",
            sort_title="Superman",
            library_root_id=root.id,
            path="/comics/Superman (2023)",
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add_all([s1, s2])
        await db_session.flush()

        result = await db_session.execute(select(Series).where(Series.library_root_id == root.id))
        series_list = list(result.scalars().all())
        assert len(series_list) == 2


# ── Issue.issue_type ───────────────────────────────────────────────


class TestIssueType:
    """Issue model issue_type column."""

    async def test_default_issue_type(self, db_session) -> None:
        """Issues default to IssueType.ISSUE."""
        series = Series(
            title="Batman",
            sort_title="Batman",
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        issue = Issue(
            series_id=series.id,
            issue_number=1.0,
            status=IssueStatus.WANTED,
        )
        db_session.add(issue)
        await db_session.flush()

        assert issue.issue_type == IssueType.ISSUE

    async def test_annual_type(self, db_session) -> None:
        """Issues can be classified as annuals."""
        series = Series(
            title="Hellblazer",
            sort_title="Hellblazer",
            status=SeriesStatus.ENDED,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        issue = Issue(
            series_id=series.id,
            issue_number=1.0,
            issue_type=IssueType.ANNUAL,
            status=IssueStatus.WANTED,
        )
        db_session.add(issue)
        await db_session.flush()

        assert issue.issue_type == IssueType.ANNUAL
        assert issue.issue_type.display_name == "Annual"

    @pytest.mark.parametrize("issue_type", list(IssueType))
    async def test_all_issue_types_persist(self, db_session, issue_type: IssueType) -> None:
        """All IssueType enum values can be stored and retrieved."""
        series = Series(
            title="Test",
            sort_title="Test",
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        issue = Issue(
            series_id=series.id,
            issue_number=1.0,
            issue_type=issue_type,
            status=IssueStatus.WANTED,
        )
        db_session.add(issue)
        await db_session.flush()

        # Re-fetch to confirm persistence
        result = await db_session.get(Issue, issue.id)
        assert result is not None
        assert result.issue_type == issue_type

    async def test_filter_by_issue_type(self, db_session) -> None:
        """Can query issues filtered by type."""
        series = Series(
            title="Batman",
            sort_title="Batman",
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        for i, itype in enumerate([IssueType.ISSUE, IssueType.ANNUAL, IssueType.ISSUE], start=1):
            db_session.add(
                Issue(
                    series_id=series.id,
                    issue_number=float(i),
                    issue_type=itype,
                    status=IssueStatus.WANTED,
                )
            )
        await db_session.flush()

        result = await db_session.execute(select(Issue).where(Issue.issue_type == IssueType.ANNUAL))
        annuals = list(result.scalars().all())
        assert len(annuals) == 1
        assert annuals[0].issue_number == 2.0


# ── Full pipeline: Series + LibraryRoot + Issues ───────────────────


class TestSeriesFolderFullPipeline:
    """End-to-end model relationships for the series folder feature."""

    async def test_complete_series_setup(self, db_session) -> None:
        """Create a series in a library root with mixed issue types."""
        root = LibraryRoot(name="Comics", path="/comics", enabled=True)
        db_session.add(root)
        await db_session.flush()

        series = Series(
            title="Invincible",
            sort_title="Invincible",
            year_start=2003,
            path="/comics/Invincible (2003)",
            library_root_id=root.id,
            status=SeriesStatus.ENDED,
            issue_count=144,
            monitored=True,
        )
        db_session.add(series)
        await db_session.flush()

        issues_data = [
            (1.0, IssueType.ISSUE),
            (2.0, IssueType.ISSUE),
            (1.0, IssueType.ANNUAL),  # Annual #1
        ]
        # Use unique issue numbers — annual gets 1001.0 to avoid UniqueConstraint
        issue_objs = []
        for num, itype in issues_data:
            actual_num = num + 1000 if itype != IssueType.ISSUE else num
            issue = Issue(
                series_id=series.id,
                issue_number=actual_num,
                issue_type=itype,
                status=IssueStatus.WANTED,
            )
            db_session.add(issue)
            issue_objs.append(issue)
        await db_session.flush()

        # Verify relationships
        await db_session.refresh(series, attribute_names=["issues", "library_root"])
        assert series.library_root is root
        assert series.path == "/comics/Invincible (2003)"
        assert len(series.issues) == 3

        # Verify issue type filtering
        regular = [i for i in series.issues if i.issue_type == IssueType.ISSUE]
        annuals = [i for i in series.issues if i.issue_type == IssueType.ANNUAL]
        assert len(regular) == 2
        assert len(annuals) == 1
