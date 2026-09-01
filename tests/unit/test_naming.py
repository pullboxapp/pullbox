"""Tests for core.naming — filesystem sanitization, folder/file formatting,
type detection, and preview generation.

These are pure unit tests with no I/O or database dependencies.
"""

from __future__ import annotations

import pytest

from pullbox.core.naming import (
    DEFAULT_COLLECTION_NON_STANDARD_FILE_TEMPLATE,
    DEFAULT_SINGLE_NON_STANDARD_FILE_TEMPLATE,
    PREVIEW_EXAMPLES,
    classify_series_type,
    detect_issue_type,
    detect_issue_type_from_metadata_title,
    detect_series_type_from_description,
    detect_series_type_from_issue_count,
    format_comic_file,
    format_filename,
    format_series_folder,
    get_naming_preview,
    parse_filename,
    resolve_collection_non_standard_file_template,
    resolve_non_standard_file_template,
    resolve_single_non_standard_file_template,
    sanitize_for_filesystem,
)
from pullbox.models.issue import IssueType

# ── IssueType enum ─────────────────────────────────────────────────


class TestIssueType:
    """IssueType enum and display names."""

    def test_all_values_exist(self) -> None:
        assert len(IssueType) == 12

    @pytest.mark.parametrize(
        ("member", "expected"),
        [
            (IssueType.ISSUE, ""),
            (IssueType.ANNUAL, "Annual"),
            (IssueType.TPB, "TPB"),
            (IssueType.OMNIBUS, "Omnibus"),
            (IssueType.GN, "Graphic Novel"),
            (IssueType.OGN, "Original Graphic Novel"),
            (IssueType.HC, "Hardcover"),
            (IssueType.ONE_SHOT, "One Shot"),
            (IssueType.SPECIAL, "Special"),
            (IssueType.DELUXE, "Deluxe Edition"),
            (IssueType.COMPENDIUM, "Compendium"),
            (IssueType.VOLUME, "Volume"),
        ],
    )
    def test_display_name(self, member: IssueType, expected: str) -> None:
        assert member.display_name == expected

    def test_string_value_roundtrip(self) -> None:
        for member in IssueType:
            assert IssueType(member.value) is member


# ── sanitize_for_filesystem ────────────────────────────────────────


class TestSanitizeForFilesystem:
    """Filesystem name sanitization."""

    def test_clean_name_unchanged(self) -> None:
        assert sanitize_for_filesystem("Batman") == "Batman"

    def test_removes_illegal_characters(self) -> None:
        assert sanitize_for_filesystem('Bat<man>: "Rise"') == "Batman - Rise"

    def test_slashes_become_separators(self) -> None:
        assert (
            sanitize_for_filesystem("Batman/Superman: World's Finest")
            == "Batman - Superman - World's Finest"
        )

    def test_many_slashes_are_replaced_linearly(self) -> None:
        result = sanitize_for_filesystem("Batman" + " /" * 25 + " Superman")

        assert result.startswith("Batman -")
        assert result.endswith("Superman")

    def test_colon_replacement_dash(self) -> None:
        assert (
            sanitize_for_filesystem("Batman: Year One", colon_replacement="dash")
            == "Batman - Year One"
        )

    def test_colon_replacement_space(self) -> None:
        assert (
            sanitize_for_filesystem("Batman: Year One", colon_replacement="space")
            == "Batman Year One"
        )

    def test_colon_replacement_empty(self) -> None:
        assert (
            sanitize_for_filesystem("Batman: Year One", colon_replacement="empty")
            == "Batman Year One"
        )

    def test_colon_replacement_smart(self) -> None:
        result = sanitize_for_filesystem("Batman: Year One", colon_replacement="smart")
        assert "\u2014" in result  # em-dash

    def test_empty_name_returns_unknown(self) -> None:
        assert sanitize_for_filesystem("") == "Unknown"

    def test_collapses_multiple_spaces(self) -> None:
        assert sanitize_for_filesystem("Batman   Returns") == "Batman Returns"

    def test_strips_trailing_dots(self) -> None:
        assert sanitize_for_filesystem("Batman...") == "Batman"

    def test_reserved_windows_names(self) -> None:
        assert sanitize_for_filesystem("CON") == "_CON"
        assert sanitize_for_filesystem("NUL") == "_NUL"
        assert sanitize_for_filesystem("COM1") == "_COM1"
        assert sanitize_for_filesystem("LPT3") == "_LPT3"

    def test_non_reserved_name_untouched(self) -> None:
        assert sanitize_for_filesystem("CONNECT") == "CONNECT"

    def test_unicode_preserved(self) -> None:
        assert sanitize_for_filesystem("バットマン") == "バットマン"

    def test_truncates_very_long_names(self) -> None:
        long_name = "A" * 300
        result = sanitize_for_filesystem(long_name)
        assert len(result.encode("utf-8")) <= 240

    def test_no_sanitization_when_disabled(self) -> None:
        result = sanitize_for_filesystem("Bat:man", replace_illegal=False)
        assert ":" in result


# ── format_series_folder ───────────────────────────────────────────


class TestFormatSeriesFolder:
    """Series folder naming from template."""

    def test_default_template(self) -> None:
        assert format_series_folder("Invincible", 2003) == "Invincible (2003)"

    def test_missing_year(self) -> None:
        assert format_series_folder("Saga", None) == "Saga (Unknown)"

    def test_publisher_token(self) -> None:
        result = format_series_folder(
            "Batman",
            2016,
            publisher="DC Comics",
            template="{Series} ({Year}) [{Publisher}]",
        )
        assert result == "Batman (2016) [DC Comics]"

    def test_comicvine_id_token(self) -> None:
        result = format_series_folder(
            "Batman",
            2016,
            comicvine_id=12345,
            template="{Series} ({Year}) [cv-{ComicVineId}]",
        )
        assert result == "Batman (2016) [cv-12345]"

    def test_title_with_colon(self) -> None:
        result = format_series_folder("Batman: Year One", 1987)
        assert result == "Batman - Year One (1987)"

    def test_all_tokens(self) -> None:
        result = format_series_folder(
            "Batman",
            2016,
            publisher="DC",
            comicvine_id=99,
            template="{Publisher} - {Series} ({Year}) {ComicVineId}",
        )
        assert result == "DC - Batman (2016) 99"

    def test_type_token_tpb(self) -> None:
        result = format_series_folder(
            "Batman",
            2016,
            series_type="tpb",
            template="{Series} ({Year}) [{Type}]",
        )
        assert result == "Batman (2016) [TPB]"

    def test_type_token_standard_produces_empty(self) -> None:
        """Standard series type produces no type suffix — brackets are cleaned up."""
        result = format_series_folder(
            "Batman",
            2016,
            series_type="standard",
            template="{Series} ({Year}) [{Type}]",
        )
        assert result == "Batman (2016)"

    def test_type_token_none_produces_empty(self) -> None:
        result = format_series_folder(
            "Batman",
            2016,
            series_type=None,
            template="{Series} ({Year}) [{Type}]",
        )
        assert result == "Batman (2016)"

    def test_type_token_hardcover(self) -> None:
        result = format_series_folder(
            "Cairo",
            2007,
            series_type="hardcover",
            template="{Series} ({Year}) [{Type}]",
        )
        assert result == "Cairo (2007) [HC]"

    def test_type_token_annual(self) -> None:
        result = format_series_folder(
            "Hellblazer",
            1988,
            series_type="annual",
            template="{Series} ({Year}) [{Type}]",
        )
        assert result == "Hellblazer (1988) [Annual]"

    def test_type_token_one_shot(self) -> None:
        result = format_series_folder(
            "Ignite Prime",
            2026,
            series_type="one_shot",
            template="{Series} ({Year}) [{Type}]",
        )
        assert result == "Ignite Prime (2026) [One-Shot]"

    def test_type_token_omnibus(self) -> None:
        result = format_series_folder(
            "Darkwing Duck",
            2026,
            series_type="omnibus",
            template="{Series} ({Year}) [{Type}]",
        )
        assert result == "Darkwing Duck (2026) [Omnibus]"

    def test_type_with_all_tokens(self) -> None:
        result = format_series_folder(
            "Batman",
            2016,
            publisher="DC Comics",
            comicvine_id=12345,
            series_type="tpb",
            template="{Publisher} - {Series} ({Year}) [{Type}] [cv-{ComicVineId}]",
        )
        assert result == "DC Comics - Batman (2016) [TPB] [cv-12345]"

    def test_type_only_template(self) -> None:
        """Edge case: template with only {Type}."""
        result = format_series_folder(
            "Batman",
            2016,
            series_type="tpb",
            template="{Type}",
        )
        assert result == "TPB"

    def test_type_deluxe(self) -> None:
        result = format_series_folder(
            "Batman - Year Three",
            2025,
            series_type="deluxe",
            template="{Series} ({Year}) [{Type}]",
        )
        assert result == "Batman - Year Three (2025) [Deluxe]"

    def test_type_graphic_novel(self) -> None:
        result = format_series_folder(
            "The College Try",
            2026,
            series_type="graphic_novel",
            template="{Series} ({Year}) [{Type}]",
        )
        assert result == "The College Try (2026) [GN]"


# ── format_comic_file ─────────────────────────────────────────────


class TestFormatComicFile:
    """Comic file naming from template with all token types."""

    def test_standard_issue(self) -> None:
        result = format_comic_file(
            "Batman",
            year=2024,
            issue=17,
            template="{Series} ({Year}) #{Issue:03d}",
        )
        assert result == "Batman (2024) #017.cbz"

    def test_annual(self) -> None:
        result = format_comic_file(
            "Hellblazer",
            year=1988,
            issue=1,
            issue_type="annual",
            template="{Series} ({Year}) Annual #{Issue:03d}",
        )
        assert result == "Hellblazer (1988) Annual #001.cbz"

    def test_tpb_with_volume(self) -> None:
        result = format_comic_file(
            "East of West",
            year=2014,
            volume=3,
            issue_type="tpb",
            template="{Series} v{Volume:02d} ({Year})",
        )
        assert result == "East of West v03 (2014).cbz"

    def test_omnibus(self) -> None:
        result = format_comic_file(
            "Beasts of Burden",
            year=2025,
            issue_type="omnibus",
            template="{Series} ({Year}) {Type}",
        )
        assert result == "Beasts of Burden (2025) Omnibus.cbz"

    def test_one_shot_no_issue_number(self) -> None:
        result = format_comic_file(
            "Ignite Prime",
            year=2026,
            issue_type="one_shot",
            template="{Series} ({Year}) {Type}",
        )
        assert result == "Ignite Prime (2026) One-Shot.cbz"

    def test_type_token_empty_for_standard_issue(self) -> None:
        result = format_comic_file(
            "Batman",
            year=2024,
            issue=5,
            issue_type="issue",
            template="{Series} ({Year}) {Type} #{Issue:03d}",
        )
        assert result == "Batman (2024) #005.cbz"

    def test_missing_issue_cleans_up_hash(self) -> None:
        result = format_comic_file(
            "Cairo",
            year=2007,
            issue_type="hc",
            template="{Series} ({Year}) #{Issue:03d}",
        )
        assert result == "Cairo (2007).cbz"

    def test_missing_volume_cleans_up(self) -> None:
        result = format_comic_file(
            "Batman",
            year=2024,
            issue=1,
            template="{Series} v{Volume:02d} ({Year}) #{Issue:03d}",
        )
        assert result == "Batman (2024) #001.cbz"

    def test_custom_extension(self) -> None:
        result = format_comic_file(
            "Batman",
            year=2024,
            issue=1,
            template="{Series} ({Year}) #{Issue:03d}",
            extension="cbr",
        )
        assert result == "Batman (2024) #001.cbr"

    def test_publisher_token(self) -> None:
        result = format_comic_file(
            "Batman",
            year=2024,
            issue=1,
            publisher="DC Comics",
            template="{Publisher} - {Series} #{Issue:03d}",
        )
        assert result == "DC Comics - Batman #001.cbz"

    def test_legacy_edition_token_is_stripped(self) -> None:
        result = format_comic_file(
            "Batman - Year Three",
            year=2025,
            issue_type="deluxe",
            template="{Series} ({Year}) {Edition}",
        )
        assert result == "Batman - Year Three (2025).cbz"

    def test_final_filename_is_sanitized_after_token_replacement(self) -> None:
        result = format_comic_file(
            "Batman",
            year=2024,
            issue=1,
            publisher="DC: Comics/Prime",
            template="{Publisher} - {Series} #{Issue:03d}",
        )
        assert result == "DC - Comics - Prime - Batman #001.cbz"

    def test_year_missing_shows_unknown(self) -> None:
        result = format_comic_file(
            "Batman",
            year=None,
            issue=1,
            template="{Series} ({Year}) #{Issue:03d}",
        )
        assert result == "Batman (Unknown) #001.cbz"

    def test_issue_decimal(self) -> None:
        result = format_comic_file(
            "Batman",
            year=2024,
            issue=15.1,
            template="{Series} ({Year}) #{Issue}",
        )
        assert result == "Batman (2024) #15.1.cbz"

    def test_colon_in_series_title(self) -> None:
        result = format_comic_file(
            "Batman: Year One",
            year=1987,
            issue=1,
            template="{Series} ({Year}) #{Issue:03d}",
        )
        assert result == "Batman - Year One (1987) #001.cbz"

    def test_title_token_in_non_standard_template(self) -> None:
        result = format_comic_file(
            "AL15",
            year=2021,
            issue_type="volume",
            title="Broken Dreams",
            template="{Series} ({Year}) {Type} - {Title}",
        )
        assert result == "AL15 (2021) Vol - Broken Dreams.cbz"

    def test_missing_title_cleans_up_trailing_separator(self) -> None:
        result = format_comic_file(
            "AL15",
            year=2021,
            issue_type="volume",
            title=None,
            template="{Series} ({Year}) {Type} - {Title}",
        )
        assert result == "AL15 (2021) Vol.cbz"


# ── format_filename (backward compat) ─────────────────────────────


class TestFormatFilenameLegacy:
    """Legacy format_filename function — backward compatibility."""

    def test_basic(self) -> None:
        result = format_filename("Batman", 1, 2016)
        assert result == "Batman (2016) #001.cbz"

    def test_large_issue_number_never_uses_scientific_notation(self) -> None:
        result = format_filename("Wonder Woman", 1_000_000.0, 1998)
        assert result == "Wonder Woman (1998) #1000000.cbz"

    def test_lowercase_tokens_normalized(self) -> None:
        result = format_filename(
            "Batman",
            42,
            2016,
            template="{series} ({year}) #{issue:03d}",
        )
        assert result == "Batman (2016) #042.cbz"

    def test_custom_template(self) -> None:
        result = format_filename(
            "Saga",
            7,
            2012,
            template="{series} #{issue:03d} ({year})",
        )
        assert result == "Saga #007 (2012).cbz"


# ── detect_issue_type ──────────────────────────────────────────────


class TestDetectIssueType:
    """Issue type detection from filename text."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Batman 045 [2026] [Digital]", "issue"),
            ("Hellblazer Annual 001 [1989]", "annual"),
            ("East of West v3 TPB", "tpb"),
            ("Beasts of Burden Omnibus", "omnibus"),
            ("The College Try [Graphic Novel]", "gn"),
            ("New Teen Titans [Original Graphic Novel]", "ogn"),
            ("Batman [OGN]", "ogn"),
            ("Cairo Hardcover (2007)", "hc"),
            ("Silver v02 [Digital HC]", "hc"),
            ("Ignite Prime [One Shot]", "one_shot"),
            ("Thanksgiving [One-Shot]", "one_shot"),
            ("Silverline Christmas [Special]", "special"),
            ("Batman Year Three Deluxe Edition", "deluxe"),
            ("Rising Stars Compendium", "compendium"),
            ("Batman Vol.1", "volume"),
            ("Batman Volume 2", "volume"),
            ("Batman Vol 3", "volume"),
        ],
    )
    def test_detection(self, text: str, expected: str) -> None:
        assert detect_issue_type(text) == expected

    def test_default_is_issue(self) -> None:
        assert detect_issue_type("Batman 045 [2026]") == "issue"

    def test_case_insensitive(self) -> None:
        assert detect_issue_type("annual ANNUAL Annual") == "annual"

    def test_priority_annual_over_special(self) -> None:
        """Annual should match before Special since it's checked first."""
        assert detect_issue_type("Annual Special Edition") == "annual"


class TestDetectIssueTypeFromMetadataTitle:
    """Provider issue titles use narrower evidence than release filenames."""

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Holiday Special", "special"),
            ("Special Edition", "issue"),
            ("HC/TPB", "volume"),
            ("Annual Special", "annual"),
            ("Epic Collection", "volume"),
            ("Questions", "issue"),
        ],
    )
    def test_metadata_title_detection(self, title: str, expected: str) -> None:
        assert detect_issue_type_from_metadata_title(title) == expected


# ── get_naming_preview ─────────────────────────────────────────────


class TestGetNamingPreview:
    """Preview generation for settings UI."""

    def test_standard_only_returns_issues(self) -> None:
        results = get_naming_preview("{Series} ({Year}) #{Issue:03d}", "standard")
        assert len(results) > 0
        for r in results:
            assert r["output"].endswith(".cbz")
            assert "#" in r["output"]

    def test_annual_only_returns_annuals(self) -> None:
        results = get_naming_preview("{Series} ({Year}) Annual #{Issue:03d}", "annual")
        assert len(results) > 0
        for r in results:
            assert "Annual" in r["output"]

    def test_non_standard_excludes_issues_and_annuals(self) -> None:
        results = get_naming_preview("{Series} ({Year}) {Type}", "non_standard")
        assert len(results) > 0
        for r in results:
            assert "[issue]" not in r["input"]
            assert "[annual]" not in r["input"]

    def test_folder_returns_deduplicated(self) -> None:
        results = get_naming_preview("{Series} ({Year})", "folder")
        outputs = [r["output"] for r in results]
        assert len(outputs) == len(set(outputs)), "Folder preview should be deduplicated"
        assert len(results) <= 6

    def test_preview_has_input_and_output(self) -> None:
        results = get_naming_preview("{Series} ({Year}) #{Issue:03d}", "standard")
        for r in results:
            assert "input" in r
            assert "output" in r
            assert len(r["input"]) > 0
            assert len(r["output"]) > 0

    def test_folder_preview_no_extension(self) -> None:
        results = get_naming_preview("{Series} ({Year})", "folder")
        for r in results:
            assert not r["output"].endswith(".cbz")

    def test_folder_preview_preserves_nested_path_segments(self) -> None:
        results = get_naming_preview("{Publisher}/{Series} ({Year})", "folder")
        outputs = [result["output"] for result in results]

        assert "DC Comics/Absolute Batman (2024)" in outputs

    def test_non_standard_type_display(self) -> None:
        results = get_naming_preview("{Series} {Type} ({Year})", "non_standard")
        type_words = {
            "TPB",
            "Omnibus",
            "GN",
            "HC",
            "One-Shot",
            "Special",
            "Deluxe",
            "Compendium",
            "Vol",
        }
        for r in results:
            found = any(tw in r["output"] for tw in type_words)
            assert found, f"Expected a type word in: {r['output']}"

    def test_non_standard_preview_includes_title_token(self) -> None:
        results = get_naming_preview("{Series} ({Year}) {Type} - {Title}", "non_standard")
        assert any("Howling Tome" in r["output"] for r in results)

    def test_non_standard_preview_uses_volume_default_shape(self) -> None:
        results = get_naming_preview(
            "{Series} ({Year}) {Type} {Volume:02d} - {Title}",
            "non_standard_collection",
        )
        assert any("TPB 01 - The Howling Tome" in r["output"] for r in results)

    def test_collection_non_standard_preview_excludes_single_release_types(self) -> None:
        results = get_naming_preview(
            "{Series} ({Year}) {Type} {Volume:02d} - {Title}",
            "non_standard_collection",
        )
        assert results
        assert all(
            "[one_shot]" not in r["input"]
            and "[special]" not in r["input"]
            and "[gn]" not in r["input"]
            for r in results
        )

    def test_single_non_standard_preview_excludes_collection_types(self) -> None:
        results = get_naming_preview(
            "{Series} ({Year}) {Type} - {Title}",
            "non_standard_single",
        )
        assert results
        assert all(
            "[tpb]" not in r["input"]
            and "[omnibus]" not in r["input"]
            and "[compendium]" not in r["input"]
            and "[hc]" not in r["input"]
            and "[deluxe]" not in r["input"]
            and "[volume]" not in r["input"]
            for r in results
        )
        assert any("One-Shot" in r["output"] for r in results)


class TestResolveNonStandardFileTemplate:
    """Default normalization for non-standard naming templates."""

    def test_blank_collection_uses_current_default(self) -> None:
        assert resolve_collection_non_standard_file_template("") == (
            DEFAULT_COLLECTION_NON_STANDARD_FILE_TEMPLATE
        )

    def test_blank_single_uses_current_default(self) -> None:
        assert (
            resolve_single_non_standard_file_template("")
            == DEFAULT_SINGLE_NON_STANDARD_FILE_TEMPLATE
        )

    def test_legacy_default_upgrades_to_collection_default(self) -> None:
        assert resolve_collection_non_standard_file_template("{Series} ({Year}) {Type}") == (
            DEFAULT_COLLECTION_NON_STANDARD_FILE_TEMPLATE
        )

    def test_previous_default_upgrades_to_collection_default(self) -> None:
        assert (
            resolve_collection_non_standard_file_template("{Series} ({Year}) {Type} - {Title}")
            == DEFAULT_COLLECTION_NON_STANDARD_FILE_TEMPLATE
        )

    def test_previous_default_upgrades_to_single_default(self) -> None:
        assert (
            resolve_single_non_standard_file_template("{Series} ({Year}) {Type} - {Title}")
            == DEFAULT_SINGLE_NON_STANDARD_FILE_TEMPLATE
        )

    def test_legacy_resolver_remains_collection_alias(self) -> None:
        assert (
            resolve_non_standard_file_template("") == DEFAULT_COLLECTION_NON_STANDARD_FILE_TEMPLATE
        )


# ── PREVIEW_EXAMPLES data integrity ───────────────────────────────


class TestPreviewExamples:
    """Verify the hardcoded preview examples are well-formed."""

    def test_minimum_count(self) -> None:
        assert len(PREVIEW_EXAMPLES) >= 10

    def test_all_types_covered(self) -> None:
        types_present = {ex.issue_type for ex in PREVIEW_EXAMPLES}
        expected = {
            "issue",
            "annual",
            "tpb",
            "volume",
            "omnibus",
            "gn",
            "hc",
            "one_shot",
            "special",
            "deluxe",
            "compendium",
        }
        assert expected == types_present

    def test_all_have_series_and_year(self) -> None:
        for ex in PREVIEW_EXAMPLES:
            assert ex.series, f"Missing series: {ex}"
            # year can be None but all our examples have it
            assert ex.year is not None, f"Missing year: {ex}"

    def test_standard_issues_have_issue_number(self) -> None:
        for ex in PREVIEW_EXAMPLES:
            if ex.issue_type == "issue":
                assert ex.issue is not None, f"Standard issue missing issue number: {ex}"


# ── parse_filename (unchanged behavior) ───────────────────────────


class TestParseFilename:
    """Filename parsing — ensure no regressions from the refactor."""

    def test_standard_format(self) -> None:
        result = parse_filename("Batman (2016) #045 (Digital) (Zone-Empire).cbz")
        assert result is not None
        assert result.series == "Batman"
        assert result.issue_number == 45.0
        assert result.year == 2016
        assert result.extension == "cbz"

    def test_number_before_year(self) -> None:
        result = parse_filename("Batman 045 (2016) (Digital-Empire).cbz")
        assert result is not None
        assert result.series == "Batman"
        assert result.issue_number == 45.0

    def test_minimal_with_hash(self) -> None:
        result = parse_filename("Saga #7.cbz")
        assert result is not None
        assert result.series == "Saga"
        assert result.issue_number == 7.0

    def test_no_match(self) -> None:
        assert parse_filename("just-some-random-text") is None

    def test_tags_extracted(self) -> None:
        result = parse_filename("Batman Annual 001 (2026) (Digital).cbz")
        assert result is not None
        assert "Annual" in result.tags
        assert "Digital" in result.tags


# ── detect_series_type_from_description (Tier 2) ──────────────────


class TestDetectSeriesTypeFromDescription:
    """Tier 2: series type detection from ComicVine description text."""

    def test_generic_collects_is_volume(self) -> None:
        assert detect_series_type_from_description("Collects Batman #50-55") == "volume"

    def test_reprints_collects_label_is_volume(self) -> None:
        assert detect_series_type_from_description("Reprints/Collects: Batman #587-590") == "volume"

    def test_one_shot(self) -> None:
        assert detect_series_type_from_description("A one-shot story exploring...") == "one_shot"

    def test_graphic_novel(self) -> None:
        result = detect_series_type_from_description("An original graphic novel by...")
        assert result == "graphic_novel"

    def test_hardcover_before_collects(self) -> None:
        result = detect_series_type_from_description("This hardcover edition collects...")
        assert result == "hardcover"

    def test_omnibus(self) -> None:
        result = detect_series_type_from_description("The complete omnibus collecting...")
        assert result == "omnibus"

    def test_compendium(self) -> None:
        result = detect_series_type_from_description("The Compendium edition gathers...")
        assert result == "compendium"

    def test_deluxe(self) -> None:
        assert detect_series_type_from_description("A Deluxe oversized edition...") == "deluxe"

    def test_trade_paperback(self) -> None:
        assert detect_series_type_from_description("Trade Paperback collecting...") == "tpb"

    def test_tpb_abbreviation(self) -> None:
        assert detect_series_type_from_description("TPB edition of the series") == "tpb"

    def test_annual(self) -> None:
        assert detect_series_type_from_description("Annual special featuring...") == "annual"

    def test_special(self) -> None:
        assert detect_series_type_from_description("A special issue celebrating...") == "special"

    def test_false_positive_includes_one_shot(self) -> None:
        assert (
            detect_series_type_from_description("This series includes a one-shot bonus story")
            == "standard"
        )

    def test_false_positive_features_one_shot(self) -> None:
        assert (
            detect_series_type_from_description(
                "Features a one-shot from writer X alongside the main arc"
            )
            == "standard"
        )

    def test_false_positive_following_the_one_shot(self) -> None:
        assert (
            detect_series_type_from_description(
                "Continuing the saga following the one-shot prelude"
            )
            == "standard"
        )

    def test_contextual_collection_signal_beyond_60_chars(self) -> None:
        padding = "A Gotham crossover event. " * 20
        description = (
            f"{padding}This collection includes stories from the following comic books: "
            "Batman #587-590."
        )
        assert detect_series_type_from_description(description) == "volume"

    def test_signal_outside_bounded_window_is_ignored(self) -> None:
        padding = "A" * 2050
        assert (
            detect_series_type_from_description(f"{padding} This omnibus collects Batman.")
            == "standard"
        )

    def test_html_is_normalized_before_classification(self) -> None:
        assert (
            detect_series_type_from_description("This <strong>hardcover</strong> edition collects")
            == "hardcover"
        )

    def test_mixed_hardcover_and_tpb_formats_are_generic_volume(self) -> None:
        description = "Released as both a hardcover edition and a trade paperback collection."
        assert detect_series_type_from_description(description) == "volume"

    @pytest.mark.parametrize(
        "description",
        [
            "Collected Editions: Batman: The Golden Age Omnibus Vol. 1",
            "Collected in Batman: The Golden Age Omnibus Vol. 1.",
            "Stories from Batman Annual #1 and Batman #12.",
            "A preview of the upcoming original graphic novel.",
            "Based on the award-winning graphic novel.",
            "This series includes a one-shot bonus story.",
        ],
    )
    def test_reference_only_collection_language_is_standard(self, description: str) -> None:
        assert detect_series_type_from_description(description) == "standard"

    @pytest.mark.parametrize(
        ("description", "expected"),
        [
            ("Annual issue for the summer season.", "annual"),
            ("A series of annuals starring Batman.", "annual"),
            ("A promotional one-shot for convention attendees.", "one_shot"),
            ("A one-off comic about Gotham City.", "one_shot"),
            ("A holiday special celebrating the season.", "special"),
            ("A special ashcan publication.", "special"),
            ("This omnibus collects the complete run.", "omnibus"),
            ("This compendium includes issues #1-48.", "compendium"),
            ("An original graphic novel by the award-winning team.", "graphic_novel"),
            ("A deluxe hardcover edition with bonus material.", "deluxe"),
            ("A trade paperback collection of the first story arc.", "tpb"),
            ("This hardcover edition collects the first twelve issues.", "hardcover"),
            ("This volume gathers stories from issues #1-6.", "volume"),
        ],
    )
    def test_contextual_identity_roster(self, description: str, expected: str) -> None:
        assert detect_series_type_from_description(description) == expected

    @pytest.mark.parametrize(
        ("description", "expected"),
        [
            (
                "Series of trade paperbacks collecting Immortal Thor and Thor Annual.",
                "tpb",
            ),
            (
                "Series of hardcovers/paperbacks collecting Chip Zdarsky's Batman run.",
                "volume",
            ),
            ("Volume 1: Collects Alien #1-5.", "volume"),
            ("A black and white graphic novel.", "graphic_novel"),
            ("Graphic novella.", "graphic_novel"),
        ],
    )
    def test_provider_collection_phrases(self, description: str, expected: str) -> None:
        assert detect_series_type_from_description(description) == expected

    @pytest.mark.parametrize(
        "description",
        [
            "A special miniseries starring Batman.",
            "A very special story about friendship.",
            "A facsimile edition of Batman #1.",
            "A director's cut with additional pages.",
            "A collection of one-shots published elsewhere.",
            "Following the one-shot prelude, the ongoing series begins.",
        ],
    )
    def test_ambiguous_format_language_is_standard(self, description: str) -> None:
        assert detect_series_type_from_description(description) == "standard"

    def test_empty_description(self) -> None:
        assert detect_series_type_from_description("") == "standard"

    def test_no_keywords(self) -> None:
        assert detect_series_type_from_description("A gritty crime drama set in...") == "standard"


# ── detect_series_type_from_issue_count (Tier 3) ──────────────────


class TestDetectSeriesTypeFromIssueCount:
    """Issue count alone must never define semantic type."""

    def test_one_issue_past_year_is_still_standard(self) -> None:
        assert detect_series_type_from_issue_count(1, 2021, current_year=2026) == "standard"

    def test_one_issue_current_year(self) -> None:
        assert detect_series_type_from_issue_count(1, 2026, current_year=2026) == "standard"

    def test_multiple_issues_past_year(self) -> None:
        assert detect_series_type_from_issue_count(5, 2021, current_year=2026) == "standard"

    def test_zero_issues(self) -> None:
        assert detect_series_type_from_issue_count(0, 2021, current_year=2026) == "standard"

    def test_no_year(self) -> None:
        assert detect_series_type_from_issue_count(1, None, current_year=2026) == "standard"

    def test_one_issue_future_year(self) -> None:
        assert detect_series_type_from_issue_count(1, 2027, current_year=2026) == "standard"


# ── classify_series_type (three-tier cascade) ─────────────────────


class TestClassifySeriesType:
    """Orchestrator: explicit title, contextual description, then standard."""

    def test_tier1_wins_over_tier2(self) -> None:
        """Title keyword takes priority over description."""
        assert (
            classify_series_type(
                "Batman Annual",
                description="A one-shot story",
            )
            == "annual"
        )

    def test_tier2_from_description(self) -> None:
        """Description keyword detected when title is clean."""
        assert (
            classify_series_type(
                "afterdark",
                description="Collects issues #1-6 of the hit series",
            )
            == "volume"
        )

    def test_issue_count_does_not_create_one_shot(self) -> None:
        """A historical singleton without semantic evidence remains standard."""
        assert (
            classify_series_type(
                "afterdark",
                description="A gritty crime drama set in the streets",
                issue_count=1,
                year_start=2021,
            )
            == "standard"
        )

    def test_no_match_standard(self) -> None:
        """Standard series with no signals at any tier."""
        assert (
            classify_series_type(
                "Batman",
                description="The Dark Knight protects Gotham",
                issue_count=50,
                year_start=2016,
            )
            == "standard"
        )

    def test_no_description_singleton_is_standard(self) -> None:
        assert (
            classify_series_type(
                "afterdark",
                description=None,
                issue_count=1,
                year_start=2021,
            )
            == "standard"
        )

    def test_officer_down_uses_late_collection_evidence(self) -> None:
        description = (
            "Officer Down crosses through the Batman family and follows the GCPD response. " * 8
        ) + (
            "This collection includes stories from the following comic books: "
            "Batman #587, Robin #86, Birds of Prey #27, and Detective Comics #754."
        )

        assert (
            classify_series_type(
                "Batman: Officer Down",
                description=description,
                issue_count=1,
                year_start=2001,
            )
            == "volume"
        )

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Batman: The Golden Age Omnibus", "omnibus"),
            ("Batman Epic Collection", "volume"),
            ("Batman Modern Era Epic Collection", "volume"),
            ("Batman Complete Collection", "volume"),
            ("Batman Ultimate Collection", "volume"),
            ("Batman Collected Edition", "volume"),
            ("Batman Complete Series", "volume"),
            ("Batman Library Edition", "volume"),
            ("Crossed: Patient Zero Ashcan", "special"),
            ("Batman Special Edition", "standard"),
        ],
    )
    def test_title_signal_roster(self, title: str, expected: str) -> None:
        assert classify_series_type(title) == expected

    @pytest.mark.parametrize(
        "title",
        [
            "Batman Absolute Edition",
            "Batman Archives",
            "Batman Masterworks",
            "Batman Gallery Edition",
            "Batman Artist's Edition",
            "Batman Treasury Edition",
            "Batman Anniversary Edition",
            "Batman Collector's Edition",
        ],
    )
    def test_conditional_edition_titles_need_description_evidence(self, title: str) -> None:
        assert classify_series_type(title) == "standard"

    def test_conditional_edition_title_uses_collection_description(self) -> None:
        assert (
            classify_series_type(
                "Batman Absolute Edition",
                description="This edition collects Batman #1-12.",
            )
            == "volume"
        )

    def test_title_only_standard(self) -> None:
        assert classify_series_type("Batman") == "standard"

    def test_title_only_annual(self) -> None:
        assert classify_series_type("Batman Annual") == "annual"
