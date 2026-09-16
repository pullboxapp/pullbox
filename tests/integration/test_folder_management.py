"""Tests for Phase 7 — folder management, rename, and type detection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pullbox.core.naming import detect_issue_type, format_series_folder
from pullbox.models.library import LibraryRoot
from pullbox.models.series import Series

# ═══════════════════════════════════════════════════════════════════════
# 1. Issue type detection from filenames
# ═══════════════════════════════════════════════════════════════════════


class TestIssueTypeDetection:
    """detect_issue_type() should classify comics from filename text."""

    def test_standard_issue(self):
        assert detect_issue_type("Batman (2024) #017") == "issue"

    def test_annual_detection(self):
        assert detect_issue_type("Batman Annual #1 (2024)") == "annual"

    def test_annual_case_insensitive(self):
        assert detect_issue_type("batman annual 03") == "annual"

    def test_tpb_detection(self):
        assert detect_issue_type("East of West TPB v03") == "tpb"

    def test_omnibus_detection(self):
        assert detect_issue_type("Beasts of Burden Omnibus v01") == "omnibus"

    def test_compendium_detection(self):
        assert detect_issue_type("Rising Stars Compendium v01") == "compendium"

    def test_graphic_novel_detection(self):
        assert detect_issue_type("The College Try Graphic Novel") == "gn"

    def test_hardcover_detection(self):
        assert detect_issue_type("Cairo HC v01") == "hc"

    def test_one_shot_detection(self):
        assert detect_issue_type("Ignite Prime One-Shot") == "one_shot"

    def test_special_detection(self):
        assert detect_issue_type("Christmas Special 2025") == "special"

    def test_deluxe_detection(self):
        assert detect_issue_type("Batman Year Three Deluxe v01") == "deluxe"

    def test_no_match_returns_issue(self):
        assert detect_issue_type("Amazing Spider-Man #022") == "issue"

    def test_empty_string(self):
        assert detect_issue_type("") == "issue"

    def test_annual_takes_priority(self):
        """Annual should match before Special if both present."""
        # annual pattern comes first in the list
        assert detect_issue_type("Batman Annual Special") == "annual"


# ═══════════════════════════════════════════════════════════════════════
# 2. Series folder rename logic
# ═══════════════════════════════════════════════════════════════════════


class TestRenameFolderLogic:
    """format_series_folder() drives rename decisions."""

    def test_standard_folder_name(self):
        name = format_series_folder("Batman", year=2024, template="{Series} ({Year})")
        assert name == "Batman (2024)"

    def test_with_publisher(self):
        name = format_series_folder(
            "Batman",
            year=2024,
            publisher="DC Comics",
            template="{Series} ({Year}) [{Publisher}]",
        )
        assert name == "Batman (2024) [DC Comics]"

    def test_colon_replacement_dash(self):
        name = format_series_folder(
            "Batman: The Dark Knight",
            year=2024,
            template="{Series} ({Year})",
            colon_replacement="dash",
        )
        assert ":" not in name
        assert "Batman -" in name or "Batman  -" in name

    def test_colon_replacement_smart(self):
        name = format_series_folder(
            "Batman: The Dark Knight",
            year=2024,
            template="{Series} ({Year})",
            colon_replacement="smart",
        )
        assert ":" not in name
        assert "\u2014" in name  # em-dash

    def test_missing_year(self):
        name = format_series_folder("Batman", year=None, template="{Series} ({Year})")
        assert name == "Batman (Unknown)"


# ═══════════════════════════════════════════════════════════════════════
# 3. Rename series folder (service method)
# ═══════════════════════════════════════════════════════════════════════


class TestRenameSeriesFolder:
    """SeriesService.rename_series_folder() renames on disk."""

    @pytest.fixture
    def series_svc(self):
        from pullbox.services.series_service import SeriesService

        return SeriesService(
            metadata_service=MagicMock(),
            event_bus=MagicMock(),
        )

    @pytest.mark.asyncio
    async def test_rename_when_template_changes(self, tmp_path, series_svc):
        """Folder is renamed when template produces a different name."""
        # Current folder
        old_folder = tmp_path / "Batman (2024)"
        old_folder.mkdir()

        # Mock series
        series = Series(
            id=1,
            title="Batman",
            sort_title="Batman",
            year_start=2024,
            comicvine_id=12345,
            path=str(old_folder),
            library_root_id=1,
        )

        # Mock library root
        root = MagicMock()
        root.path = str(tmp_path)

        # Mock config — new template includes publisher
        config_data = {
            "series_folder_template": "{Series} ({Year}) [{Publisher}]",
            "replace_illegal_characters": "true",
            "colon_replacement": "dash",
        }

        session = AsyncMock()
        session.get = AsyncMock(
            side_effect=lambda cls, id: series if cls is not LibraryRoot else root,
        )

        # Mock _load_naming_config
        with (
            patch.object(
                series_svc.__class__,
                "_load_naming_config",
                new_callable=AsyncMock,
                return_value=config_data,
            ),
            patch(
                "pullbox.services.series_service.require_mutable_library_target",
                new_callable=AsyncMock,
            ),
        ):
            new_path = await series_svc.rename_series_folder(session, 1)

        assert new_path is not None
        assert "Unknown" in new_path  # publisher is None → "Unknown"
        assert not old_folder.exists()
        assert Path(new_path).exists()

    @pytest.mark.asyncio
    async def test_no_rename_when_already_correct(self, tmp_path, series_svc):
        """No rename happens when folder already matches template."""
        folder = tmp_path / "Batman (2024)"
        folder.mkdir()

        series = Series(
            id=1,
            title="Batman",
            sort_title="Batman",
            year_start=2024,
            comicvine_id=12345,
            path=str(folder),
            library_root_id=1,
        )

        root = MagicMock()
        root.path = str(tmp_path)

        config_data = {
            "series_folder_template": "{Series} ({Year})",
            "replace_illegal_characters": "true",
            "colon_replacement": "dash",
        }

        session = AsyncMock()
        session.get = AsyncMock(
            side_effect=lambda cls, id: series if cls is not LibraryRoot else root,
        )

        with patch.object(
            series_svc.__class__,
            "_load_naming_config",
            new_callable=AsyncMock,
            return_value=config_data,
        ):
            new_path = await series_svc.rename_series_folder(session, 1)

        assert new_path is None
        assert folder.exists()

    @pytest.mark.asyncio
    async def test_returns_none_for_series_without_path(self, series_svc):
        """Series without a path returns None."""
        series = MagicMock()
        series.path = None
        series.library_root_id = None

        session = AsyncMock()
        session.get = AsyncMock(return_value=series)

        result = await series_svc.rename_series_folder(session, 1)
        assert result is None

    @pytest.mark.asyncio
    async def test_collision_appends_cv_id(self, tmp_path, series_svc):
        """When target folder exists, appends [cv-{id}]."""
        old_folder = tmp_path / "Old Name (2024)"
        old_folder.mkdir()

        # Existing folder with the target name
        target = tmp_path / "Batman (2024)"
        target.mkdir()

        series = Series(
            id=1,
            title="Batman",
            sort_title="Batman",
            year_start=2024,
            comicvine_id=99999,
            path=str(old_folder),
            library_root_id=1,
        )

        root = MagicMock()
        root.path = str(tmp_path)

        config_data = {
            "series_folder_template": "{Series} ({Year})",
            "replace_illegal_characters": "true",
            "colon_replacement": "dash",
        }

        session = AsyncMock()
        session.get = AsyncMock(
            side_effect=lambda cls, id: series if cls is not LibraryRoot else root,
        )

        with (
            patch.object(
                series_svc.__class__,
                "_load_naming_config",
                new_callable=AsyncMock,
                return_value=config_data,
            ),
            patch(
                "pullbox.services.series_service.require_mutable_library_target",
                new_callable=AsyncMock,
            ),
        ):
            new_path = await series_svc.rename_series_folder(session, 1)

        assert new_path is not None
        assert "[cv-99999]" in new_path
        assert not old_folder.exists()
