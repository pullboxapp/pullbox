"""Tests for the versioned source-layout grammar."""

from __future__ import annotations

import pytest

from pullbox.core.library_layout import (
    ImportLayoutMode,
    LayoutTemplateError,
    LayoutValueError,
    SourceLayoutSpec,
    compile_source_layout,
    resolve_source_layout_spec,
)


def test_series_folder_preset_is_registered_and_normalized() -> None:
    effective = resolve_source_layout_spec(
        SourceLayoutSpec(mode=ImportLayoutMode.PRESET, preset="series_folders")
    )

    assert effective.series_path_template == "{Series}"
    assert effective.issue_filename_template is None
    assert effective.to_dict() == {
        "schema_version": 1,
        "mode": "preset",
        "preset": "series_folders",
        "series_path_template": "{Series}",
        "issue_filename_template": None,
        "selected_cluster_id": None,
        "fallback_to_auto": True,
    }


def test_publisher_series_preset_extracts_folder_evidence() -> None:
    matcher = compile_source_layout(
        SourceLayoutSpec(mode=ImportLayoutMode.PRESET, preset="publisher_series")
    )

    match = matcher.match("DC Comics/Batman (2011)/Issue 001.cbz")

    assert match is not None
    assert match.publisher == "DC Comics"
    assert match.series == "Batman"
    assert match.year == 2011
    assert match.issue_number == "1"


def test_series_folder_preset_extracts_supported_bare_year_suffix() -> None:
    matcher = compile_source_layout(
        SourceLayoutSpec(mode=ImportLayoutMode.PRESET, preset="series_folders")
    )

    match = matcher.match("Absolute Batman 2024/Issue 014.cbz")

    assert match is not None
    assert match.series == "Absolute Batman"
    assert match.year == 2024
    assert match.issue_number == "14"


@pytest.mark.parametrize("issue_text", ["1", "01", "001"])
def test_issue_width_is_tolerant_on_input(issue_text: str) -> None:
    matcher = compile_source_layout(
        SourceLayoutSpec(
            mode=ImportLayoutMode.CUSTOM,
            series_path_template="{Series} ({Year})",
            issue_filename_template="Issue {Issue:03d} {IssueTitle}",
        )
    )

    match = matcher.match(f"Batman (2011)/Issue {issue_text} The Court of Owls.cbz")

    assert match is not None
    assert match.series == "Batman"
    assert match.issue_number == "1"
    assert match.issue_title == "The Court of Owls"


def test_title_alias_and_repeated_series_tokens_must_agree() -> None:
    matcher = compile_source_layout(
        SourceLayoutSpec(
            mode=ImportLayoutMode.CUSTOM,
            series_path_template="{Series}",
            issue_filename_template="{Series} {Title} Issue {Issue}",
        )
    )

    match = matcher.match("Batman/Batman The Court of Owls, Part One Issue 001.cbz")

    assert match is not None
    assert match.issue_title == "The Court of Owls, Part One"
    assert match.issue_number == "1"
    assert matcher.match("Batman/Superman The Court of Owls Issue 001.cbz") is None


def test_repeated_multiword_series_uses_the_folder_capture_as_boundary() -> None:
    matcher = compile_source_layout(
        SourceLayoutSpec(
            mode=ImportLayoutMode.CUSTOM,
            series_path_template="{Series}",
            issue_filename_template="{Series} {IssueTitle} Issue {Issue}",
        )
    )

    match = matcher.match(
        "The Amazing Spider-Man/The Amazing Spider-Man The Parker Luck, Part One Issue 001.cbz"
    )

    assert match is not None
    assert match.series == "The Amazing Spider-Man"
    assert match.issue_title == "The Parker Luck, Part One"


def test_user_regex_characters_are_matched_as_static_text() -> None:
    matcher = compile_source_layout(
        SourceLayoutSpec(
            mode=ImportLayoutMode.CUSTOM,
            series_path_template="{Series}.*",
        )
    )

    assert matcher.match("Batman.*/Issue 001.cbz") is not None
    assert matcher.match("BatmanXYZ/Issue 001.cbz") is None


@pytest.mark.parametrize(
    ("path_template", "filename_template"),
    [
        ("/{Series}", None),
        ("../{Series}", None),
        ("{Publisher}//{Series}", None),
        ("{Unknown}", None),
        ("{Series}/{Series}{Publisher}", None),
        ("{Series}", "{Issue:03x}"),
        ("{Series}", "{Issue}.cbz"),
        ("C:\\{Series}", None),
    ],
)
def test_unsafe_or_ambiguous_templates_are_rejected(
    path_template: str,
    filename_template: str | None,
) -> None:
    with pytest.raises(LayoutTemplateError):
        compile_source_layout(
            SourceLayoutSpec(
                mode=ImportLayoutMode.CUSTOM,
                series_path_template=path_template,
                issue_filename_template=filename_template,
            )
        )


@pytest.mark.parametrize("unsafe_value", ["CON", "bad\x00name", "bad\nname"])
def test_unsafe_captured_values_are_rejected(unsafe_value: str) -> None:
    matcher = compile_source_layout(
        SourceLayoutSpec(
            mode=ImportLayoutMode.CUSTOM,
            series_path_template="{Series}",
        )
    )

    with pytest.raises(LayoutValueError):
        matcher.match(f"{unsafe_value}/Issue 001.cbz")


def test_unknown_schema_version_and_preset_are_rejected() -> None:
    with pytest.raises(LayoutTemplateError, match="schema version"):
        compile_source_layout(SourceLayoutSpec(schema_version=2))
    with pytest.raises(LayoutTemplateError, match="preset"):
        compile_source_layout(
            SourceLayoutSpec(mode=ImportLayoutMode.PRESET, preset="not_registered")
        )


def test_source_layout_spec_has_deterministic_round_trip() -> None:
    original = SourceLayoutSpec(
        mode=ImportLayoutMode.CUSTOM,
        series_path_template="{Publisher}/{Series} ({Year})",
        issue_filename_template="Issue {Issue:03d} - {IssueTitle}",
        fallback_to_auto=False,
    )

    restored = SourceLayoutSpec.from_dict(original.to_dict())

    assert restored == original


def test_source_layout_spec_rejects_non_boolean_fallback() -> None:
    with pytest.raises(LayoutTemplateError, match="boolean"):
        SourceLayoutSpec.from_dict(
            {
                "schema_version": 1,
                "mode": "custom",
                "series_path_template": "{Series}",
                "fallback_to_auto": "false",
            }
        )


@pytest.mark.parametrize(
    "relative_path",
    [
        "/Batman/Issue 001.cbz",
        "Batman/../Superman/Issue 001.cbz",
        "Batman//Issue 001.cbz",
        "Batman/./Issue 001.cbz",
    ],
)
def test_matcher_rejects_unsafe_relative_input_paths(relative_path: str) -> None:
    matcher = compile_source_layout(
        SourceLayoutSpec(mode=ImportLayoutMode.PRESET, preset="series_folders")
    )

    with pytest.raises(LayoutValueError):
        matcher.match(relative_path)


def test_exact_large_issue_number_is_not_rendered_in_scientific_notation() -> None:
    matcher = compile_source_layout(
        SourceLayoutSpec(
            mode=ImportLayoutMode.CUSTOM,
            series_path_template="{Series}",
            issue_filename_template="Issue {Issue}",
        )
    )

    match = matcher.match("DC One Million/Issue 1000000.cbz")

    assert match is not None
    assert match.issue_number == "1000000"
