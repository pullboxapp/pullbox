"""Unit tests for the unified release title parser (pullbox.core.release_parser).

Covers all major naming conventions (bracket, parenthesis, dot-separated),
all IssueType values, edge cases, and error conditions.

Run:
    pytest tests/unit/test_release_parser.py -v
    pytest tests/unit/test_release_parser.py -k "bracket" -v
"""

import pytest

from pullbox.core.release_parser import (
    issues_match,
    normalize_issue_number,
    parse_release_title,
)
from pullbox.models.issue import IssueType


class TestParseReleaseTitle:
    """Tests for parse_release_title() across all naming formats."""

    # ── Square Bracket Format (71% of real data) ───────────────────

    def test_basic_bracket_format(self) -> None:
        r = parse_release_title("Batman 016 [2026] [Digital] [Shan-Empire]")
        assert r is not None
        assert r.series_name == "Batman"
        assert r.issue_number == 16.0
        assert r.year == 2026
        assert r.issue_type == IssueType.ISSUE
        assert r.scan_group == "Shan-Empire"

    def test_dc_one_million_issue_number_without_hash_is_exact(self) -> None:
        r = parse_release_title("Wonder Woman 1000000 [1998] [digital-Empire]")

        assert r is not None
        assert r.series_name == "Wonder Woman"
        assert r.issue_number == 1_000_000.0
        assert r.year == 1998

    def test_html_entity_in_name(self) -> None:
        r = parse_release_title(
            "Spider-Man &amp; Wolverine 003 [2025] [4 covers] [Digital] [dekabro-Empire]"
        )
        assert r is not None
        assert "Spider-Man" in r.series_name
        assert "Wolverine" in r.series_name
        assert r.issue_number == 3.0
        assert r.year == 2025

    def test_hyphenated_series_name(self) -> None:
        r = parse_release_title(
            "Batman-Judge Dredd-Judgment on Gotham 001 [1992] [digital] [Marika-Empire]"
        )
        assert r is not None
        assert r.issue_number == 1.0
        assert r.year == 1992
        assert r.issue_type == IssueType.ISSUE

    def test_webrip_scan_group(self) -> None:
        r = parse_release_title("Absolute Batman 016 [2026] [Webrip] [The Last Kryptonian-DCP]")
        assert r is not None
        assert r.series_name == "Absolute Batman"
        assert r.issue_number == 16.0
        assert r.scan_group == "The Last Kryptonian-DCP"

    def test_x_prefix_series(self) -> None:
        r = parse_release_title("Expatriate X-Men 001 [2025] [Digital] [Kileko-Empire]")
        assert r is not None
        assert "X-Men" in r.series_name
        assert r.issue_number == 1.0

    def test_month_year_format(self) -> None:
        r = parse_release_title("Aquaman 019 HD [Feb 1965]")
        assert r is not None
        assert r.series_name == "Aquaman"
        assert r.issue_number == 19.0

    def test_two_digit_issue(self) -> None:
        r = parse_release_title("Flash 03 [2025] [Digital] [Zone-Empire]")
        assert r is not None
        assert r.series_name == "Flash"
        assert r.issue_number == 3.0
        assert r.year == 2025

    def test_four_covers_not_parsed_as_issue(self) -> None:
        r = parse_release_title("Venom 005 [2026] [4 covers] [Digital] [Shan-Empire]")
        assert r is not None
        assert r.issue_number == 5.0  # NOT 4
        assert r.year == 2026

    def test_multiple_bracket_groups(self) -> None:
        r = parse_release_title("Daredevil 021 [2026] [Digital] [Webrip] [The Last Kryptonian-DCP]")
        assert r is not None
        assert r.series_name == "Daredevil"
        assert r.issue_number == 21.0

    def test_long_series_name(self) -> None:
        r = parse_release_title("Teenage Mutant Ninja Turtles 007 [2025] [Digital] [Shan-Empire]")
        assert r is not None
        assert "Teenage Mutant Ninja Turtles" in r.series_name
        assert r.issue_number == 7.0

    # ── Parenthesis Format (10% of real data) ──────────────────────

    def test_paren_format_with_extension(self) -> None:
        r = parse_release_title("Saga 054 (2018) (Digital) (Zone-Empire).cbz")
        assert r is not None
        assert r.series_name == "Saga"
        assert r.issue_number == 54.0
        assert r.year == 2018
        assert r.file_format == "cbz"

    def test_prog_issue_number_with_resolution_metadata(self) -> None:
        r = parse_release_title("2000AD prog 2483 (2026) (4320p) (juvecube).cbz")
        assert r is not None
        assert r.series_name == "2000AD"
        assert r.issue_number == 2483.0
        assert r.year == 2026
        assert r.scan_group == "juvecube"

    def test_prog_issue_number_with_ai_resolution_metadata(self) -> None:
        r = parse_release_title("2000AD prog 2469 (2026) (AI-4320p) (juvecube).cbz")
        assert r is not None
        assert r.series_name == "2000AD"
        assert r.issue_number == 2469.0
        assert r.year == 2026
        assert r.scan_group == "juvecube"

    def test_spaced_2000_ad_prog_preserves_series_year_name(self) -> None:
        r = parse_release_title("2000 AD Prog 2483 (2026) (4320p) (juvecube).cbz")
        assert r is not None
        assert r.series_name == "2000 AD"
        assert r.issue_number == 2483.0
        assert r.year == 2026

    @pytest.mark.parametrize(
        ("title", "series", "issue", "year"),
        [
            ("2000AD 2487 [2026] [Digital-Empire]", "2000AD", 2487, 2026),
            ("2000 AD 2489 (2026) (digital-Empire).cbz", "2000 AD", 2489, 2026),
            ("2000AD.2490.2026.digital.Shan-Empire.cbz", "2000AD", 2490, 2026),
            ("2000AD 2487", "2000AD", 2487, None),
            ("Action Comics 1000 [2018] [Digital]", "Action Comics", 1000, 2018),
            ("Detective Comics 1027 (2020) (Digital).cbr", "Detective Comics", 1027, 2020),
            ("Weekly Anthology 1899 [2026]", "Weekly Anthology", 1899, 2026),
            ("2000AD 2001 [2016]", "2000AD", 2001, 2016),
            ("Weekly Anthology 2100 [2026]", "Weekly Anthology", 2100, 2026),
            ("Weekly Anthology 9999 [2026]", "Weekly Anthology", 9999, 2026),
            ("District 13 2487 [2026]", "District 13", 2487, 2026),
        ],
    )
    def test_bare_four_digit_issue_number(self, title, series, issue, year) -> None:
        parsed = parse_release_title(title, expected_series=(series,))
        assert parsed is not None
        assert parsed.series_name == series
        assert parsed.issue_number == issue
        assert parsed.issue_number_text == str(issue)
        assert parsed.year == year
        assert not parsed.is_pack

    @pytest.mark.parametrize(
        ("title", "series", "issue", "year"),
        [
            ("Spider-Man 2099 [2026]", "Spider-Man 2099", None, 2026),
            ("Spider-Man 2099 1 [2026]", "Spider-Man 2099", 1, 2026),
            ("Marvel 1602 1 [2003]", "Marvel 1602", 1, 2003),
            ("Marvel 1602 [2003]", "Marvel 1602", None, 2003),
            ("Batman 3000 [2026]", "Batman 3000", None, 2026),
            ("2000 AD 005 [2026]", "2000 AD", 5, 2026),
            ("2000AD [2026] [Digital-Empire]", "2000AD", None, 2026),
            ("Weekly Anthology 2026", "Weekly Anthology", None, 2026),
            ("Weekly Anthology 2026 [2025]", "Weekly Anthology", None, 2025),
            ("2000AD 2487-2490 [2026]", "2000AD 2487-2490", None, 2026),
            ("2000AD 2487 - 2490 [2026]", "2000AD 2487 - 2490", None, 2026),
            ("2000AD 2487 of 2490 [2026]", "2000AD 2487 of 2490", None, 2026),
            ("2000AD 12345 [2026]", "2000AD 12345", None, 2026),
        ],
    )
    def test_four_digit_issue_heuristic_preserves_ambiguous_tokens(
        self, title, series, issue, year
    ) -> None:
        parsed = parse_release_title(title)
        assert parsed is not None
        assert parsed.series_name == series
        assert parsed.issue_number == issue
        assert parsed.year == year

    @pytest.mark.parametrize("series", ["Marvel 1602", "Batman 3000", "Spider-Man 2099"])
    def test_known_numeric_title_does_not_become_an_issue(self, series) -> None:
        parsed = parse_release_title(f"{series} [2026]", expected_series=(series,))
        assert parsed is not None
        assert parsed.series_name == series
        assert parsed.issue_number is None

    def test_explicit_volume_year_is_distinct_from_publication_year(self) -> None:
        parsed = parse_release_title(
            "2000AD v1977 2487 [2026] [Digital-Empire]", expected_series=("2000 AD",)
        )
        assert parsed is not None
        assert parsed.series_name == "2000AD"
        assert parsed.issue_number == 2487
        assert parsed.issue_type is IssueType.ISSUE
        assert parsed.year == 2026
        assert parsed.volume_year == 1977

    def test_annual_issue_type_paren(self) -> None:
        r = parse_release_title("Batman Annual 003 (2018) (digital) (Son of Ultron-Empire).cbr")
        assert r is not None
        assert r.series_name == "Batman"
        assert r.issue_number == 3.0
        assert r.issue_type == IssueType.ANNUAL

    def test_volume_prefix(self) -> None:
        r = parse_release_title("The Amazing Spider-Man v2 015 (2000)")
        assert r is not None
        assert "Amazing Spider-Man" in r.series_name
        assert r.issue_number == 15.0
        assert r.volume == "v2"
        assert r.year == 2000

    def test_limited_series(self) -> None:
        r = parse_release_title("Spider-Island 02 (of 05) (2015) (digital) (Minutemen-Spaztastic)")
        assert r is not None
        assert r.series_name == "Spider-Island"
        assert r.issue_number == 2.0
        assert r.year == 2015

    def test_inline_limited_series_marker(self) -> None:
        r = parse_release_title(
            "Invincible Presents Atom Eve &amp; Rex Splode 02 of 03 (2009) "
            "(Minutemen DTs&amp;MustacheGuy)"
        )
        assert r is not None
        assert r.issue_number == 2.0
        assert r.year == 2009
        assert r.series_name is not None
        assert r.series_name.startswith("Invincible Presents Atom Eve & Rex Splode")

    def test_single_token_paren_scan_group_after_digital_metadata(self) -> None:
        r = parse_release_title("Necronomicon 04 (of 04) (2008) (Digital) (TanCombs).cbr")
        assert r is not None
        assert r.series_name == "Necronomicon"
        assert r.issue_number == 4.0
        assert r.year == 2008
        assert r.scan_group == "TanCombs"

    def test_paren_hash_format(self) -> None:
        r = parse_release_title("Batman (2016) #045 (Digital) (Zone-Empire).cbz")
        assert r is not None
        assert r.series_name == "Batman"
        assert r.issue_number == 45.0
        assert r.year == 2016

    def test_digital_rip_metadata_is_fully_stripped(self) -> None:
        r = parse_release_title("Adventure Time 013 (2026) (Digital Rip) (Hourman-DCP).cbr")
        assert r is not None
        assert r.series_name == "Adventure Time"
        assert r.issue_number == 13.0
        assert r.year == 2026
        assert r.issue_type == IssueType.ISSUE
        assert r.scan_group == "Hourman-DCP"

    @pytest.mark.parametrize(
        ("title", "expected_series"),
        [
            (
                "Batman 145 (2024) (Webrip) (The Last Kryptonian-DCP).cbr",
                "Batman",
            ),
            (
                "SHAZAM! 009 (2024) (webrip) (The Last Kryptonian-DCP).cbr",
                "SHAZAM!",
            ),
        ],
    )
    def test_parenthesized_webrip_metadata_is_fully_stripped(
        self,
        title: str,
        expected_series: str,
    ) -> None:
        r = parse_release_title(title)
        assert r is not None
        assert r.series_name == expected_series
        assert r.year == 2024
        assert r.scan_group == "The Last Kryptonian-DCP"

    def test_parenthesized_known_publisher_metadata_is_stripped(self) -> None:
        r = parse_release_title(
            "Coraline (Harper Collins) (2008) (Digital) (Son of Ultron-Empire).cbr"
        )

        assert r is not None
        assert r.series_name == "Coraline"
        assert r.year == 2008
        assert r.scan_group == "Son of Ultron-Empire"

    def test_parenthesized_known_publisher_plus_year_metadata_is_stripped(self) -> None:
        r = parse_release_title("Negation 02 (CrossGen 2003).cbz")

        assert r is not None
        assert r.series_name == "Negation"
        assert r.issue_number == 2.0
        assert r.year == 2003

    def test_arbitrary_parenthetical_title_text_is_preserved(self) -> None:
        r = parse_release_title("Example Series (The Lost Chapter) 001 (2026) (Digital).cbz")

        assert r is not None
        assert r.series_name == "Example Series (The Lost Chapter)"
        assert r.issue_number == 1.0
        assert r.year == 2026

    def test_scanned_physical_copy_and_proper_are_release_metadata(self) -> None:
        r = parse_release_title(
            "Superman Spider-Man 01 (2026) (Scanned Physical Copy) - PROPER.cbr"
        )

        assert r is not None
        assert r.series_name == "Superman Spider-Man"
        assert r.issue_number == 1.0
        assert r.year == 2026
        assert r.file_format == "cbr"

    def test_os_marker_is_one_shot_metadata_not_series_title(self) -> None:
        r = parse_release_title(
            "Murder Drones - Home 001 (OS) (2026) (Digital Rip) (Hourman-DCP).cbr"
        )
        assert r is not None
        assert r.series_name == "Murder Drones - Home"
        assert r.issue_number == 1.0
        assert r.year == 2026
        assert r.issue_type == IssueType.ONE_SHOT
        assert r.scan_group == "Hourman-DCP"

    # ── Dot-Separated Format (12% of real data) ───────────────────

    def test_dot_separated(self) -> None:
        r = parse_release_title("Harley.Quinn.051.2025.digital.Pyrate-DCP")
        assert r is not None
        assert r.series_name == "Harley Quinn"
        assert r.issue_number == 51.0
        assert r.year == 2025
        assert r.scan_group == "Pyrate-DCP"

    def test_scene_style_numbered_pdf_strips_narrow_release_group_prefix(self) -> None:
        r = parse_release_title("bb-Sacrificers.No.7.pdf")

        assert r is not None
        assert r.series_name == "Sacrificers"
        assert r.issue_number == 7.0
        assert r.scan_group == "bb"
        assert r.file_format == "pdf"

    def test_dot_separated_with_publisher(self) -> None:
        r = parse_release_title(
            "Dark.Horse-The.World.Of.Black.Hammer.Omnibus.Vol.05.2025.Retail.Comic"
        )
        assert r is not None
        assert r.issue_type == IssueType.OMNIBUS

    def test_dot_separated_comic_heroes_issue_suffix(self) -> None:
        r = parse_release_title("Comic.Heroes-Issue.29.2016.cbz")
        assert r is not None
        assert r.series_name == "Comic Heroes"
        assert r.issue_number == 29.0
        assert r.year == 2016

    def test_dot_separated_format_comic_suffix_still_stripped(self) -> None:
        r = parse_release_title("Batman.050.2024.HYBRiD.COMIC.eBook-GRiM.cbz")
        assert r is not None
        assert r.series_name == "Batman"
        assert r.issue_number == 50.0
        assert r.year == 2024

    def test_minimal_underscore_issue_filename(self) -> None:
        r = parse_release_title("dc_connect_72.pdf")
        assert r is not None
        assert r.series_name == "dc connect"
        assert r.issue_number == 72.0
        assert r.file_format == "pdf"

    def test_minimal_dot_issue_filename(self) -> None:
        r = parse_release_title("DC.Connect.072.pdf")
        assert r is not None
        assert r.series_name == "DC Connect"
        assert r.issue_number == 72.0
        assert r.file_format == "pdf"

    def test_generic_separator_scan_filename_does_not_invent_issue_number(self) -> None:
        r = parse_release_title("scan_0042.cbz")
        assert r is not None
        assert r.series_name == "scan_0042"
        assert r.issue_number is None

    def test_minimal_separator_numeric_title_does_not_invent_issue_number(self) -> None:
        r = parse_release_title("Marvel_1602.pdf")
        assert r is not None
        assert r.series_name == "Marvel_1602"
        assert r.issue_number is None

    def test_dotted_title_year_without_issue_number(self) -> None:
        r = parse_release_title("Friendo.2019.pdf")
        assert r is not None
        assert r.series_name == "Friendo"
        assert r.issue_number is None
        assert r.year == 2019

    def test_dotted_future_year_style_series_name_is_not_publication_year(self) -> None:
        r = parse_release_title("Spider-Man.2099.pdf")
        assert r is not None
        assert r.series_name == "Spider-Man.2099"
        assert r.year is None

    # ── Non-Issue Types (all 9 non-ISSUE values) ──────────────────

    def test_tpb_detection(self) -> None:
        r = parse_release_title("Zatanna & The Ripper v03 TPB [2023] [Pyrate-DCP]")
        assert r is not None
        assert r.issue_type == IssueType.TPB

    def test_annual_bracket_format(self) -> None:
        r = parse_release_title("Absolute Batman 2025 Annual 001 [2025] [Digital] [Shan-Empire]")
        assert r is not None
        assert r.issue_type == IssueType.ANNUAL
        assert r.series_name == "Absolute Batman"

    def test_one_shot_takes_precedence(self) -> None:
        r = parse_release_title(
            "Vampirella Helliday 2025 Special [2025] [One Shot] "
            "[Dynamite Entertainment] [Digital-HD] [LeDuch]"
        )
        assert r is not None
        assert r.issue_type == IssueType.ONE_SHOT  # One Shot > Special
        assert r.scan_group == "LeDuch"

    def test_gn_detection(self) -> None:
        r = parse_release_title("Big Box Apocalypse GN (2014) (Digital) (DR & Quinch-Empire)")
        assert r is not None
        assert r.issue_type == IssueType.GN

    def test_ogn_detection(self) -> None:
        r = parse_release_title("New Teen Titans - Games - Original Graphic Novel [2011]")
        assert r is not None
        assert r.issue_type == IssueType.OGN

    def test_omnibus_detection(self) -> None:
        r = parse_release_title("Gantz.Omnibus.Vol.11")
        assert r is not None
        assert r.issue_type == IssueType.OMNIBUS

    def test_special_detection(self) -> None:
        r = parse_release_title("DC K.O. Special 001 [2025] [Digital] [Zone-Empire]")
        assert r is not None
        assert r.issue_type == IssueType.SPECIAL

    def test_hc_detection(self) -> None:
        r = parse_release_title("Cairo Hardcover (2007) (Digital)")
        assert r is not None
        assert r.issue_type == IssueType.HC

    def test_deluxe_detection(self) -> None:
        r = parse_release_title("Batman Year Three Deluxe Edition [2025]")
        assert r is not None
        assert r.issue_type == IssueType.DELUXE

    def test_compendium_detection(self) -> None:
        r = parse_release_title("Rising Stars Compendium [2015] [Digital]")
        assert r is not None
        assert r.issue_type == IssueType.COMPENDIUM

    # ── Volume Type Detection ─────────────────────────────────────

    @pytest.mark.parametrize(
        "title,expected_type,expected_series",
        [
            ("Batman Vol.1 (2016).cbz", IssueType.VOLUME, "Batman"),
            ("Batman Volume 2 (2016).cbz", IssueType.VOLUME, "Batman"),
            ("Batman Vol 3 (2016).cbz", IssueType.VOLUME, "Batman"),
            ("Batman Vol.1:Court of Owls (2016).cbz", IssueType.VOLUME, "Batman"),
            ("Batman Vol 1 TPB (2016).cbz", IssueType.TPB, None),  # TPB wins
            ("Batman v01 [2025]", IssueType.VOLUME, "Batman"),  # v-prefix reclassified
        ],
    )
    def test_volume_type_detection(
        self, title: str, expected_type: IssueType, expected_series: str | None
    ) -> None:
        r = parse_release_title(title)
        assert r is not None
        assert r.issue_type == expected_type
        if expected_series is not None:
            assert r.series_name == expected_series

    def test_volume_prefix_with_issue_stays_issue(self) -> None:
        """v2 015 has both volume AND issue — should stay ISSUE, not VOLUME."""
        r = parse_release_title("The Amazing Spider-Man v2 015 (2000)")
        assert r is not None
        assert r.issue_type == IssueType.ISSUE
        assert r.issue_number == 15.0
        assert r.volume == "v2"

    # ── Edge Cases ─────────────────────────────────────────────────

    def test_season_decimal_not_issue(self) -> None:
        r = parse_release_title("Arrow - Season 2.5 009 (2014) (Digital) (Pirate-Empire)")
        assert r is not None
        assert r.issue_number == 9.0
        assert "Arrow" in r.series_name

    def test_numeric_series_title_keeps_zero_padded_issue_at_tail(self) -> None:
        r = parse_release_title("Powers 25 009 (2026) (digital) (Son of Ultron-Empire).cbr")
        assert r is not None
        assert r.series_name == "Powers 25"
        assert r.issue_number == 9.0
        assert r.year == 2026

    def test_numeric_series_title_with_hash_still_parses_tail_issue(self) -> None:
        r = parse_release_title("Batman 66 #001 (2013) (Digital).cbz")
        assert r is not None
        assert r.series_name == "Batman 66"
        assert r.issue_number == 1.0
        assert r.year == 2013

    def test_no_space_before_parens(self) -> None:
        r = parse_release_title("D4VE2 001(2015)(2 covers)(c2c)(Digi-Hybrid)(TLK-EMPIRE-HD)")
        assert r is not None
        assert r.series_name == "D4VE2"
        assert r.issue_number == 1.0
        assert r.year == 2015

    def test_fills_prefix_stripped(self) -> None:
        r = parse_release_title("Fills [01/01] Black of Heart 001 (2020) (Digital-Empire)")
        assert r is not None
        assert r.series_name == "Black of Heart"
        assert r.issue_number == 1.0

    def test_year_not_confused_with_issue(self) -> None:
        r = parse_release_title("Batman 005 [2026] [Digital]")
        assert r is not None
        assert r.issue_number == 5.0
        assert r.year == 2026

    # ── Legacy Filename Format (backward compat) ──────────────────

    def test_legacy_hash_year_format(self) -> None:
        r = parse_release_title("Batman (2016) #045 (Digital) (Zone-Empire).cbz")
        assert r is not None
        assert r.series_name == "Batman"
        assert r.issue_number == 45.0
        assert r.year == 2016

    def test_legacy_minimal(self) -> None:
        r = parse_release_title("Batman 045.cbz")
        assert r is not None
        assert r.series_name == "Batman"
        assert r.issue_number == 45.0

    def test_legacy_hash_only(self) -> None:
        r = parse_release_title("Batman #45.cbz")
        assert r is not None
        assert r.series_name == "Batman"
        assert r.issue_number == 45.0

    # ── Error Cases ────────────────────────────────────────────────

    def test_empty_string_returns_none(self) -> None:
        assert parse_release_title("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert parse_release_title("   ") is None

    def test_extension_only_returns_none(self) -> None:
        assert parse_release_title(".cbz") is None

    def test_unparseable_returns_default_type(self) -> None:
        r = parse_release_title("???")
        assert r is not None
        assert r.issue_type == IssueType.ISSUE  # Default, not UNKNOWN


class TestParseReleaseParametrized:
    """Parametrized tests covering the core parsing matrix."""

    @pytest.mark.parametrize(
        "title,expected_series,expected_issue,expected_type",
        [
            (
                "Batman 016 [2026] [Digital] [Shan-Empire]",
                "Batman",
                16.0,
                IssueType.ISSUE,
            ),
            (
                "Saga 054 (2018) (Digital) (Zone-Empire).cbz",
                "Saga",
                54.0,
                IssueType.ISSUE,
            ),
            (
                "Batman Annual 003 (2018)",
                "Batman",
                3.0,
                IssueType.ANNUAL,
            ),
            (
                "Zatanna & The Ripper v03 TPB [2023]",
                "Zatanna",
                None,
                IssueType.TPB,
            ),
            (
                "Gantz.Omnibus.Vol.11",
                "Gantz",
                None,
                IssueType.OMNIBUS,
            ),
            (
                "Ignite Prime [One Shot] [2026]",
                "Ignite Prime",
                None,
                IssueType.ONE_SHOT,
            ),
            (
                "Batman 005 [2024] [Digital]",
                "Batman",
                5.0,
                IssueType.ISSUE,
            ),
        ],
    )
    def test_parser_parametrized(
        self,
        title: str,
        expected_series: str | None,
        expected_issue: float | None,
        expected_type: IssueType,
    ) -> None:
        r = parse_release_title(title)
        assert r is not None
        if expected_series:
            assert expected_series in (r.series_name or "")
        if expected_issue is not None:
            assert r.issue_number == expected_issue
        assert r.issue_type == expected_type


class TestNormalizeIssueNumber:
    """Tests for normalize_issue_number()."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("5", 5.0),
            ("05", 5.0),
            ("005", 5.0),
            ("#5", 5.0),
            ("5.1", 5.1),
            ("04 (of 04)", 4.0),
            ("04 [of 04]", 4.0),
            ("4 of 4", 4.0),
            ("5a", 5.0),
            ("12AU", 12.0),
            ("½", 0.5),
            ("1/2", 0.5),
            ("0", 0.0),
            (None, None),
            ("", None),
            (5.0, 5.0),
            (5, 5.0),
        ],
    )
    def test_normalize(self, raw: str | float | int | None, expected: float | None) -> None:
        assert normalize_issue_number(raw) == expected

    def test_unicode_quarter(self) -> None:
        assert normalize_issue_number("¼") == 0.25


class TestIssuesMatch:
    """Tests for issues_match()."""

    def test_exact_match(self) -> None:
        assert issues_match(5.0, 5.0) is True

    @pytest.mark.regression
    def test_five_is_not_fifty(self) -> None:
        """THE critical bug: Batman #5 must NOT match Batman #50."""
        assert issues_match(5.0, 50.0) is False

    @pytest.mark.regression
    def test_five_is_not_fifteen(self) -> None:
        """Another critical case: #5 must NOT match #15."""
        assert issues_match(5.0, 15.0) is False

    def test_none_found_no_match(self) -> None:
        assert issues_match(5.0, None) is False

    def test_float_tolerance(self) -> None:
        assert issues_match(5.0, 5.0009) is True

    def test_different_floats_no_match(self) -> None:
        assert issues_match(5.0, 5.5) is False

    def test_zero_matches_zero(self) -> None:
        assert issues_match(0.0, 0.0) is True

    def test_decimal_issue_match(self) -> None:
        assert issues_match(5.1, 5.1) is True

    def test_decimal_issue_no_match(self) -> None:
        assert issues_match(5.1, 5.2) is False
