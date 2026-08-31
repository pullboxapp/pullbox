"""Library naming helpers used by file registration."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, overload

from sqlalchemy import func, select

from pullbox.core.naming import (
    format_comic_file,
    format_series_folder,
    issue_type_supports_volume,
    issue_type_uses_collection_template,
    normalize_issue_type_for_naming,
    resolve_non_standard_file_template,
    resolve_single_non_standard_file_template,
)
from pullbox.models.issue import Issue, IssueType, is_non_standard_issue_type
from pullbox.models.series import Series

_SERIES_PATH_TOKEN_RE = re.compile(r"\{(?P<name>[A-Za-z][A-Za-z0-9]*)\}")
_SERIES_PATH_TOKENS = frozenset({"Publisher", "Series", "Year", "ComicVineId", "Type"})
_MAX_SERIES_PATH_TEMPLATE_BYTES = 1024
_FILE_TEMPLATE_TOKEN_RE = re.compile(r"\{[^{}]+\}")
_FILE_TEMPLATE_TOKENS = frozenset(
    {
        "{Series}",
        "{Year}",
        "{Issue}",
        "{Issue:03d}",
        "{Volume}",
        "{Volume:02d}",
        "{Type}",
        "{IssueTitle}",
        "{Title}",
        "{Publisher}",
        "{Edition}",
    }
)
_MAX_FILE_TEMPLATE_BYTES = 1024

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.library_policy import LibraryIngestPolicy
    from pullbox.models.library import LibraryRoot


def build_naming_snapshot(
    *,
    source_path: Path,
    prepared_source: Path,
    target_path: Path,
    issue: Issue,
    series: object,
    root: LibraryRoot,
    naming_policy: LibraryIngestPolicy | dict[str, str],
    rename: bool,
    effective_issue_type: str,
    transfer_method: str,
    move_to_library: bool,
    normalized_source: bool,
    update_embedded_comicinfo_from_match: bool,
    normalize_to_cbz: bool,
) -> dict[str, Any]:
    """Capture the naming inputs that explain the final placed path."""
    raw_issue_type = (
        issue.issue_type.value
        if isinstance(issue.issue_type, IssueType)
        else str(issue.issue_type)
        if issue.issue_type is not None
        else None
    )
    series_payload: dict[str, Any] = {}
    if isinstance(series, Series):
        series_payload = {
            "id": series.id,
            "comicvine_id": series.comicvine_id,
            "title": series.title,
            "year_start": series.year_start,
            "year_end": series.year_end,
            "series_type": series.series_type.value if series.series_type else None,
            "publisher": series.publisher.name if series.publisher is not None else None,
            "library_root_id": series.library_root_id,
        }

    return {
        "source_path": str(source_path),
        "prepared_source_path": str(prepared_source),
        "target_path": str(target_path),
        "target_folder": str(target_path.parent),
        "target_file_name": target_path.name,
        "source_extension": source_path.suffix.lstrip(".").lower(),
        "target_extension": target_path.suffix.lstrip(".").lower(),
        "rename_enabled": rename,
        "move_to_library": move_to_library,
        "transfer_method": transfer_method,
        "normalized_source": normalized_source,
        "normalize_to_cbz": normalize_to_cbz,
        "update_embedded_comicinfo_from_match": update_embedded_comicinfo_from_match,
        "library_root": {
            "id": root.id,
            "path": root.path,
        },
        "root_policy": {
            "id": getattr(naming_policy, "root_policy_id", None),
            "source": str(getattr(naming_policy, "policy_source", "global_default")),
            "revision": int(getattr(naming_policy, "policy_revision", 0)),
            "source_import_job_id": getattr(
                naming_policy,
                "source_import_job_id",
                None,
            ),
        },
        "series": series_payload,
        "issue": {
            "id": issue.id,
            "comicvine_id": issue.comicvine_id,
            "issue_number": issue.issue_number,
            "issue_number_text": issue.effective_issue_number_text,
            "title": issue.title,
            "raw_issue_type": raw_issue_type,
            "effective_issue_type": effective_issue_type,
        },
        "template_key": template_key_for_issue_type(effective_issue_type),
        "templates": {
            "series_path_template": policy_value(
                naming_policy,
                "series_path_template",
                policy_value(
                    naming_policy,
                    "series_folder_template",
                    "{Series} ({Year})",
                ),
            )
            or policy_value(
                naming_policy,
                "series_folder_template",
                "{Series} ({Year})",
            ),
            "series_folder_template": policy_value(
                naming_policy, "series_folder_template", "{Series} ({Year})"
            ),
            "comic_file_template": policy_value(
                naming_policy, "comic_file_template", "{Series} ({Year}) #{Issue:03d}"
            ),
            "annual_file_template": policy_value(
                naming_policy,
                "annual_file_template",
                "{Series} ({Year}) Annual #{Issue:03d}",
            ),
            "non_standard_file_template": policy_value(
                naming_policy,
                "non_standard_file_template",
                None,
            ),
            "single_non_standard_file_template": policy_value(
                naming_policy,
                "single_non_standard_file_template",
                None,
            ),
            "replace_illegal_characters": policy_bool(
                naming_policy, "replace_illegal_characters", True
            ),
            "colon_replacement": policy_value(naming_policy, "colon_replacement", "dash"),
        },
    }


def template_key_for_issue_type(issue_type: str) -> str:
    if issue_type == IssueType.ANNUAL.value:
        return "annual_file_template"
    if is_non_standard_issue_type(issue_type):
        if issue_type_uses_collection_template(issue_type):
            return "non_standard_file_template"
        return "single_non_standard_file_template"
    return "comic_file_template"


async def resolve_naming_issue_type(session: AsyncSession, issue: Issue) -> str:
    """Resolve the effective issue type used for naming output."""
    raw_issue_type = issue.issue_type.value if hasattr(issue, "issue_type") else "issue"
    if raw_issue_type not in {IssueType.TPB.value, IssueType.VOLUME.value}:
        return raw_issue_type

    count_result = await session.execute(
        select(func.count())
        .select_from(Issue)
        .where(
            Issue.series_id == issue.series_id,
            Issue.issue_type.in_([IssueType.TPB, IssueType.VOLUME]),
        )
    )
    collection_entry_count = int(count_result.scalar_one() or 0)
    return normalize_issue_type_for_naming(
        raw_issue_type,
        collection_series_entry_count=collection_entry_count,
    )


def build_series_folder_name(
    series: object,
    naming_policy: LibraryIngestPolicy | dict[str, str],
) -> str:
    """Build a series folder name from a Series model and naming config."""
    title = ""
    year: int | None = None
    publisher_name: str | None = None
    cv_id: int | None = None

    series_type_value: str | None = None

    if isinstance(series, Series):
        title = series.title
        year = series.year_start
        cv_id = series.comicvine_id
        series_type_value = series.series_type.value if series.series_type else None
        if series.publisher is not None:
            publisher_name = series.publisher.name

    return format_series_folder(
        title,
        year=year,
        publisher=publisher_name,
        comicvine_id=cv_id,
        series_type=series_type_value,
        template=policy_value(naming_policy, "series_folder_template", "{Series} ({Year})"),
        replace_illegal=policy_bool(naming_policy, "replace_illegal_characters", True),
        colon_replacement=policy_value(naming_policy, "colon_replacement", "dash"),
    )


def build_series_relative_path(
    series: object,
    naming_policy: LibraryIngestPolicy | dict[str, str],
) -> Path:
    """Render and sanitize every segment in a root-relative series path."""
    from pathlib import Path

    template = policy_value(
        naming_policy,
        "series_path_template",
        policy_value(naming_policy, "series_folder_template", "{Series} ({Year})"),
    ) or policy_value(naming_policy, "series_folder_template", "{Series} ({Year})")
    raw_segments = validate_series_path_template(template)

    rendered_segments = tuple(
        build_series_folder_name(
            series,
            {
                "series_folder_template": segment,
                "replace_illegal_characters": str(
                    policy_bool(naming_policy, "replace_illegal_characters", True)
                ),
                "colon_replacement": policy_value(
                    naming_policy,
                    "colon_replacement",
                    "dash",
                ),
            },
        )
        for segment in raw_segments
    )
    if any("/" in segment or "\\" in segment for segment in rendered_segments):
        raise ValueError("Series path token rendered an unsafe path separator.")
    if any(segment in {"", ".", ".."} for segment in rendered_segments):
        raise ValueError("Series path template rendered an unsafe segment.")
    return Path(*rendered_segments)


def validate_series_path_template(template: str) -> tuple[str, ...]:
    """Validate a root-relative output template and return its raw segments."""
    if not template or len(template.encode("utf-8")) > _MAX_SERIES_PATH_TEMPLATE_BYTES:
        raise ValueError("Series path template is empty or too long.")
    if template.startswith(("/", "\\")) or "\\" in template:
        raise ValueError("Series path template must be root-relative.")

    raw_segments = template.split("/")
    if any(segment in {"", ".", ".."} for segment in raw_segments):
        raise ValueError("Series path template contains an unsafe segment.")

    for matched in _SERIES_PATH_TOKEN_RE.finditer(template):
        if matched.group("name") not in _SERIES_PATH_TOKENS:
            raise ValueError(f"Unsupported series path token: {matched.group('name')}")
    without_tokens = _SERIES_PATH_TOKEN_RE.sub("", template)
    if "{" in without_tokens or "}" in without_tokens:
        raise ValueError("Series path template contains an invalid token.")
    return tuple(raw_segments)


def validate_library_file_template(template: str) -> None:
    """Reject unsupported or path-producing tokens in a file-name template."""
    if not template or len(template.encode("utf-8")) > _MAX_FILE_TEMPLATE_BYTES:
        raise ValueError("Library file template is empty or too long.")
    if "/" in template or "\\" in template or any(ord(char) < 32 for char in template):
        raise ValueError("Library file template must produce a single file name.")

    for token in _FILE_TEMPLATE_TOKEN_RE.findall(template):
        if token not in _FILE_TEMPLATE_TOKENS:
            raise ValueError(f"Unsupported library file token: {token}")
    without_tokens = _FILE_TEMPLATE_TOKEN_RE.sub("", template)
    if "{" in without_tokens or "}" in without_tokens:
        raise ValueError("Library file template contains an invalid token.")


def compute_target_filename(
    issue: Issue,
    series: object,
    source_path: Path,
    naming_policy: LibraryIngestPolicy | dict[str, str],
    *,
    issue_type_override: str | None = None,
) -> str:
    """Compute target filename using naming templates."""
    extension = source_path.suffix.lstrip(".")

    title = ""
    year: int | None = None
    publisher_name: str | None = None
    volume_number: int | None = None

    if isinstance(series, Series):
        title = series.title
        year = series.year_start
        if series.publisher is not None:
            publisher_name = series.publisher.name

    issue_type = issue_type_override or (
        issue.issue_type.value if hasattr(issue, "issue_type") else "issue"
    )
    if issue_type_supports_volume(issue_type) and float(issue.issue_number).is_integer():
        volume_number = int(issue.issue_number)

    if issue_type == "annual":
        template = policy_value(
            naming_policy,
            "annual_file_template",
            "{Series} ({Year}) Annual #{Issue:03d}",
        )
    elif is_non_standard_issue_type(issue_type):
        if issue_type_uses_collection_template(issue_type):
            template = resolve_non_standard_file_template(
                policy_value(naming_policy, "non_standard_file_template", None)
            )
        else:
            template = resolve_single_non_standard_file_template(
                policy_value(naming_policy, "single_non_standard_file_template", None)
            )
    else:
        template = policy_value(
            naming_policy,
            "comic_file_template",
            "{Series} ({Year}) #{Issue:03d}",
        )

    return format_comic_file(
        series=title,
        year=year,
        issue=issue.effective_issue_number_text,
        volume=volume_number,
        issue_type=issue_type,
        title=issue.title,
        publisher=publisher_name,
        template=template,
        extension=extension,
        replace_illegal=policy_bool(naming_policy, "replace_illegal_characters", True),
        colon_replacement=policy_value(naming_policy, "colon_replacement", "dash"),
    )


@overload
def policy_value(
    policy: LibraryIngestPolicy | dict[str, str],
    key: str,
    default: str,
) -> str: ...


@overload
def policy_value(
    policy: LibraryIngestPolicy | dict[str, str],
    key: str,
    default: None,
) -> str | None: ...


def policy_value(
    policy: LibraryIngestPolicy | dict[str, str],
    key: str,
    default: str | None,
) -> str | None:
    if isinstance(policy, dict):
        return policy.get(key, default)
    return getattr(policy, key, default)


def policy_bool(
    policy: LibraryIngestPolicy | dict[str, str],
    key: str,
    default: bool,
) -> bool:
    if isinstance(policy, dict):
        return str(policy.get(key, "true" if default else "false")).lower() == "true"
    return bool(getattr(policy, key, default))
