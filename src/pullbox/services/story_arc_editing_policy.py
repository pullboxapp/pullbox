"""Release gate for user-authored overrides, separate from provider/import writes."""

from pullbox.config import get_settings
from pullbox.models.story_arc import StoryArc, StoryArcSourceKind
from pullbox.services.story_arc_service import StoryArcServiceError


class StoryArcManualEditingDisabledError(StoryArcServiceError):
    """Provider metadata and membership cannot currently be edited manually."""


def can_manually_edit_arc(arc: StoryArc) -> bool:
    """Keep unlinked imported/custom lists editable, even when creation is off."""
    provider_managed = (
        arc.source_kind == StoryArcSourceKind.PROVIDER or arc.comicvine_id is not None
    )
    return not provider_managed or get_settings().story_arc_manual_edit_enabled


def require_manual_arc_edit(arc: StoryArc) -> None:
    """Check user-facing writes without blocking catalog refresh or imports."""
    if not can_manually_edit_arc(arc):
        raise StoryArcManualEditingDisabledError(
            "This story arc is managed by its metadata provider. Manual metadata and membership "
            "edits are not enabled. Monitoring, reading order, and storage settings "
            "remain available."
        )
