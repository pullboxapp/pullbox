"""Story-arc identity normalization must be conservative and deterministic."""

from __future__ import annotations

import pytest

from pullbox.core.story_arc_identity import normalize_story_arc_name


def test_normalize_story_arc_name_collapses_case_and_whitespace() -> None:
    assert (
        normalize_story_arc_name("  Crisis\t on  Infinite\nEarths  ") == "crisis on infinite earths"
    )


def test_normalize_story_arc_name_uses_canonical_unicode_equivalence() -> None:
    composed = "Pok\N{LATIN SMALL LETTER E WITH ACUTE}mon"
    decomposed = "Poke\N{COMBINING ACUTE ACCENT}mon"

    assert normalize_story_arc_name(composed) == normalize_story_arc_name(decomposed)


def test_normalize_story_arc_name_preserves_semantic_punctuation_and_articles() -> None:
    assert normalize_story_arc_name("The End!") == "the end!"
    assert normalize_story_arc_name("The End!") != normalize_story_arc_name("End")
    assert normalize_story_arc_name("Secret Wars: 2099") != normalize_story_arc_name(
        "Secret Wars 2099"
    )


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_normalize_story_arc_name_rejects_blank_names(name: str) -> None:
    with pytest.raises(ValueError, match="must not be blank"):
        normalize_story_arc_name(name)
