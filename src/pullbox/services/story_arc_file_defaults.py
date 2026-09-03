"""Global file defaults for new arcs; existing/imported policies never inherit live settings."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from pullbox.core.story_arc_naming import StoryArcNamingValues, render_story_arc_relative_path
from pullbox.models.config import DEFAULT_SYSTEM_CONFIG, SystemConfig
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementIntegrationError,
    StoryArcPlacementPolicyInput,
    validate_story_arc_placement_policy_input,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

STORY_ARC_FILE_DEFAULT_KEYS = tuple(
    key for key in DEFAULT_SYSTEM_CONFIG if key.startswith("story_arc_files_")
)


class StoryArcFileDefaults(BaseModel):
    """Editable preferences, separate from the complete effective per-arc snapshot."""

    enabled: bool
    method: Literal["copy", "hardlink", "symlink"]
    library_root_id: str = Field(max_length=20)
    destination: str = Field(max_length=1000)
    folder_template: str = Field(max_length=1024)
    filename_style: Literal["original", "custom"]
    prefix_reading_order: bool
    reading_order_width: int = Field(ge=2, le=6)
    file_template: str = Field(max_length=1024)
    symlink_style: Literal["relative", "absolute"]
    synchronize: bool

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()

    @property
    def summary_label(self) -> str:
        if not self.enabled:
            return "No separate folder"
        return {
            "copy": "Copy issues into arc folders",
            "hardlink": "Hardlink issues into arc folders",
            "symlink": "Symlink issues into arc folders",
        }[self.method]

    def proposal(self) -> StoryArcPlacementPolicyInput:
        template = self.file_template
        if self.filename_style == "original":
            template = "{OriginalFilename}"
            if self.prefix_reading_order:
                template = f"{{ReadingOrder:0{self.reading_order_width}d}} - {template}"
        try:
            root_id = int(self.library_root_id) if self.library_root_id else None
        except ValueError as exc:
            raise StoryArcPlacementIntegrationError(
                "invalid_root", "Choose a valid Story Arc library root."
            ) from exc
        return StoryArcPlacementPolicyInput(
            mode=self.method if self.enabled else "logical",
            target_library_root_id=root_id if self.enabled else None,
            destination_root=(self.destination.strip() or None) if self.enabled else None,
            folder_template=self.folder_template,
            file_template=template,
            symlink_style=self.symlink_style if self.enabled and self.method == "symlink" else None,
            synchronize=self.enabled and self.synchronize,
        )

    def naming_preview(self) -> str:
        proposal = self.proposal()
        return str(
            render_story_arc_relative_path(
                StoryArcNamingValues(
                    story_arc="The Court of Owls",
                    reading_order=1,
                    series="Batman",
                    issue_number="001",
                    extension="cbz",
                    issue_title="Knife Trick",
                    year=2011,
                    start_year=2011,
                    end_year=2012,
                    publisher="DC Comics",
                    original_filename="Batman 001.cbz",
                ),
                folder_template=proposal.folder_template,
                file_template=proposal.file_template,
            )
        )


def parse_story_arc_file_defaults(values: dict[str, str]) -> StoryArcFileDefaults:
    try:
        defaults = StoryArcFileDefaults.model_validate(
            {
                key.removeprefix("story_arc_files_"): values.get(key, DEFAULT_SYSTEM_CONFIG[key][0])
                for key in STORY_ARC_FILE_DEFAULT_KEYS
            }
        )
        defaults.naming_preview()
        return defaults
    except ValidationError as exc:
        error = exc.errors()[0]
        field = str(error["loc"][0]).replace("_", " ")
        raise StoryArcPlacementIntegrationError(
            "invalid_defaults", f"Story Arc {field}: {error['msg']}"
        ) from exc
    except ValueError as exc:
        raise StoryArcPlacementIntegrationError(
            "invalid_defaults", "Invalid Story Arc file defaults. " + str(exc)
        ) from exc


async def load_story_arc_file_defaults(session: AsyncSession) -> StoryArcFileDefaults:
    rows = await session.scalars(
        select(SystemConfig).where(SystemConfig.key.in_(STORY_ARC_FILE_DEFAULT_KEYS))
    )
    return parse_story_arc_file_defaults({row.key: row.value for row in rows})


async def validate_story_arc_file_defaults(session: AsyncSession, values: dict[str, str]) -> None:
    defaults = parse_story_arc_file_defaults(values)
    await validate_story_arc_placement_policy_input(session, defaults.proposal(), revision=1)
