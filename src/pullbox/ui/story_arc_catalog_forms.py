"""Validated browser fields for reviewing and adopting a provider Story Arc."""

from pydantic import BaseModel, Field

from pullbox.services.story_arc_service import StoryArcValidationError


class StoryArcCatalogAddForm(BaseModel):
    """Require a reviewed order, canonical root and global file-defaults acknowledgement."""

    fingerprint: str = Field(max_length=128)
    file_defaults_fingerprint: str = Field(default="", max_length=128)
    order_reviewed: bool = False
    issue_provider_ids: list[str] = Field(default_factory=list, max_length=2000)
    reading_orders: list[int] = Field(default_factory=list, max_length=2000)
    skipped_issue_provider_ids: list[str] = Field(default_factory=list, max_length=2000)
    library_root_id: int = Field(ge=1)
    monitored: bool = False
    search_missing: bool = False
    include_upcoming: bool = False

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
