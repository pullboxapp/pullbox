"""Translate allowlisted Mylar story-arc settings into a review-only draft.

The translator is deliberately data-only. It does not resolve paths, inspect
library roots, call providers, or activate a policy. A later confirmation
boundary must select an approved root and validate the proposed destination.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pullbox.core.story_arc_naming import (
    DEFAULT_STORY_ARC_FILE_TEMPLATE,
    DEFAULT_STORY_ARC_FOLDER_TEMPLATE,
    validate_story_arc_folder_template,
)

if TYPE_CHECKING:
    from pullbox.core.mylar3_reader import Mylar3ArcSettingsSnapshot

MYLAR_STORY_ARC_POLICY_DRAFT_SCHEMA_VERSION = 1
MYLAR_READING_ORDER_FILE_TEMPLATE = DEFAULT_STORY_ARC_FILE_TEMPLATE
MYLAR_PLAIN_FILE_TEMPLATE = "{Series} {IssueNumber}{IssueTitleOptional}"

_EXPECTED_SETTINGS = frozenset(
    {
        "STORYARCDIR",
        "STORYARC_LOCATION",
        "COPY2ARCDIR",
        "ARC_FOLDERFORMAT",
        "ARC_FILEOPS",
        "ARC_FILEOPS_SOFTLINK_RELATIVE",
        "UPCOMING_STORYARCS",
        "SEARCH_STORYARCS",
        "READ2FILENAME",
    }
)
_DEFAULT_FOLDER_FORMAT = "$arc"
_DEFAULT_FILE_OPERATION = "copy"
_MAX_DESTINATION_BYTES = 1000
_MYLAR_FOLDER_TOKEN_RE = re.compile(r"\$(?P<name>[A-Za-z_][A-Za-z0-9_]*)")
_WARNING_CODE_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
_FOLDER_TOKEN_MAP = {
    "arc": "{StoryArc}",
    "spanyears": "{SpanYears}",
    "publisher": "{Publisher}",
}


def build_mylar_story_arc_policy_draft(
    settings: Mylar3ArcSettingsSnapshot,
) -> dict[str, object]:
    """Return one safe proposal that always requires explicit confirmation.

    ``placement_policy`` deliberately uses the exact keys accepted by the
    placement-policy boundary. Its root ID remains unset and its destination is
    only source evidence until the user approves and validates both values.
    """
    warnings = [_warning_code(item) for item in settings.parse_warnings]
    values: dict[str, bool | str | None] = {}
    for setting in settings.values:
        if setting.key not in _EXPECTED_SETTINGS:
            warnings.append(f"unsupported_setting:{_warning_code(setting.key)}")
            continue
        if setting.key in values:
            warnings.append(f"duplicate_setting:{_warning_code(setting.key)}")
            continue
        values[setting.key] = setting.value

    story_arc_directory = _bool_setting(
        values,
        "STORYARCDIR",
        default=False,
        warnings=warnings,
    )
    destination_root = _optional_string_setting(
        values,
        "STORYARC_LOCATION",
        default=None,
        warnings=warnings,
    )
    copy_to_arc_directory = _bool_setting(
        values,
        "COPY2ARCDIR",
        default=False,
        warnings=warnings,
    )
    raw_folder_format = _string_setting(
        values,
        "ARC_FOLDERFORMAT",
        default=_DEFAULT_FOLDER_FORMAT,
        warnings=warnings,
    )
    raw_file_operation = _string_setting(
        values,
        "ARC_FILEOPS",
        default=_DEFAULT_FILE_OPERATION,
        warnings=warnings,
    )
    relative_symlink = _bool_setting(
        values,
        "ARC_FILEOPS_SOFTLINK_RELATIVE",
        default=False,
        warnings=warnings,
    )
    include_upcoming = _bool_setting(
        values,
        "UPCOMING_STORYARCS",
        default=False,
        warnings=warnings,
    )
    search_missing = _bool_setting(
        values,
        "SEARCH_STORYARCS",
        default=False,
        warnings=warnings,
    )
    prefix_reading_order = _bool_setting(
        values,
        "READ2FILENAME",
        default=False,
        warnings=warnings,
    )

    mode = _placement_mode(
        story_arc_directory=story_arc_directory,
        copy_to_arc_directory=copy_to_arc_directory,
        file_operation=raw_file_operation,
        warnings=warnings,
    )
    folder_template = _translate_folder_template(raw_folder_format, warnings=warnings)
    file_template = (
        MYLAR_READING_ORDER_FILE_TEMPLATE if prefix_reading_order else MYLAR_PLAIN_FILE_TEMPLATE
    )
    synchronize = mode in {"copy", "hardlink", "symlink"}
    symlink_style = "relative" if relative_symlink else "absolute" if mode == "symlink" else None
    if mode != "symlink":
        symlink_style = None

    requires_destination = mode != "logical"
    proposed_destination = _bounded_destination(destination_root, warnings=warnings)
    if not requires_destination:
        proposed_destination = None
    elif proposed_destination is None:
        warnings.append("destination_root_missing")

    return {
        "schema_version": MYLAR_STORY_ARC_POLICY_DRAFT_SCHEMA_VERSION,
        "source": "mylar3",
        "activation": "requires_confirmation",
        "settings_present": bool(settings.present),
        # Explicit confirmation is required even when no warning was detected.
        "review_required": True,
        "review_warnings": _unique_warnings(warnings),
        "monitored": search_missing or include_upcoming,
        "search_missing": search_missing,
        "include_upcoming": include_upcoming,
        "sync_enabled": synchronize,
        "placement_policy": {
            "schema_version": 1,
            "mode": mode,
            "target_library_root_id": None,
            "destination_root": proposed_destination,
            "folder_template": folder_template,
            "file_template": file_template,
            "symlink_style": symlink_style,
            "synchronize": synchronize,
        },
        "confirmation": {
            "target_library_root_required": requires_destination,
            "destination_root_requires_approval": requires_destination,
            "ready_for_activation": False,
        },
    }


def _placement_mode(
    *,
    story_arc_directory: bool,
    copy_to_arc_directory: bool,
    file_operation: str,
    warnings: list[str],
) -> str:
    if not story_arc_directory:
        if copy_to_arc_directory:
            warnings.append("copy_without_story_arc_directory")
        return "logical"
    if not copy_to_arc_directory:
        return "reference_only"

    normalized = file_operation.strip().casefold()
    if normalized == "copy":
        return "copy"
    if normalized == "move":
        warnings.append("legacy_move_mapped_to_copy")
        return "copy"
    if normalized == "hardlink":
        return "hardlink"
    if normalized == "softlink":
        return "symlink"
    warnings.append("unsupported_arc_fileops")
    return "reference_only"


def _translate_folder_template(raw: str, *, warnings: list[str]) -> str:
    unsupported: list[str] = []

    def replace(match: re.Match[str]) -> str:
        source_name = match.group("name")
        replacement = _FOLDER_TOKEN_MAP.get(source_name.casefold())
        if replacement is None:
            unsupported.append(_warning_code(source_name).casefold())
            return match.group(0)
        return replacement

    translated = _MYLAR_FOLDER_TOKEN_RE.sub(replace, raw.strip())
    warnings.extend(f"unsupported_folder_token:{name}" for name in unsupported)
    if unsupported or "$" in translated:
        warnings.append("invalid_arc_folderformat")
        return DEFAULT_STORY_ARC_FOLDER_TEMPLATE
    try:
        validate_story_arc_folder_template(translated)
    except ValueError:
        warnings.append("invalid_arc_folderformat")
        return DEFAULT_STORY_ARC_FOLDER_TEMPLATE
    return translated


def _bool_setting(
    values: dict[str, bool | str | None],
    key: str,
    *,
    default: bool,
    warnings: list[str],
) -> bool:
    if key not in values:
        warnings.append(f"missing_setting:{key}")
        return default
    value = values[key]
    if not isinstance(value, bool):
        warnings.append(f"invalid_setting_type:{key}")
        return default
    return value


def _string_setting(
    values: dict[str, bool | str | None],
    key: str,
    *,
    default: str,
    warnings: list[str],
) -> str:
    if key not in values:
        warnings.append(f"missing_setting:{key}")
        return default
    value = values[key]
    if not isinstance(value, str):
        warnings.append(f"invalid_setting_type:{key}")
        return default
    return value


def _optional_string_setting(
    values: dict[str, bool | str | None],
    key: str,
    *,
    default: str | None,
    warnings: list[str],
) -> str | None:
    if key not in values:
        warnings.append(f"missing_setting:{key}")
        return default
    value = values[key]
    if value is None:
        return None
    if not isinstance(value, str):
        warnings.append(f"invalid_setting_type:{key}")
        return default
    normalized = value.strip()
    return normalized or None


def _bounded_destination(value: str | None, *, warnings: list[str]) -> str | None:
    if value is None:
        return None
    if len(value.encode("utf-8")) > _MAX_DESTINATION_BYTES:
        warnings.append("destination_root_too_long")
        return None
    return value


def _warning_code(value: object) -> str:
    bounded = str(value)[:200]
    normalized = _WARNING_CODE_RE.sub("_", bounded).strip("_")
    return normalized or "unknown"


def _unique_warnings(warnings: list[str]) -> list[str]:
    return list(dict.fromkeys(warnings))
