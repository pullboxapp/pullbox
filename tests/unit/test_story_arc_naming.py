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


@pytest.mark.parametrize(
    "template,prefix",
    [("{OriginalFilename}", ""), ("{ReadingOrder:02d} - {OriginalFilename}", "01 - ")],
)
@pytest.mark.parametrize(
    "filename", ["Batman  1000000 (Digital) [Team].CBZ", "Été #1AU.cbz", "Issue {Series}.cbz"]
)
def test_original_filename_preserves_basename_and_extension_exactly(
    template: str, prefix: str, filename: str
) -> None:
    rendered = render_story_arc_relative_path(
        _values(original_filename=filename), file_template=template
    )

    assert rendered == Path("Absolute Carnage", prefix + filename)


@pytest.mark.parametrize(
    "filename",
    [
        None,
        "",
        "../source.cbz",
        "/source.cbz",
        r"..\source.cbz",
        r"C:\source.cbz",
        "source\x00.cbz",
        "source\n.cbz",
        "CON.cbz",
        "CON .cbz",
        "COM¹.cbz",
        "CONIN$.cbz",
        "source?.cbz",
        "source.cbz ",
        "a" * 241 + ".cbz",
        "source.cbr",
        "source\udcff.cbz",
    ],
)
def test_original_filename_fails_closed_instead_of_renaming_unsafe_source(
    filename: str | None,
) -> None:
    with pytest.raises(ValueError, match="Original filename"):
        render_story_arc_relative_path(
            _values(original_filename=filename), file_template="{OriginalFilename}"
        )


@pytest.mark.parametrize(
    "template",
    [
        "{OriginalFilename}.{Extension}",
        "{OriginalFilename}{OriginalFilename}",
        "{OriginalFilename}.cbz",
        "{OriginalFilename:02d}",
    ],
)
def test_original_filename_cannot_duplicate_extension_or_add_a_suffix(template: str) -> None:
    with pytest.raises(ValueError):
        validate_story_arc_file_template(template)


def test_original_filename_prefix_cannot_silently_truncate_the_basename() -> None:
    filename = "a" * 236 + ".cbz"
    with pytest.raises(ValueError, match="Original filename"):
        render_story_arc_relative_path(
            _values(original_filename=filename),
            file_template="{ReadingOrder:02d} - {OriginalFilename}",
        )


def test_saved_metadata_template_ignores_original_filename_and_keeps_exact_issue_text() -> None:
    rendered = render_story_arc_relative_path(
        _values(original_filename="../unused.cbz", issue_number="1000000"),
        file_template=DEFAULT_STORY_ARC_FILE_TEMPLATE,
    )

    assert rendered.name == "001 - Venom 1000000 - Rex.cbz"


def test_original_filename_keeps_spacing_while_metadata_prefix_is_sanitized() -> None:
    rendered = render_story_arc_relative_path(
        _values(original_filename="My  Scanner {Series}.CBZ", series="Venom/Spidey"),
        file_template="{ReadingOrder:02d} - {Series} - {OriginalFilename}",
    )

    assert rendered.name == "01 - Venom - Spidey - My  Scanner {Series}.CBZ"
