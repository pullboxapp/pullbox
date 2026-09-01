"""Unit tests for the series name matching engine (pullbox.core.name_matcher).

Covers normalization pipeline, tiered matching strategy (exact, alternate,
token-set, fuzzy), and edge cases.

Run:
    pytest tests/unit/test_name_matcher.py -v
    pytest tests/unit/test_name_matcher.py -k "normalize" -v
"""

import pytest

from pullbox.core.name_matcher import NameMatcher, NameMatchResult


class TestNormalize:
    """Tests for NameMatcher.normalize() pipeline."""

    def test_strip_leading_the(self) -> None:
        assert NameMatcher.normalize("The Amazing Spider-Man") == "amazing spider man"

    def test_strip_leading_a(self) -> None:
        assert NameMatcher.normalize("A Walk Through Hell") == "walk through hell"

    def test_strip_leading_an(self) -> None:
        assert NameMatcher.normalize("An Unkindness of Ravens") == "unkindness of ravens"

    def test_html_entity_decode(self) -> None:
        assert NameMatcher.normalize("Spider-Man &amp; Wolverine") == NameMatcher.normalize(
            "Spider-Man & Wolverine"
        )

    def test_unicode_dash_normalized(self) -> None:
        assert NameMatcher.normalize("Batman\u2014Dark Knight") == NameMatcher.normalize(
            "Batman-Dark Knight"
        )

    def test_en_dash_normalized(self) -> None:
        assert NameMatcher.normalize("Batman\u2013Dark Knight") == NameMatcher.normalize(
            "Batman-Dark Knight"
        )

    def test_smart_quotes_normalized(self) -> None:
        assert NameMatcher.normalize("World\u2019s Finest") == NameMatcher.normalize(
            "World's Finest"
        )

    def test_punctuation_removed(self) -> None:
        n = NameMatcher.normalize("Batman/Superman: World's Finest")
        assert "/" not in n
        assert ":" not in n
        assert "'" not in n

    def test_ampersand_and_word_equivalent(self) -> None:
        assert NameMatcher.normalize("A & B") == NameMatcher.normalize("A and B")

    def test_bare_ampersand_normalized(self) -> None:
        assert NameMatcher.normalize("Q&A") == NameMatcher.normalize("Q and A")

    def test_collapse_whitespace(self) -> None:
        assert "  " not in NameMatcher.normalize("Batman    Returns")

    def test_diacritics_stripped(self) -> None:
        n = NameMatcher.normalize("Naïve New Beasts")
        assert n == "naive new beasts"

    def test_french_diacritics_do_not_split_provider_query_tokens(self) -> None:
        assert (
            NameMatcher.normalize("Remède Impérial - L'Étrange Médecin de la Cour")
            == "remede imperial l etrange medecin de la cour"
        )

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Seishun♂Sobbat", "seishun male sobbat"),
            ("Girl♀Power", "girl female power"),
            ("Clamp ×××HOLiC", "clamp xxxholic"),  # noqa: RUF001
            ("Yū☆Yū☆Hakusho", "yu yu hakusho"),
            ("C++ Stories @ 100%", "c plus plus stories at 100 percent"),
            ("Issue № 1 + 2 = 3", "issue number 1 plus 2 equals 3"),
        ],
    )
    def test_common_title_symbols_have_stable_semantics(
        self,
        raw: str,
        expected: str,
    ) -> None:
        assert NameMatcher.normalize(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Smørrebrød Straße Łódź", "smorrebrod strasse lodz"),
            ("Æsir Œuvre Đorđe", "aesir oeuvre dorde"),
            ("Þór Iðunn Kayıp Əli", "thor idunn kayip ali"),  # noqa: RUF001
        ],
    )
    def test_common_latin_letters_without_nfkd_decompositions_are_transliterated(
        self,
        raw: str,
        expected: str,
    ) -> None:
        assert NameMatcher.normalize(raw) == expected

    def test_lowercase(self) -> None:
        assert NameMatcher.normalize("BATMAN") == "batman"

    def test_empty_string(self) -> None:
        assert NameMatcher.normalize("") == ""

    def test_whitespace_only(self) -> None:
        assert NameMatcher.normalize("   ") == ""


class TestMatch:
    """Tests for NameMatcher.match() tiered matching."""

    def setup_method(self) -> None:
        self.matcher = NameMatcher()

    def test_exact_after_normalization(self) -> None:
        r = self.matcher.match("The Amazing Spider-Man", "Amazing Spider-Man")
        assert r.is_match is True
        assert r.similarity == 1.0
        assert r.match_type == "exact"

    def test_punctuation_variants_match(self) -> None:
        r = self.matcher.match(
            "Batman/Superman: World's Finest",
            "Batman Superman Worlds Finest",
        )
        assert r.is_match is True
        assert r.similarity == 1.0

    def test_compact_spacing_variant_is_exact(self) -> None:
        r = self.matcher.match("2000AD", "2000 AD")
        assert r.is_match is True
        assert r.similarity == 1.0
        assert r.match_type == "exact"

    def test_hyphen_slash_equivalent(self) -> None:
        r = self.matcher.match("Red Hood-Arsenal", "Red Hood/Arsenal")
        assert r.is_match is True

    def test_ampersand_and_equivalent(self) -> None:
        r = self.matcher.match("Spider-Man &amp; Wolverine", "Spider-Man and Wolverine")
        assert r.is_match is True

    def test_alternate_name_match(self) -> None:
        r = self.matcher.match("TMNT", "Teenage Mutant Ninja Turtles", alternate_names=["TMNT"])
        assert r.is_match is True
        assert r.match_type == "alternate"

    def test_alternate_name_normalized(self) -> None:
        r = self.matcher.match("tmnt", "Teenage Mutant Ninja Turtles", alternate_names=["TMNT"])
        assert r.is_match is True

    def test_alternate_matched_against_value(self) -> None:
        r = self.matcher.match("TMNT", "Teenage Mutant Ninja Turtles", alternate_names=["TMNT"])
        assert r.matched_against == "TMNT"

    def test_token_set_reorder(self) -> None:
        r = self.matcher.match("Spider-Man Amazing The", "The Amazing Spider-Man")
        assert r.is_match is True
        assert r.match_type == "token_set"

    def test_token_set_similarity(self) -> None:
        r = self.matcher.match("Spider-Man Amazing The", "The Amazing Spider-Man")
        assert r.similarity == 0.90

    def test_fuzzy_typo(self) -> None:
        r = self.matcher.match("Daredevil", "Daredevl")
        assert r.is_match is True
        assert r.match_type == "fuzzy"
        assert r.similarity >= 0.70

    def test_different_series_no_match(self) -> None:
        r = self.matcher.match("Batman", "Superman")
        assert r.is_match is False

    def test_similar_but_different_series(self) -> None:
        r = self.matcher.match("Spider-Man", "Spider-Man 2099")
        if r.is_match:
            assert r.similarity < 0.85

    def test_completely_different(self) -> None:
        r = self.matcher.match("Saga", "Walking Dead")
        assert r.is_match is False

    def test_empty_string_no_match(self) -> None:
        r = self.matcher.match("", "Batman")
        assert r.is_match is False

    def test_subset_name_caution(self) -> None:
        r = self.matcher.match("X-Men", "Uncanny X-Men")
        if r.is_match:
            assert r.similarity < 0.85

    def test_exact_match_reports_target(self) -> None:
        r = self.matcher.match("Batman", "Batman")
        assert r.matched_against == "Batman"

    def test_no_match_empty_matched_against(self) -> None:
        r = self.matcher.match("Saga", "Walking Dead")
        assert r.matched_against == ""
        assert r.match_type == "none"

    def test_x_men_hyphen_vs_space(self) -> None:
        r = self.matcher.match("X-Men", "X Men")
        assert r.is_match is True
        assert r.similarity == 1.0

    def test_no_alternate_names(self) -> None:
        r = self.matcher.match("Batman", "Batman", alternate_names=None)
        assert r.is_match is True

    def test_empty_alternate_list(self) -> None:
        r = self.matcher.match("Batman", "Batman", alternate_names=[])
        assert r.is_match is True

    def test_result_is_frozen(self) -> None:
        r = NameMatchResult(is_match=True, similarity=1.0, match_type="exact", matched_against="X")
        with pytest.raises(AttributeError):
            r.is_match = False  # type: ignore[misc]
