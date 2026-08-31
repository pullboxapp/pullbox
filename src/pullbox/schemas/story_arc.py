"""Request and response schemas for first-class logical story arcs."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic resolves this at runtime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pullbox.models.story_arc import (  # noqa: TC001 - Pydantic resolves these at runtime
    StoryArcLifecycle,
    StoryArcResolutionState,
    StoryArcSourceKind,
)


class StoryArcCreate(BaseModel):
    """Create a Pullbox-owned logical story arc."""

    name: str = Field(..., min_length=1, max_length=500)
    description: str | None = None
    monitored: bool = False
    search_missing: bool = False
    include_upcoming: bool = False
    sync_enabled: bool = False


class StoryArcUpdate(BaseModel):
    """Patch mutable arc metadata with optimistic revision protection."""

    expected_revision: int = Field(..., ge=1)
    name: str | None = Field(None, min_length=1, max_length=500)
    description: str | None = None
    monitored: bool | None = None
    search_missing: bool | None = None
    include_upcoming: bool | None = None
    sync_enabled: bool | None = None

    @model_validator(mode="after")
    def require_mutation(self) -> Self:
        """Reject revision-only patches that would advance state without a change."""
        if self.model_fields_set <= {"expected_revision"}:
            raise ValueError("At least one story-arc field must be provided")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("Story-arc name cannot be null")
        if "monitored" in self.model_fields_set and self.monitored is None:
            raise ValueError("Story-arc monitored cannot be null")
        if "search_missing" in self.model_fields_set and self.search_missing is None:
            raise ValueError("Story-arc search_missing cannot be null")
        if "include_upcoming" in self.model_fields_set and self.include_upcoming is None:
            raise ValueError("Story-arc include_upcoming cannot be null")
        if "sync_enabled" in self.model_fields_set and self.sync_enabled is None:
            raise ValueError("Story-arc sync_enabled cannot be null")
        return self


class StoryArcResponse(BaseModel):
    """Logical story-arc metadata plus aggregate membership counts."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    normalized_name: str
    description: str | None = None
    comicvine_id: int | None = None
    comicvine_url: str | None = None
    source_kind: StoryArcSourceKind
    lifecycle: StoryArcLifecycle
    monitored: bool
    search_missing: bool
    include_upcoming: bool
    sync_enabled: bool
    target_library_root_id: int | None = None
    revision: int
    membership_count: int = 0
    resolved_count: int = 0
    missing_count: int = 0
    conflict_count: int = 0
    created_at: datetime
    updated_at: datetime


class StoryArcMembershipCreate(BaseModel):
    """Add a resolved or unresolved ordered entry to an arc."""

    issue_id: int | None = Field(None, gt=0)
    sequence_number: int = Field(..., ge=0)
    source_ordinal: int = Field(0, ge=0)
    source_issue_number_text: str | None = Field(None, min_length=1, max_length=320)


class StoryArcMembershipUpdate(BaseModel):
    """Patch one membership's order, exact number, or skip state."""

    sequence_number: int | None = Field(None, ge=0)
    source_ordinal: int | None = Field(None, ge=0)
    source_issue_number_text: str | None = Field(None, min_length=1, max_length=320)
    intentionally_skipped: bool | None = None

    @model_validator(mode="after")
    def require_mutation(self) -> Self:
        """Reject empty membership patches."""
        if not self.model_fields_set:
            raise ValueError("At least one membership field must be provided")
        if "sequence_number" in self.model_fields_set and self.sequence_number is None:
            raise ValueError("Membership sequence_number cannot be null")
        if "source_ordinal" in self.model_fields_set and self.source_ordinal is None:
            raise ValueError("Membership source_ordinal cannot be null")
        if (
            "source_issue_number_text" in self.model_fields_set
            and self.source_issue_number_text is None
        ):
            raise ValueError("Membership source_issue_number_text cannot be null")
        if "intentionally_skipped" in self.model_fields_set and self.intentionally_skipped is None:
            raise ValueError("Membership intentionally_skipped cannot be null")
        return self


class StoryArcMembershipResolve(BaseModel):
    """Resolve an entry to an existing canonical issue."""

    issue_id: int = Field(..., gt=0)


class StoryArcMembershipReorder(BaseModel):
    """Provide the complete ordered membership identity set for an arc."""

    expected_revision: int = Field(..., ge=1)
    membership_ids: list[int] = Field(..., max_length=50_000)


class StoryArcMembershipResponse(BaseModel):
    """One durable ordered story-arc membership."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    story_arc_id: int
    issue_id: int | None = None
    sequence_number: int
    source_ordinal: int
    resolution_state: StoryArcResolutionState
    source_kind: StoryArcSourceKind
    source_issue_number_text: str | None = None
    source_series_name: str | None = None
    source_issue_title: str | None = None
    source_publisher: str | None = None
    sync_eligible: bool


class StoryArcMembershipOrderResponse(BaseModel):
    """A completed membership reorder and the arc's new revision."""

    items: list[StoryArcMembershipResponse]
    revision: int
