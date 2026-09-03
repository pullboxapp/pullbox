"""Unified naming editor over the existing global and library policy stores."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from sqlalchemy import select

from pullbox.core.exceptions import PullboxError, ValidationError
from pullbox.core.library_policy import load_library_naming_policy
from pullbox.core.naming import get_naming_preview
from pullbox.models.config import DEFAULT_SYSTEM_CONFIG, SystemConfig
from pullbox.models.library import LibraryRoot
from pullbox.schemas.config import (
    LibraryRootPolicyState,
    NamingSettingsPreview,
    NamingSettingsState,
)
from pullbox.schemas.import_job import FutureRootPolicyPayload
from pullbox.services.import_root_policy_activation import normalize_root_policy_definition
from pullbox.services.library_root_policy_service import (
    clear_library_root_policy,
    get_library_root_policy_state,
    update_library_root_policy,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.schemas.config import NamingSettingsUpdate

_PREVIEW_TYPES = {
    "series_path_template": "folder",
    "comic_file_template": "standard",
    "annual_file_template": "annual",
    "non_standard_file_template": "non_standard_collection",
    "single_non_standard_file_template": "non_standard_single",
}
_CONFIG_KEYS = {
    **{field: field for field in _PREVIEW_TYPES},
    "series_path_template": "series_folder_template",
    "replace_illegal_characters": "replace_illegal_characters",
    "colon_replacement": "colon_replacement",
}


async def get_naming_settings(
    session: AsyncSession, library_root_id: int | None = None
) -> NamingSettingsState:
    """Read current effective values, including provenance in the stale-edit token."""
    revision = 0
    source = "global_default"
    if library_root_id is None:
        current = await load_library_naming_policy(session)
        definition = {field: getattr(current, key) for field, key in _CONFIG_KEYS.items()}
        definition["schema_version"] = 1
        identity: object = None
        use_global = False
    else:
        state = LibraryRootPolicyState.model_validate(
            await get_library_root_policy_state(session, library_root_id)
        )
        definition = state.effective_policy.model_dump()
        revision = state.revision
        source = str(definition["source"])
        identity = (state.policy_id, revision, source, definition["source_import_job_id"])
        use_global = state.scope == "global_default"
    policy = FutureRootPolicyPayload.model_validate(definition)
    fingerprint = hashlib.sha256(
        json.dumps([library_root_id, identity, policy.model_dump()], sort_keys=True).encode("utf-8")
    ).hexdigest()
    return NamingSettingsState(
        library_root_id=library_root_id,
        fingerprint=fingerprint,
        policy=policy,
        use_global=use_global,
        revision=revision,
        source=source,
    )


async def save_naming_settings(
    session: AsyncSession, update: NamingSettingsUpdate
) -> NamingSettingsState:
    """Save just naming; preserve import snapshots and existing revision guards."""
    if update.library_root_id is None and update.use_global:
        raise ValidationError("Select a library before restoring global naming defaults.")
    # Lock existing rows before comparing. Root helpers retain their revision check.
    await session.scalars(
        select(SystemConfig)
        .where(SystemConfig.key.in_(_CONFIG_KEYS.values()))
        .order_by(SystemConfig.key)
        .with_for_update()
    )
    if update.library_root_id is not None:
        await session.scalar(
            select(LibraryRoot).where(LibraryRoot.id == update.library_root_id).with_for_update()
        )
    current = await get_naming_settings(session, update.library_root_id)
    if current.fingerprint != update.expected_fingerprint:
        raise PullboxError(
            message=(
                "Naming settings changed after you loaded them. Reload this scope and try again."
            ),
            code="NAMING_SETTINGS_CONFLICT",
            status_code=409,
        )
    if update.library_root_id is not None:
        if update.use_global:
            await clear_library_root_policy(
                session, update.library_root_id, expected_revision=current.revision
            )
        else:
            await update_library_root_policy(
                session,
                update.library_root_id,
                expected_revision=current.revision,
                definition=update.policy.model_dump(),
            )
    else:
        definition = normalize_root_policy_definition(update.policy.model_dump())
        for field, key in _CONFIG_KEYS.items():
            value = definition[field]
            text = str(value).lower() if isinstance(value, bool) else str(value)
            row = await session.get(SystemConfig, key)
            if row is None:
                session.add(
                    SystemConfig(key=key, value=text, value_type=DEFAULT_SYSTEM_CONFIG[key][1])
                )
            else:
                row.value = text
        await session.flush()
    return await get_naming_settings(session, update.library_root_id)


def preview_naming_settings(policy: FutureRootPolicyPayload) -> NamingSettingsPreview:
    """Use identical validation and cleanup for both scopes, without any writes."""
    definition = normalize_root_policy_definition(policy.model_dump())
    return NamingSettingsPreview(
        examples={
            field: get_naming_preview(
                str(definition[field]),
                template_type,
                replace_illegal=policy.replace_illegal_characters,
                colon_replacement=policy.colon_replacement,
            )
            for field, template_type in _PREVIEW_TYPES.items()
        }
    )
