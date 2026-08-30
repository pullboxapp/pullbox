"""Explicit Step 3 confirmation for one staged story-arc policy."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.story_arc_naming import (
    DEFAULT_STORY_ARC_FILE_TEMPLATE,
    DEFAULT_STORY_ARC_FOLDER_TEMPLATE,
    StoryArcNamingValues,
    render_story_arc_relative_path,
)
from pullbox.models.import_job import ImportJob, ImportJobStatus
from pullbox.models.story_arc import ImportedStoryArcStatus
from pullbox.models.story_arc_import import ImportedStoryArc
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementIntegrationError,
    StoryArcPlacementPolicyInput,
    StoryArcPlacementPolicyMode,
    validate_story_arc_placement_policy_input,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

_POLICY_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_MUTABLE_REVIEW_STATUSES = frozenset(
    {
        ImportedStoryArcStatus.DETECTED,
        ImportedStoryArcStatus.NEEDS_REVIEW,
        ImportedStoryArcStatus.READY,
    }
)
_SETTING_LABELS = {
    "STORYARCDIR": "Separate story-arc directory",
    "STORYARC_LOCATION": "Destination",
    "COPY2ARCDIR": "Copy to arc directory",
    "ARC_FOLDERFORMAT": "Folder format",
    "ARC_FILEOPS": "File operation",
    "ARC_FILEOPS_SOFTLINK_RELATIVE": "Relative symlinks",
    "UPCOMING_STORYARCS": "Include upcoming issues",
    "SEARCH_STORYARCS": "Search for missing issues",
    "READ2FILENAME": "Reading-order prefix",
}
_REVIEW_WARNING_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


@dataclass(frozen=True, slots=True)
class ImportStoryArcPolicyConfirmationResult:
    """Safe API-facing result without private destination details."""

    imported_story_arc_id: int
    activation: str
    materialize_filesystem: bool
    mode: str
    monitored: bool
    search_missing: bool
    include_upcoming: bool
    sync_enabled: bool
    policy_digest: str


@dataclass(frozen=True, slots=True)
class ImportStoryArcSourceSettingReview:
    """One sanitized source-setting summary without private path values."""

    key: str
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class ImportStoryArcPolicyReview:
    """Safe Step 3 presentation of one draft or confirmed policy."""

    policy_digest: str
    activation: str
    confirmed: bool
    warnings: tuple[str, ...]
    source_settings: tuple[ImportStoryArcSourceSettingReview, ...]
    monitored: bool
    search_missing: bool
    include_upcoming: bool
    materialize_filesystem: bool
    mode: str
    target_library_root_id: int | None
    destination_configured: bool
    folder_template: str
    file_template: str
    symlink_style: str | None
    synchronize: bool
    example_relative_path: str | None


def story_arc_policy_digest(snapshot: Mapping[str, object]) -> str:
    """Return the stable optimistic token for one complete JSON policy snapshot."""
    encoded = json.dumps(
        dict(snapshot),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_import_story_arc_policy_review(
    snapshot: Mapping[str, object],
    source_settings_snapshot: Mapping[str, object],
    diagnostics: Mapping[str, object] | None = None,
) -> ImportStoryArcPolicyReview:
    """Build a bounded, path-redacted review view without source or filesystem I/O."""
    raw = dict(snapshot)
    placement = _mapping(raw.get("placement_policy"))
    activation = str(raw.get("activation") or "requires_confirmation")
    mode = _safe_string_choice(
        placement.get("mode"),
        {"logical", "reference_only", "copy", "hardlink", "symlink"},
        "logical",
    )
    folder_template = _bounded_template(
        placement.get("folder_template"),
        DEFAULT_STORY_ARC_FOLDER_TEMPLATE,
    )
    file_template = _bounded_template(
        placement.get("file_template"),
        DEFAULT_STORY_ARC_FILE_TEMPLATE,
    )
    raw_symlink_style = placement.get("symlink_style")
    symlink_style = (
        raw_symlink_style
        if isinstance(raw_symlink_style, str) and raw_symlink_style in {"absolute", "relative"}
        else None
    )
    if mode != "symlink":
        symlink_style = None
    root_id_raw = placement.get("target_library_root_id")
    root_id = (
        root_id_raw
        if isinstance(root_id_raw, int) and not isinstance(root_id_raw, bool) and root_id_raw > 0
        else None
    )
    destination = placement.get("destination_root")
    destination_configured = isinstance(destination, str) and bool(destination.strip())
    warnings = _review_warning_codes(raw, source_settings_snapshot, diagnostics or {})
    return ImportStoryArcPolicyReview(
        policy_digest=story_arc_policy_digest(raw),
        activation=activation,
        confirmed=activation == "confirmed",
        warnings=warnings,
        source_settings=_source_setting_reviews(source_settings_snapshot),
        monitored=raw.get("monitored") is True,
        search_missing=raw.get("search_missing") is True,
        include_upcoming=raw.get("include_upcoming") is True,
        materialize_filesystem=mode != "logical",
        mode=mode,
        target_library_root_id=root_id,
        destination_configured=destination_configured,
        folder_template=folder_template,
        file_template=file_template,
        symlink_style=symlink_style,
        synchronize=placement.get("synchronize") is True,
        example_relative_path=_policy_example(folder_template, file_template),
    )


async def confirm_import_story_arc_policy(
    session: AsyncSession,
    *,
    job_id: int,
    imported_story_arc_id: int,
    expected_policy_digest: str,
    explicit_confirmation: bool,
    materialize_filesystem: bool,
    monitored: bool,
    search_missing: bool,
    include_upcoming: bool,
    placement_policy: StoryArcPlacementPolicyInput,
) -> ImportStoryArcPolicyConfirmationResult:
    """Validate and freeze one staged policy without creating arcs or files."""
    if explicit_confirmation is not True:
        raise ValidationError("You must explicitly confirm this story arc policy")
    if not isinstance(expected_policy_digest, str) or not _POLICY_DIGEST_RE.fullmatch(
        expected_policy_digest
    ):
        raise ValidationError("Story arc policy review token is invalid")
    if not all(
        isinstance(value, bool)
        for value in (
            materialize_filesystem,
            monitored,
            search_missing,
            include_upcoming,
        )
    ):
        raise ValidationError("Story arc policy choices must be true or false")
    if not monitored and (search_missing or include_upcoming):
        raise ValidationError(
            "Story arc search and upcoming automation require monitoring to be enabled"
        )

    job = await session.scalar(select(ImportJob).where(ImportJob.id == job_id).with_for_update())
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status != ImportJobStatus.REVIEW or job.import_started_at is not None:
        raise ValidationError("Job must be in REVIEW state to confirm story arc policy")

    staged_arc = await session.scalar(
        select(ImportedStoryArc)
        .where(
            ImportedStoryArc.id == imported_story_arc_id,
            ImportedStoryArc.import_job_id == job_id,
        )
        .with_for_update()
    )
    if staged_arc is None:
        raise NotFoundError("ImportedStoryArc", imported_story_arc_id)
    if staged_arc.status not in _MUTABLE_REVIEW_STATUSES:
        raise ValidationError("Story arc policy can only change during active Step 3 review")

    current_snapshot = dict(staged_arc.proposed_policy_snapshot or {})
    current_source = current_snapshot.get("source")
    if current_source is not None and current_source != staged_arc.source_kind.value:
        raise ValidationError("Staged story arc policy source does not match its evidence")
    current_digest = story_arc_policy_digest(current_snapshot)
    if not hmac.compare_digest(current_digest, expected_policy_digest):
        raise ValidationError("Story arc policy changed; review the latest draft and try again")

    logical = _proposal_is_logical(placement_policy)
    if materialize_filesystem == logical:
        raise ValidationError(
            "Story arc filesystem materialization choice does not match the placement policy"
        )

    try:
        validated_policy = await validate_story_arc_placement_policy_input(
            session,
            placement_policy,
            revision=1,
        )
    except StoryArcPlacementIntegrationError as exc:
        raise ValidationError(str(exc)) from exc

    canonical_snapshot = validated_policy.snapshot
    if materialize_filesystem is False and canonical_snapshot["mode"] != "logical":
        raise ValidationError("Logical story arc import cannot activate a filesystem policy")
    if materialize_filesystem is True and canonical_snapshot["mode"] == "logical":
        raise ValidationError("Filesystem materialization requires a placement mode")

    durable_warnings = _review_warning_codes(
        current_snapshot,
        staged_arc.source_settings_snapshot or {},
        staged_arc.diagnostics or {},
    )
    confirmed_snapshot: dict[str, object] = {
        "schema_version": 1,
        "source": staged_arc.source_kind.value,
        "activation": "confirmed",
        "monitored": monitored,
        "search_missing": search_missing,
        "include_upcoming": include_upcoming,
        "sync_enabled": validated_policy.synchronize,
        "placement_policy": canonical_snapshot,
    }
    diagnostics = dict(staged_arc.diagnostics or {})
    diagnostics["story_arc_policy_review"] = {
        "schema_version": 1,
        "warning_codes": list(durable_warnings),
    }
    staged_arc.diagnostics = diagnostics
    staged_arc.proposed_policy_snapshot = confirmed_snapshot
    await session.flush()
    return ImportStoryArcPolicyConfirmationResult(
        imported_story_arc_id=int(staged_arc.id),
        activation="confirmed",
        materialize_filesystem=materialize_filesystem,
        mode=validated_policy.mode.value,
        monitored=monitored,
        search_missing=search_missing,
        include_upcoming=include_upcoming,
        sync_enabled=validated_policy.synchronize,
        policy_digest=story_arc_policy_digest(confirmed_snapshot),
    )


def _proposal_is_logical(policy: StoryArcPlacementPolicyInput) -> bool:
    try:
        return StoryArcPlacementPolicyMode(policy.mode) is StoryArcPlacementPolicyMode.LOGICAL
    except ValueError:
        return False


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, dict):
        return value
    return {}


def _safe_string_choice(value: object, allowed: set[str], default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


def _bounded_template(value: object, default: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024:
        return default
    return value


def _review_warning_codes(
    snapshot: Mapping[str, object],
    source_settings_snapshot: Mapping[str, object],
    diagnostics: Mapping[str, object],
) -> tuple[str, ...]:
    values: list[object] = []
    draft_warnings = snapshot.get("review_warnings")
    if isinstance(draft_warnings, list | tuple):
        values.extend(draft_warnings[:20])
    parse_warnings = source_settings_snapshot.get("parse_warnings")
    if isinstance(parse_warnings, list | tuple):
        values.extend(parse_warnings[:20])
    durable_review = _mapping(diagnostics.get("story_arc_policy_review"))
    durable_warnings = durable_review.get("warning_codes")
    if isinstance(durable_warnings, list | tuple):
        values.extend(durable_warnings[:20])
    result: list[str] = []
    for value in values:
        normalized = _REVIEW_WARNING_RE.sub("_", str(value)[:200]).strip("_")
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result[:20])


def _source_setting_reviews(
    source_settings_snapshot: Mapping[str, object],
) -> tuple[ImportStoryArcSourceSettingReview, ...]:
    raw_values = _mapping(source_settings_snapshot.get("values"))
    reviews: list[ImportStoryArcSourceSettingReview] = []
    for key, label in _SETTING_LABELS.items():
        setting = _mapping(raw_values.get(key))
        if not setting:
            continue
        value = setting.get("value")
        if key == "STORYARC_LOCATION":
            display = "Configured" if isinstance(value, str) and bool(value.strip()) else "Not set"
        elif isinstance(value, bool):
            display = "Enabled" if value else "Disabled"
        elif key == "ARC_FILEOPS" and isinstance(value, str):
            normalized = value.strip().casefold()
            display = (
                normalized
                if normalized in {"copy", "move", "hardlink", "softlink"}
                else "Needs review"
            )
        elif key == "ARC_FOLDERFORMAT" and isinstance(value, str):
            display = "Detected" if value.strip() else "Not set"
        else:
            display = "Detected" if value is not None else "Not set"
        reviews.append(ImportStoryArcSourceSettingReview(key=key, label=label, value=display))
    return tuple(reviews)


def _policy_example(folder_template: str, file_template: str) -> str | None:
    try:
        rendered = render_story_arc_relative_path(
            StoryArcNamingValues(
                story_arc="Example Arc",
                reading_order=1,
                series="Example Series",
                publisher="Example Publisher",
                issue_number="1",
                issue_title="Example Issue",
                year=2026,
                start_year=2025,
                end_year=2026,
                extension="cbz",
            ),
            folder_template=folder_template,
            file_template=file_template,
        )
    except ValueError:
        return None
    return str(rendered)
