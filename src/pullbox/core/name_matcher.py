"""Series name matching engine with dynamic normalization.

Extracted from matching_service.py's normalize_series_name() and
series_similarity(), enhanced with Mylar3-inspired dynamic normalization
(HTML entity decode, unicode normalization, smart quote/dash handling,
& ↔ "and" replacement) and tiered matching strategy.

Used by both MatchingService (disk files) and ReleaseValidator (search
results) to ensure consistent matching behavior across both modalities.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

# Pre-compiled patterns
_POSSESSIVE_RE = re.compile(r"'s\b")
_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")
_ARTICLES = frozenset({"the", "a", "an"})
_PUBLICATION_FORM_SUFFIXES = frozenset({"magazine"})
_TITLE_COMPAT_TRANSLATION = str.maketrans(
    {
        "Ø": "O",
        "ø": "o",
        "ẞ": "SS",
        "ß": "ss",
        "Ł": "L",
        "ł": "l",
        "Æ": "AE",
        "æ": "ae",
        "Œ": "OE",
        "œ": "oe",
        "Đ": "D",
        "đ": "d",
        "Ð": "D",
        "ð": "d",
        "Þ": "Th",
        "þ": "th",
        "ı": "i",  # noqa: RUF001 - intentional dotless i
        "Ə": "A",
        "ə": "a",
        "Ǝ": "E",
        "ǝ": "e",
        "♂": " male ",
        "♀": " female ",
        "×": "x",  # noqa: RUF001 - intentional Unicode multiplication sign
        "✕": "x",
        "✖": "x",
        "+": " plus ",
        "@": " at ",
        "%": " percent ",
        "№": " number ",
        "#": " number ",
        "=": " equals ",
        "$": " dollar ",
        "€": " euro ",
        "£": " pound ",
        "¥": " yen ",
        "∞": " infinity ",
        "☆": " ",
        "★": " ",
        "●": " ",
        "○": " ",
        "•": " ",
        "・": " ",
        "·": " ",
        "®": "",
        "©": "",
        "™": "",
    }
)


def _compact_normalized(name: str) -> str:
    """Collapse spacing after normal normalization for stylized compact titles."""
    return name.replace(" ", "")


@dataclass(frozen=True)
class NameMatchResult:
    """Result of comparing a parsed name against a target series."""

    is_match: bool
    similarity: float  # 0.0-1.0
    match_type: str  # "exact", "alternate", "token_set", "fuzzy", "none"
    matched_against: str  # Which name matched (target or which alternate)


class NameMatcher:
    """Flexible series name matching with dynamic normalization.

    This is the SINGLE matching engine for all of Pullbox — used by both
    MatchingService (disk files) and ReleaseValidator (search results).
    """

    @staticmethod
    def normalize(name: str) -> str:
        """Dynamic name normalization.

        Replaces the old normalize_series_name() in matching_service.py.
        Backward-compatible — produces the same results for the basic cases
        the old function handled, plus handles NZB-specific patterns like
        HTML entities and unicode.
        """
        if not name:
            return ""

        # Step 1: HTML entity decode (&amp; → &)
        s = html.unescape(name)

        # Preserve semantic title symbols before compatibility normalization
        # can rewrite them (for example, ``№`` becomes ``No`` under NFKD).
        s = s.translate(_TITLE_COMPAT_TRANSLATION)

        # Step 2: Unicode normalization (NFKD) — decomposes accented chars
        s = unicodedata.normalize("NFKD", s)
        # Remove decomposed combining marks before punctuation normalization.
        # Otherwise each accent becomes a space and splits provider-query words
        # (for example, ``Remède`` became ``reme de``).
        s = "".join(char for char in s if not unicodedata.combining(char))
        # NFKD converts full-width operators to ASCII; give those the same
        # stable semantics as their native-width forms.
        s = s.translate(_TITLE_COMPAT_TRANSLATION)

        # Step 3: Replace unicode dashes with ASCII hyphen
        s = s.replace("\u2014", "-").replace("\u2013", "-").replace("\u2012", "-")

        # Step 4: Replace smart quotes/apostrophes with ASCII
        s = s.replace("\u2018", "'").replace("\u2019", "'")
        s = s.replace("\u201c", '"').replace("\u201d", '"')

        # Step 5: Lowercase
        s = s.lower()

        # Step 6: Strip leading articles
        for article in ("the ", "a ", "an "):
            if s.startswith(article):
                s = s[len(article) :]
                break

        # Step 7: Normalize & ↔ "and"
        s = s.replace(" & ", " and ").replace("&", " and ")

        # Step 8a: Collapse possessives ('s → s) before stripping punctuation
        s = _POSSESSIVE_RE.sub("s", s)

        # Step 8b: Remove punctuation (keep only alphanumeric and spaces)
        s = _PUNCTUATION_RE.sub(" ", s)

        # Step 9: Collapse whitespace
        s = _WHITESPACE_RE.sub(" ", s)

        # Step 10: Strip edges
        return s.strip()

    # Default thresholds (stored as 0-100 in config, 0.0-1.0 internally)
    DEFAULT_FUZZY_HIGH = 0.85
    DEFAULT_FUZZY_LOW = 0.70

    def match(
        self,
        parsed_name: str,
        target_name: str,
        alternate_names: list[str] | None = None,
        *,
        fuzzy_high_threshold: float | None = None,
        fuzzy_low_threshold: float | None = None,
    ) -> NameMatchResult:
        """Compare a parsed release name against a target series name.

        Tiered matching: exact → alternate → token-set → fuzzy.

        Args:
            fuzzy_high_threshold: High-confidence fuzzy cutoff (0.0-1.0).
                Defaults to 0.85.
            fuzzy_low_threshold: Low-confidence fuzzy cutoff (0.0-1.0).
                Defaults to 0.70.
        """
        high = fuzzy_high_threshold if fuzzy_high_threshold is not None else self.DEFAULT_FUZZY_HIGH
        low = fuzzy_low_threshold if fuzzy_low_threshold is not None else self.DEFAULT_FUZZY_LOW

        norm_parsed = self.normalize(parsed_name)
        norm_target = self.normalize(target_name)

        # Handle empty inputs
        if not norm_parsed or not norm_target:
            return NameMatchResult(
                is_match=False, similarity=0.0, match_type="none", matched_against=""
            )

        # Tier 1: Exact normalized match
        if norm_parsed == norm_target:
            return NameMatchResult(
                is_match=True, similarity=1.0, match_type="exact", matched_against=target_name
            )
        if _compact_normalized(norm_parsed) == _compact_normalized(norm_target):
            return NameMatchResult(
                is_match=True, similarity=1.0, match_type="exact", matched_against=target_name
            )

        # Tier 2: Alternate name exact match
        for alt in alternate_names or []:
            norm_alt = self.normalize(alt)
            if norm_parsed == norm_alt:
                return NameMatchResult(
                    is_match=True, similarity=1.0, match_type="alternate", matched_against=alt
                )
            if _compact_normalized(norm_parsed) == _compact_normalized(norm_alt):
                return NameMatchResult(
                    is_match=True, similarity=1.0, match_type="alternate", matched_against=alt
                )

        # Tier 3: Token-set match (same words, different order)
        # Strip articles from both sets so "The" position doesn't matter
        parsed_tokens = set(norm_parsed.split()) - _ARTICLES
        target_tokens = set(norm_target.split()) - _ARTICLES
        if parsed_tokens == target_tokens and len(parsed_tokens) > 0:
            return NameMatchResult(
                is_match=True,
                similarity=0.90,
                match_type="token_set",
                matched_against=target_name,
            )

        # Tier 3½: Starts-with match — the parsed name begins with the target
        # (or vice versa).  Common for releases with subtitles appended, e.g.
        # "Immortal Thor - All Weather Turns To Storm" vs "Immortal Thor".
        # Only match if the extra portion looks like a subtitle (2+ extra
        # words), not a numeric variant like "Spider-Man 2099" vs "Spider-Man".
        starts_with_match = self._check_starts_with(norm_parsed, norm_target)
        if starts_with_match:
            return NameMatchResult(
                is_match=True,
                similarity=0.95,
                match_type="starts_with",
                matched_against=target_name,
            )

        # Tier 3¾: Target tokens are a subset of parsed tokens — the parsed
        # name contains all words from the target plus extra subtitle words.
        # Require at least 2 extra words to avoid matching numeric variants.
        # Also require target tokens to cover at least half of the parsed tokens
        # to avoid loose matches like "Death Sentence" matching
        # "Star Wars Darth Maul Death Sentence" (2/6 = 33% coverage).
        if (
            target_tokens
            and target_tokens < parsed_tokens
            and len(parsed_tokens - target_tokens) >= 2
            and len(target_tokens) / len(parsed_tokens) >= 0.5
        ):
            return NameMatchResult(
                is_match=True,
                similarity=0.90,
                match_type="token_subset",
                matched_against=target_name,
            )

        # Tier 4-5: Fuzzy match via SequenceMatcher
        ratio = SequenceMatcher(None, norm_parsed, norm_target).ratio()
        if ratio >= high:
            return NameMatchResult(
                is_match=True, similarity=ratio, match_type="fuzzy", matched_against=target_name
            )
        if ratio >= low:
            return NameMatchResult(
                is_match=True, similarity=ratio, match_type="fuzzy", matched_against=target_name
            )

        # Tier 6: No match
        return NameMatchResult(
            is_match=False, similarity=ratio, match_type="none", matched_against=""
        )

    @staticmethod
    def _check_starts_with(norm_a: str, norm_b: str) -> bool:
        """Check if one normalized name starts with the other + subtitle.

        Returns True only when the shared base is specific enough and the
        extra portion looks like a subtitle. This avoids false positives like
        "The Dead" matching "Dead Space Salvage" after article stripping.
        """
        if norm_a.startswith(norm_b):
            base = norm_b
            extra = norm_a[len(norm_b) :].strip()
        elif norm_b.startswith(norm_a):
            base = norm_a
            extra = norm_b[len(norm_a) :].strip()
        else:
            return False

        extra_words = extra.split()
        if len(extra_words) == 1 and extra_words[0] in _PUBLICATION_FORM_SUFFIXES:
            return True
        if len(base.split()) < 2:
            return False
        return len(extra_words) >= 2
