"""Tests for low-level ComicInfo.xml parsing."""

from __future__ import annotations

from pullbox.core.comicinfo import parse_comicinfo


def test_parse_comicinfo_preserves_story_arc_name_and_order_text() -> None:
    result = parse_comicinfo(
        """
        <ComicInfo>
          <StoryArc>Batman: The Court of Owls</StoryArc>
          <StoryArcNumber>001.50-A</StoryArcNumber>
        </ComicInfo>
        """
    )

    assert result.story_arc == "Batman: The Court of Owls"
    assert result.story_arc_number == "001.50-A"


def test_parse_comicinfo_rejects_xml_entities() -> None:
    xml = """\
<!DOCTYPE ComicInfo [
  <!ENTITY injected "Injected Series">
]>
<ComicInfo><Series>&injected;</Series></ComicInfo>
"""

    result = parse_comicinfo(xml)

    assert result.series is None
