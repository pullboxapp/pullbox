"""REST schemas for story-arc placement policy, preview, and synchronization."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic resolves this at runtime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pullbox.models.story_arc import (  # noqa: TC001 - used by Pydantic at runtime
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
    StoryArcPlacementState,
    StoryArcSymlinkStyle,
)
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementPolicyMode,
)


class StoryArcPlacementPolicyPayload(BaseModel):
    """Complete candidate policy; omission never inherits hidden global state."""

    mode: StoryArcPlacementPolicyMode
    target_library_root_id: int | None = Field(None, gt=0)
    destination_root: str | None = Field(None, max_length=1000)
    folder_template: str = Field(..., min_length=1, max_length=1024)
    file_template: str = Field(..., min_length=1, max_length=1024)
    symlink_style: StoryArcSymlinkStyle | None = None
    synchronize: bool

    @model_validator(mode="after")
    def validate_mode_shape(self) -> Self:
        """Reject contradictory roots/styles before reaching filesystem code."""
        if self.mode is StoryArcPlacementPolicyMode.LOGICAL:
            if self.target_library_root_id is not None or self.destination_root is not None:
                raise ValueError("Logical-only policy must not configure a placement root")
        elif self.target_library_root_id is None or self.destination_root is None:
            raise ValueError("Non-logical policy requires a library and destination root")
        if self.mode is StoryArcPlacementPolicyMode.SYMLINK:
            if self.symlink_style is None:
                raise ValueError("Symlink policy requires a symlink style")
        elif self.symlink_style is not None:
            raise ValueError("Only symlink policy may specify a symlink style")
        return self


class StoryArcPlacementPolicyUpdate(StoryArcPlacementPolicyPayload):
    """Optimistically replace one complete per-arc policy."""

    expected_revision: int = Field(..., ge=1)


class StoryArcPlacementPolicyResponse(BaseModel):
    """Effective complete policy, including whether it has been frozen."""

    configured: bool
    revision: int
    mode: StoryArcPlacementPolicyMode
    target_library_root_id: int | None
    destination_root: str | None
    folder_template: str
    file_template: str
    symlink_style: StoryArcSymlinkStyle | None
    synchronize: bool
    snapshot: dict[str, object]


class StoryArcPlacementPreviewItemResponse(BaseModel):
    """Read-only placement plan for one ordered membership."""

    model_config = ConfigDict(from_attributes=True)

    membership_id: int
    sequence_number: int
    issue_id: int | None
    issue_number_text: str
    mode: str
    state: str
    target_path: str | None
    collision: str
    reason: str | None
    required_bytes: int
    proposed_ownership: str
    overwrite_allowed: bool
    classification: str
    placement_id: int | None
    current_ownership: StoryArcPlacementOwnership | None
    inspection_code: str | None


class StoryArcPlacementPreviewPageResponse(BaseModel):
    """Policy plus one bounded deterministic preview page."""

    policy: StoryArcPlacementPolicyResponse
    items: list[StoryArcPlacementPreviewItemResponse]
    total: int
    limit: int
    offset: int
    has_more: bool


class StoryArcPlacementResponse(BaseModel):
    """Durable placement ownership, fingerprint, and synchronization state."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    issue_story_arc_id: int
    library_file_id: int | None
    library_root_id: int | None
    placement_path: str
    mode: StoryArcPlacementMode
    ownership: StoryArcPlacementOwnership
    symlink_style: StoryArcSymlinkStyle | None
    rendered_reading_order: int | None
    policy_schema_version: int | None
    source_fingerprint: dict[str, object]
    target_fingerprint: dict[str, object]
    state: StoryArcPlacementState
    last_result: dict[str, object]
    last_checked_at: datetime | None


class StoryArcPlacementSyncRequest(BaseModel):
    """Explicitly approve adoption of an identical user artifact when desired."""

    adopt_identical_existing: bool = False


class StoryArcPlacementSyncResponse(BaseModel):
    """One completed logical, referenced, created, repaired, or idempotent sync."""

    membership_id: int
    outcome: str
    placement: StoryArcPlacementResponse | None


class StoryArcPlacementRemovalResponse(BaseModel):
    """Ownership-aware removal result that makes preservation guarantees explicit."""

    placement_id: int
    ownership: StoryArcPlacementOwnership
    artifact_removed: bool
    canonical_preserved: bool
    referenced_artifact_preserved: bool
    automatic_sync_disabled: bool
