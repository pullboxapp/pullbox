"""Canonical ComicVine page links for records Pullbox tracks."""

from __future__ import annotations


def comicvine_issue_url(comicvine_id: int | None, stored_url: str | None = None) -> str | None:
    """Return the ComicVine page for an issue.

    A link fetched from ComicVine is kept as-is. Issues created from catalog
    data carry a ComicVine ID but no stored link, so the link is derived from
    the ID; ComicVine resolves ``/issue/4000-<id>/`` to the issue page.
    """
    stored = (stored_url or "").strip()
    if stored:
        return stored
    if comicvine_id is None or comicvine_id <= 0:
        return None
    return f"https://comicvine.gamespot.com/issue/4000-{comicvine_id}/"
