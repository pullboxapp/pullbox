"""Safe data-only naming for optional story-arc placements."""

from __future__ import annotations

from pathlib import Path

import pytest

from pullbox.core.story_arc_naming import (
    DEFAULT_STORY_ARC_FILE_TEMPLATE,
    DEFAULT_STORY_ARC_FOLDER_TEMPLATE,
    StoryArcNamingValues,
    render_story_arc_relative_path,
    validate_story_arc_file_template,
    validate_story_arc_folder_template,
)


def _values(**overrides: object) -> StoryArcNamingValues:
    values: dict[str, object] = {
        "story_arc": "Absolute Carnage",
        "reading_order": 1,
        "series": "Venom",
        "issue_number": "1",
        "issue_title": "Rex",
        "year": 2019,
        "start_year": 2019,
        "end_year": 2020,
        "publisher": "Marvel Comics",
        "extension": "cbz",
    }
    values.update(overrides)
    return StoryArcNamingValues(**values)  # type: ignore[arg-type]


def test_default_story_arc_path_is_human_readable_and_relative() -> None:
    assert render_story_arc_relative_path(_values()) == Path(
        "Absolute Carnage",
        "001 - Venom 1 - Rex.cbz",
    )


@pytest.mark.parametrize("issue_number", ["1000000", "1AU", "0.5"])
def test_exact_issue_number_survives_arc_filename_rendering(issue_number: str) -> None:
    rendered = render_story_arc_relative_path(_values(issue_number=issue_number, issue_title=None))

    assert rendered == Path("Absolute Carnage", f"001 - Venom {issue_number}.cbz")


def test_optional_issue_title_does_not_leave_a_dangling_separator() -> None:
    rendered = render_story_arc_relative_path(_values(issue_title=None))

    assert rendered.name == "001 - Venom 1.cbz"


def test_mylar_style_span_year_folder_can_be_previewed_safely() -> None:
    rendered = render_story_arc_relative_path(
        _values(),
        folder_template="{StoryArc} ({SpanYears})",
        file_template="{ReadingOrder:04d} - {Series} {IssueNumber}{IssueTitleOptional}",
    )

    assert rendered == Path("Absolute Carnage (2019 - 2020)", "0001 - Venom 1 - Rex.cbz")


def test_mylar_publisher_folder_token_is_rendered_as_safe_data() -> None:
    rendered = render_story_arc_relative_path(
        _values(publisher="Marvel/Comics"),
        folder_template="{Publisher} - {StoryArc}",
    )

    assert rendered == Path("Marvel - Comics - Absolute Carnage", "001 - Venom 1 - Rex.cbz")


@pytest.mark.parametrize(
    "template",
    ["../{StoryArc}", "/{StoryArc}", "{StoryArc}/{StartYear}", "{Unknown}"],
)
def test_folder_templates_cannot_escape_or_execute_unknown_tokens(template: str) -> None:
    with pytest.raises(ValueError):
        validate_story_arc_folder_template(template)


@pytest.mark.parametrize(
    "template",
    [
        "../{Series}",
        "{Series}/{IssueNumber}",
        "{ReadingOrder:999d} - {Series}",
        "{Series} {Unknown}",
    ],
)
def test_file_templates_cannot_escape_or_use_unbounded_formatting(template: str) -> None:
    with pytest.raises(ValueError):
        validate_story_arc_file_template(template)


def test_default_templates_are_explicitly_validated() -> None:
    validate_story_arc_folder_template(DEFAULT_STORY_ARC_FOLDER_TEMPLATE)
    validate_story_arc_file_template(DEFAULT_STORY_ARC_FILE_TEMPLATE)
