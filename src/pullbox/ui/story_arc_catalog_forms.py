"""Validated browser fields for reviewing and adopting a provider Story Arc."""

from typing import Literal

from pydantic import BaseModel, Field

from pullbox.core.story_arc_naming import (
    DEFAULT_STORY_ARC_FILE_TEMPLATE,
    DEFAULT_STORY_ARC_FOLDER_TEMPLATE,
    ORIGINAL_STORY_ARC_FILE_TEMPLATE,
)
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementIntegrationError,
    StoryArcPlacementPolicyInput,
    StoryArcPlacementPolicyMode,
)
from pullbox.services.story_arc_service import StoryArcValidationError


class StoryArcCatalogAddForm(BaseModel):
    """Require a complete reviewed order and separate canonical/placement roots."""

    fingerprint: str = Field(max_length=128)
    order_reviewed: bool = False
    issue_provider_ids: list[str] = Field(default_factory=list, max_length=2000)
    reading_orders: list[int] = Field(default_factory=list, max_length=2000)
    skipped_issue_provider_ids: list[str] = Field(default_factory=list, max_length=2000)
    library_root_id: int = Field(ge=1)
    monitored: bool = False
    search_missing: bool = False
    include_upcoming: bool = False
    mode: str = Field(default="logical", max_length=50)
    target_library_root_id: str = ""
    destination_root: str = Field(default="", max_length=1000)
    folder_template: str = Field(default=DEFAULT_STORY_ARC_FOLDER_TEMPLATE, max_length=1024)
    filename_style: Literal["original", "custom"] = "original"
    prefix_reading_order: bool = False
    reading_order_width: int = Field(default=2, ge=2, le=6)
    file_template: str = Field(default=DEFAULT_STORY_ARC_FILE_TEMPLATE, max_length=1024)
    symlink_style: str = Field(default="", max_length=50)
    synchronize: bool = False

    def reviewed_order(self) -> list[str]:
        """Every member must have one distinct positive position before adoption."""
        if not self.order_reviewed:
            raise StoryArcValidationError("Review and confirm the reading order before adding.")
        if (
            not self.issue_provider_ids
            or len(self.issue_provider_ids) != len(self.reading_orders)
            or len(set(self.issue_provider_ids)) != len(self.issue_provider_ids)
            or len(set(self.reading_orders)) != len(self.reading_orders)
            or any(order < 1 for order in self.reading_orders)
        ):
            raise StoryArcValidationError("Give every member a different positive reading order.")
        return [
            provider_id
            for _, provider_id in sorted(
                zip(self.reading_orders, self.issue_provider_ids, strict=True)
            )
        ]

    def placement_policy(self) -> StoryArcPlacementPolicyInput:
        """Preserve existing templates while defaulting new copies to current filenames."""
        try:
            mode = StoryArcPlacementPolicyMode(self.mode)
            root_id = int(self.target_library_root_id) if self.target_library_root_id else None
        except ValueError as exc:
            raise StoryArcPlacementIntegrationError(
                "invalid_policy", "Review the storage choices."
            ) from exc
        file_template = self.file_template
        if self.filename_style == "original":
            file_template = ORIGINAL_STORY_ARC_FILE_TEMPLATE
            if self.prefix_reading_order:
                file_template = f"{{ReadingOrder:0{self.reading_order_width}d}} - {file_template}"
        logical = mode is StoryArcPlacementPolicyMode.LOGICAL
        return StoryArcPlacementPolicyInput(
            mode=mode,
            target_library_root_id=None if logical else root_id,
            destination_root=None if logical else self.destination_root.strip() or None,
            folder_template=self.folder_template,
            file_template=file_template,
            symlink_style=(self.symlink_style or None)
            if mode is StoryArcPlacementPolicyMode.SYMLINK
            else None,
            synchronize=self.synchronize and not logical,
        )
