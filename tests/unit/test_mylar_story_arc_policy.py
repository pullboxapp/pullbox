"""Mylar story-arc settings become safe, review-only policy drafts."""

from __future__ import annotations

from pullbox.core.mylar3_reader import Mylar3ArcSettingsSnapshot, Mylar3ArcSettingValue
from pullbox.core.mylar_story_arc_policy import build_mylar_story_arc_policy_draft


def _settings(**overrides: bool | str | None) -> Mylar3ArcSettingsSnapshot:
    values: dict[str, bool | str | None] = {
        "STORYARCDIR": True,
        "STORYARC_LOCATION": "/mnt/comics/StoryArcs",
        "COPY2ARCDIR": True,
        "ARC_FOLDERFORMAT": "$publisher - $arc ($spanyears)",
        "ARC_FILEOPS": "softlink",
        "ARC_FILEOPS_SOFTLINK_RELATIVE": True,
        "UPCOMING_STORYARCS": True,
        "SEARCH_STORYARCS": True,
        "READ2FILENAME": True,
    }
    values.update(overrides)
    return Mylar3ArcSettingsSnapshot(
        present=True,
        parse_warnings=(),
        values=tuple(
            Mylar3ArcSettingValue(
                key=key,
                section="General" if key == "READ2FILENAME" else "StoryArc",
                value=value,
                raw_value=None if value is None else str(value),
                used_default=False,
            )
            for key, value in values.items()
        ),
    )


def test_all_nine_mylar_settings_map_to_one_canonical_review_only_draft() -> None:
    draft = build_mylar_story_arc_policy_draft(_settings())

    assert draft == {
        "schema_version": 1,
        "source": "mylar3",
        "activation": "requires_confirmation",
        "settings_present": True,
        "review_required": True,
        "review_warnings": [],
        "monitored": True,
        "search_missing": True,
        "include_upcoming": True,
        "sync_enabled": True,
        "placement_policy": {
            "schema_version": 1,
            "mode": "symlink",
            "target_library_root_id": None,
            "destination_root": "/mnt/comics/StoryArcs",
            "folder_template": "{Publisher} - {StoryArc} ({SpanYears})",
            "file_template": ("{ReadingOrder:03d} - {Series} {IssueNumber}{IssueTitleOptional}"),
            "symlink_style": "relative",
            "synchronize": True,
        },
        "confirmation": {
            "target_library_root_required": True,
            "destination_root_requires_approval": True,
            "ready_for_activation": False,
        },
    }


def test_legacy_move_is_never_exposed_and_proposes_safe_copy_with_warning() -> None:
    draft = build_mylar_story_arc_policy_draft(_settings(ARC_FILEOPS="move"))

    assert draft["placement_policy"]["mode"] == "copy"  # type: ignore[index]
    assert "legacy_move_mapped_to_copy" in draft["review_warnings"]
    assert draft["placement_policy"]["mode"] != "move"  # type: ignore[index]


def test_disabled_arc_directory_is_logical_even_when_copy_setting_is_inconsistent() -> None:
    draft = build_mylar_story_arc_policy_draft(_settings(STORYARCDIR=False, COPY2ARCDIR=True))

    placement = draft["placement_policy"]
    assert placement == {
        "schema_version": 1,
        "mode": "logical",
        "target_library_root_id": None,
        "destination_root": None,
        "folder_template": "{Publisher} - {StoryArc} ({SpanYears})",
        "file_template": ("{ReadingOrder:03d} - {Series} {IssueNumber}{IssueTitleOptional}"),
        "symlink_style": None,
        "synchronize": False,
    }
    assert draft["sync_enabled"] is False
    assert "copy_without_story_arc_directory" in draft["review_warnings"]


def test_separate_directory_without_copy_is_a_non_mutating_reference_proposal() -> None:
    draft = build_mylar_story_arc_policy_draft(_settings(COPY2ARCDIR=False))

    assert draft["placement_policy"]["mode"] == "reference_only"  # type: ignore[index]
    assert draft["placement_policy"]["synchronize"] is False  # type: ignore[index]
    assert draft["sync_enabled"] is False


def test_supported_file_operations_and_symlink_styles_are_translated() -> None:
    hardlink = build_mylar_story_arc_policy_draft(_settings(ARC_FILEOPS="hardlink"))
    absolute_link = build_mylar_story_arc_policy_draft(
        _settings(ARC_FILEOPS="softlink", ARC_FILEOPS_SOFTLINK_RELATIVE=False)
    )

    assert hardlink["placement_policy"]["mode"] == "hardlink"  # type: ignore[index]
    assert hardlink["placement_policy"]["symlink_style"] is None  # type: ignore[index]
    assert absolute_link["placement_policy"]["mode"] == "symlink"  # type: ignore[index]
    assert absolute_link["placement_policy"]["symlink_style"] == "absolute"  # type: ignore[index]


def test_reading_order_prefix_can_be_disabled_without_losing_exact_issue_tokens() -> None:
    draft = build_mylar_story_arc_policy_draft(_settings(READ2FILENAME=False))

    assert draft["placement_policy"]["file_template"] == (  # type: ignore[index]
        "{Series} {IssueNumber}{IssueTitleOptional}"
    )


def test_unknown_operation_and_folder_token_fail_closed_with_review_warnings() -> None:
    settings = _settings(
        ARC_FILEOPS="teleport",
        ARC_FOLDERFORMAT="$arc - $secret",
    )
    settings = Mylar3ArcSettingsSnapshot(
        present=True,
        parse_warnings=("unknown_value:ARC_FILEOPS",),
        values=settings.values,
    )

    draft = build_mylar_story_arc_policy_draft(settings)

    placement = draft["placement_policy"]
    assert placement["mode"] == "reference_only"  # type: ignore[index]
    assert placement["folder_template"] == "{StoryArc}"  # type: ignore[index]
    assert draft["review_warnings"] == [
        "unknown_value:ARC_FILEOPS",
        "unsupported_arc_fileops",
        "unsupported_folder_token:secret",
        "invalid_arc_folderformat",
    ]
    assert draft["review_required"] is True


def test_destination_is_only_a_candidate_until_root_and_path_are_approved() -> None:
    draft = build_mylar_story_arc_policy_draft(_settings(STORYARC_LOCATION=None))

    assert draft["placement_policy"]["target_library_root_id"] is None  # type: ignore[index]
    assert draft["placement_policy"]["destination_root"] is None  # type: ignore[index]
    assert "destination_root_missing" in draft["review_warnings"]
    assert draft["activation"] == "requires_confirmation"
    assert draft["confirmation"]["ready_for_activation"] is False  # type: ignore[index]


def test_unknown_and_malformed_setting_values_remain_visible_as_review_warnings() -> None:
    settings = _settings()
    settings = Mylar3ArcSettingsSnapshot(
        present=True,
        parse_warnings=(),
        values=(*settings.values, Mylar3ArcSettingValue("SURPRISE", "StoryArc", "x", "x", False)),
    )

    draft = build_mylar_story_arc_policy_draft(settings)

    assert "unsupported_setting:SURPRISE" in draft["review_warnings"]
