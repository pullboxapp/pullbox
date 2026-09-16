"""Tests for Phase 4 — series folder creation on add.

Tests the folder creation logic in SeriesService._create_series_folder(),
including naming config, collision handling, and error cases.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pullbox.core.events import EventBus
from pullbox.core.exceptions import ValidationError
from pullbox.models.config import SystemConfig
from pullbox.models.library import LibraryRoot, LibraryRootPolicy, LibraryRootPolicySource
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.services.series_service import SeriesService

# ── Helpers ────────────────────────────────────────────────────────


def _make_service() -> SeriesService:
    """Create a SeriesService with mocked dependencies."""
    mock_metadata = AsyncMock()
    event_bus = EventBus()
    return SeriesService(metadata_service=mock_metadata, event_bus=event_bus)


async def _seed_naming_config(session, **overrides) -> None:
    """Seed naming-related SystemConfig rows."""
    defaults = {
        "series_folder_template": ("{Series} ({Year})", "string"),
        "replace_illegal_characters": ("true", "bool"),
        "colon_replacement": ("dash", "string"),
    }
    for key, (value, vtype) in defaults.items():
        override_val = overrides.get(key, value)
        session.add(SystemConfig(key=key, value=override_val, value_type=vtype))
    await session.flush()


# ── Basic folder creation ──────────────────────────────────────────


class TestSeriesFolderCreation:
    """SeriesService._create_series_folder() basic behavior."""

    async def test_creates_folder_on_disk(self, db_session, tmp_path) -> None:
        """Folder is physically created in the library root."""
        root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=True)
        db_session.add(root)
        await db_session.flush()
        await _seed_naming_config(db_session)

        series = Series(
            title="Batman",
            sort_title="Batman",
            year_start=2024,
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        svc = _make_service()
        await svc._create_series_folder(db_session, series, root.id, comicvine_id=162083)

        expected_path = tmp_path / "Batman (2024)"
        assert expected_path.is_dir()
        assert series.path == str(expected_path)
        assert series.library_root_id == root.id
        assert series.preferred_library_root_id == root.id

    async def test_sets_series_path_and_root_id(self, db_session, tmp_path) -> None:
        """Series model fields are updated after folder creation."""
        root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=True)
        db_session.add(root)
        await db_session.flush()
        await _seed_naming_config(db_session)

        series = Series(
            title="Saga",
            sort_title="Saga",
            year_start=2012,
            status=SeriesStatus.ENDED,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        svc = _make_service()
        await svc._create_series_folder(db_session, series, root.id, comicvine_id=49263)

        assert series.path == str(tmp_path / "Saga (2012)")
        assert series.library_root_id == root.id

    async def test_uses_selected_roots_complete_naming_policy(self, db_session, tmp_path) -> None:
        root = LibraryRoot(name="Publisher layout", path=str(tmp_path), enabled=True)
        db_session.add(root)
        await db_session.flush()
        db_session.add(
            LibraryRootPolicy(
                library_root_id=root.id,
                schema_version=1,
                series_path_template="{Publisher}/{Series} ({Year})",
                comic_file_template="{Series} #{Issue:03d}",
                annual_file_template="{Series} Annual #{Issue:03d}",
                non_standard_file_template="{Series} {Type} {Volume:02d}",
                single_non_standard_file_template="{Series} {Type}",
                replace_illegal_characters=True,
                colon_replacement="dash",
                source=LibraryRootPolicySource.MANUAL,
                revision=1,
            )
        )
        await db_session.flush()

        series = Series(
            title="Batman",
            sort_title="Batman",
            year_start=2024,
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        await _make_service()._create_series_folder(
            db_session,
            series,
            root.id,
            comicvine_id=162083,
        )

        assert series.path == str(tmp_path / "Unknown" / "Batman (2024)")
        assert (tmp_path / "Unknown" / "Batman (2024)").is_dir()

    async def test_unknown_year_fallback(self, db_session, tmp_path) -> None:
        """Series without year_start uses 'Unknown' in folder name."""
        root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=True)
        db_session.add(root)
        await db_session.flush()
        await _seed_naming_config(db_session)

        series = Series(
            title="Mystery Series",
            sort_title="Mystery Series",
            year_start=None,
            status=SeriesStatus.UNKNOWN,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        svc = _make_service()
        await svc._create_series_folder(db_session, series, root.id, comicvine_id=99999)

        expected_path = tmp_path / "Mystery Series (Unknown)"
        assert expected_path.is_dir()
        assert series.path == str(expected_path)


# ── Collision handling ─────────────────────────────────────────────


class TestSeriesFolderCollision:
    """Folder name collision resolution with [cv-{id}] suffix."""

    async def test_appends_cv_suffix_on_collision(self, db_session, tmp_path) -> None:
        """Pre-existing folder triggers [cv-{id}] suffix."""
        root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=True)
        db_session.add(root)
        await db_session.flush()
        await _seed_naming_config(db_session)

        # Create the conflicting folder first
        (tmp_path / "Batman (2024)").mkdir()

        series = Series(
            title="Batman",
            sort_title="Batman",
            year_start=2024,
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        svc = _make_service()
        await svc._create_series_folder(db_session, series, root.id, comicvine_id=162083)

        expected_path = tmp_path / "Batman (2024) [cv-162083]"
        assert expected_path.is_dir()
        assert series.path == str(expected_path)

    async def test_no_suffix_when_no_collision(self, db_session, tmp_path) -> None:
        """No suffix when folder doesn't already exist."""
        root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=True)
        db_session.add(root)
        await db_session.flush()
        await _seed_naming_config(db_session)

        series = Series(
            title="Invincible",
            sort_title="Invincible",
            year_start=2003,
            status=SeriesStatus.ENDED,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        svc = _make_service()
        await svc._create_series_folder(db_session, series, root.id, comicvine_id=12345)

        expected_path = tmp_path / "Invincible (2003)"
        assert expected_path.is_dir()
        assert "[cv-" not in series.path

    async def test_reclaims_progress_only_folder_before_suffixing(
        self,
        db_session,
        tmp_path,
    ) -> None:
        """Leaked archive progress files should not force a fake [cv-{id}] collision."""
        root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=True)
        db_session.add(root)
        await db_session.flush()
        await _seed_naming_config(
            db_session,
            series_folder_template="{Series} ({Year}) [{Type}]",
        )

        leaked_folder = tmp_path / "Friendo (2019) [TPB]"
        leaked_folder.mkdir()
        (leaked_folder / "pullbox-archive-progress-deadbeef.json").write_text(
            '{"stage":"rewriting","current":1,"total":1,"unit":"entries"}',
            encoding="utf-8",
        )

        series = Series(
            title="Friendo",
            sort_title="Friendo",
            year_start=2019,
            status=SeriesStatus.ENDED,
            issue_count=0,
            series_type=SeriesType.TPB,
        )
        db_session.add(series)
        await db_session.flush()

        svc = _make_service()
        await svc._create_series_folder(db_session, series, root.id, comicvine_id=119294)

        expected_path = tmp_path / "Friendo (2019) [TPB]"
        assert expected_path.is_dir()
        assert series.path == str(expected_path)
        assert not (tmp_path / "Friendo (2019) [TPB] [cv-119294]").exists()
        assert not list(expected_path.glob("pullbox-archive-progress-*.json"))


# ── Custom templates ───────────────────────────────────────────────


class TestSeriesFolderCustomTemplate:
    """Folder creation with non-default naming templates."""

    async def test_publisher_in_template(self, db_session, tmp_path) -> None:
        """Template with {Publisher} token uses publisher name."""
        root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=True)
        db_session.add(root)
        await db_session.flush()
        await _seed_naming_config(
            db_session,
            series_folder_template="{Series} ({Year}) [{Publisher}]",
        )

        # Series without publisher (no FK set)
        series = Series(
            title="Saga",
            sort_title="Saga",
            year_start=2012,
            status=SeriesStatus.ENDED,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        svc = _make_service()
        await svc._create_series_folder(db_session, series, root.id, comicvine_id=49263)

        expected_path = tmp_path / "Saga (2012) [Unknown]"
        assert expected_path.is_dir()

    async def test_comicvine_id_in_template(self, db_session, tmp_path) -> None:
        """Template with {ComicVineId} token embeds the CV ID."""
        root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=True)
        db_session.add(root)
        await db_session.flush()
        await _seed_naming_config(
            db_session,
            series_folder_template="{Series} ({Year}) - {ComicVineId}",
        )

        series = Series(
            title="X-Men",
            sort_title="X-Men",
            year_start=2024,
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        svc = _make_service()
        await svc._create_series_folder(db_session, series, root.id, comicvine_id=155827)

        expected_path = tmp_path / "X-Men (2024) - 155827"
        assert expected_path.is_dir()


# ── Error cases ────────────────────────────────────────────────────


class TestSeriesFolderErrors:
    """Error handling in folder creation."""

    async def test_nonexistent_root_raises(self, db_session, tmp_path) -> None:
        """ValidationError when library root ID doesn't exist."""
        await _seed_naming_config(db_session)

        series = Series(
            title="Batman",
            sort_title="Batman",
            year_start=2024,
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        svc = _make_service()
        with pytest.raises(ValidationError, match="not found"):
            await svc._create_series_folder(db_session, series, 9999, comicvine_id=162083)

    async def test_disabled_root_raises(self, db_session, tmp_path) -> None:
        """ValidationError when library root is disabled."""
        root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=False)
        db_session.add(root)
        await db_session.flush()
        await _seed_naming_config(db_session)

        series = Series(
            title="Batman",
            sort_title="Batman",
            year_start=2024,
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        svc = _make_service()
        with pytest.raises(ValidationError, match="disabled"):
            await svc._create_series_folder(db_session, series, root.id, comicvine_id=162083)

    async def test_reference_only_root_raises(self, db_session, tmp_path) -> None:
        """ValidationError when a root does not allow managed writes."""
        root = LibraryRoot(
            name="Reference only",
            path=str(tmp_path),
            enabled=True,
            allow_referenced_registrations=True,
            allow_managed_writes=False,
        )
        db_session.add(root)
        await db_session.flush()
        await _seed_naming_config(db_session)

        series = Series(
            title="Batman",
            sort_title="Batman",
            year_start=2024,
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        svc = _make_service()
        with pytest.raises(ValidationError, match="managed writes"):
            await svc._create_series_folder(db_session, series, root.id, comicvine_id=162083)

    async def test_unavailable_root_raises(self, db_session, tmp_path) -> None:
        """ValidationError when a managed root is no longer available."""
        root = LibraryRoot(
            name="Offline",
            path=str(tmp_path / "missing"),
            enabled=True,
            allow_managed_writes=True,
        )
        db_session.add(root)
        await db_session.flush()
        await _seed_naming_config(db_session)

        series = Series(
            title="Batman",
            sort_title="Batman",
            year_start=2024,
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        svc = _make_service()
        with pytest.raises(ValidationError, match="existing directory"):
            await svc._create_series_folder(db_session, series, root.id, comicvine_id=162083)


# ── Config fallback ────────────────────────────────────────────────


class TestSeriesFolderConfigFallback:
    """Folder creation uses defaults when config keys are missing."""

    async def test_default_template_when_no_config(self, db_session, tmp_path) -> None:
        """Falls back to {Series} ({Year}) when no config in DB."""
        root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=True)
        db_session.add(root)
        await db_session.flush()
        # Deliberately NOT seeding config

        series = Series(
            title="Spider-Man",
            sort_title="Spider-Man",
            year_start=2022,
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add(series)
        await db_session.flush()

        svc = _make_service()
        await svc._create_series_folder(db_session, series, root.id, comicvine_id=140120)

        expected_path = tmp_path / "Spider-Man (2022)"
        assert expected_path.is_dir()
        assert series.path == str(expected_path)


# ── Schema validation ──────────────────────────────────────────────


class TestSeriesCreateSchema:
    """SeriesCreate schema accepts library_root_id."""

    def test_library_root_id_optional(self) -> None:
        """library_root_id defaults to None."""
        from pullbox.schemas.series import SeriesCreate

        body = SeriesCreate(comicvine_id=162083)
        assert body.library_root_id is None

    def test_library_root_id_set(self) -> None:
        """library_root_id can be set to a positive integer."""
        from pullbox.schemas.series import SeriesCreate

        body = SeriesCreate(comicvine_id=162083, library_root_id=1)
        assert body.library_root_id == 1

    def test_library_root_id_zero_rejected(self) -> None:
        """library_root_id=0 is rejected by gt=0 validator."""
        from pydantic import ValidationError as PydanticValidationError

        from pullbox.schemas.series import SeriesCreate

        with pytest.raises(PydanticValidationError):
            SeriesCreate(comicvine_id=162083, library_root_id=0)
