"""ComicVine page links derived from the records Pullbox tracks."""

from __future__ import annotations

import pytest

from pullbox.core.comicvine_links import comicvine_issue_url


def test_stored_link_is_kept() -> None:
    stored = "https://comicvine.gamespot.com/king-dracula-4/4000-1122334/"

    assert comicvine_issue_url(1122334, stored) == stored


@pytest.mark.parametrize("stored", [None, "", "   "])
def test_link_is_derived_from_the_issue_id_when_none_is_stored(stored: str | None) -> None:
    assert (
        comicvine_issue_url(1002169, stored) == "https://comicvine.gamespot.com/issue/4000-1002169/"
    )


@pytest.mark.parametrize("comicvine_id", [None, 0, -5])
def test_no_link_without_a_usable_issue_id(comicvine_id: int | None) -> None:
    assert comicvine_issue_url(comicvine_id) is None
