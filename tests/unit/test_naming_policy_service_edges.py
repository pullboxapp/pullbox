"""Service-level coverage for naming scope persistence and revision guards."""

from __future__ import annotations

import pytest

from pullbox.core.exceptions import PullboxError, ValidationError
from pullbox.models.library import LibraryRoot
from pullbox.schemas.config import NamingSettingsUpdate
from pullbox.services.library_root_policy_service import (
    LibraryRootNotFoundError,
    clear_library_root_policy,
    preview_library_root_policy,
    update_library_root_policy,
)
from pullbox.services.naming_settings import get_naming_settings, save_naming_settings


@pytest.mark.asyncio
async def test_global_naming_save_persists_complete_policy_and_rejects_stale_edits(
    db_session,
) -> None:
    current = await get_naming_settings(db_session)
    updated_policy = current.policy.model_copy(
        update={"series_path_template": "{Publisher}/{Series} ({Year})"}
    )
    saved = await save_naming_settings(
        db_session,
        NamingSettingsUpdate(
            expected_fingerprint=current.fingerprint,
            policy=updated_policy,
        ),
    )
    assert saved.policy.series_path_template == "{Publisher}/{Series} ({Year})"

    with pytest.raises(PullboxError, match="changed after you loaded"):
        await save_naming_settings(
            db_session,
            NamingSettingsUpdate(
                expected_fingerprint=current.fingerprint,
                policy=current.policy,
            ),
        )
    with pytest.raises(ValidationError, match="Select a library"):
        await save_naming_settings(
            db_session,
            NamingSettingsUpdate(
                expected_fingerprint=saved.fingerprint,
                policy=saved.policy,
                use_global=True,
            ),
        )


@pytest.mark.asyncio
async def test_library_naming_override_updates_then_returns_to_global(db_session, tmp_path) -> None:
    root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=True)
    db_session.add(root)
    await db_session.flush()

    inherited = await get_naming_settings(db_session, root.id)
    custom_policy = inherited.policy.model_copy(
        update={"comic_file_template": "{Series} #{Issue:03d}"}
    )
    custom = await save_naming_settings(
        db_session,
        NamingSettingsUpdate(
            library_root_id=root.id,
            expected_fingerprint=inherited.fingerprint,
            policy=custom_policy,
        ),
    )
    assert custom.use_global is False
    assert custom.revision == 1

    revised_policy = custom.policy.model_copy(update={"colon_replacement": "space"})
    revised = await save_naming_settings(
        db_session,
        NamingSettingsUpdate(
            library_root_id=root.id,
            expected_fingerprint=custom.fingerprint,
            policy=revised_policy,
        ),
    )
    assert revised.revision == 2
    assert revised.source == "manual"

    restored = await save_naming_settings(
        db_session,
        NamingSettingsUpdate(
            library_root_id=root.id,
            expected_fingerprint=revised.fingerprint,
            policy=revised.policy,
            use_global=True,
        ),
    )
    assert restored.use_global is True
    assert restored.revision == 0


@pytest.mark.asyncio
async def test_root_policy_preview_handles_invalid_examples_and_missing_roots(
    db_session, tmp_path
) -> None:
    root = LibraryRoot(name="Preview", path=str(tmp_path), enabled=True)
    db_session.add(root)
    await db_session.flush()
    global_state = await get_naming_settings(db_session)

    preview = await preview_library_root_policy(
        db_session,
        root.id,
        definition=global_state.policy.model_dump(),
        examples=[
            {"series": "", "issue_number": 1},
            {"series": "Batman", "issue_number": True},
        ],
    )
    assert preview["current_scope"] == "global_default"
    assert preview["proposed_series_paths"]

    with pytest.raises(LibraryRootNotFoundError):
        await preview_library_root_policy(
            db_session,
            999999,
            definition=global_state.policy.model_dump(),
        )


@pytest.mark.asyncio
async def test_root_policy_low_level_revision_contract(db_session, tmp_path) -> None:
    root = LibraryRoot(name="Direct", path=str(tmp_path), enabled=True)
    db_session.add(root)
    await db_session.flush()
    definition = (await get_naming_settings(db_session)).policy.model_dump()

    created = await update_library_root_policy(
        db_session,
        root.id,
        expected_revision=0,
        definition=definition,
    )
    assert created["revision"] == 1

    with pytest.raises(PullboxError, match="changed after it was loaded"):
        await update_library_root_policy(
            db_session,
            root.id,
            expected_revision=0,
            definition=definition,
        )

    cleared = await clear_library_root_policy(db_session, root.id, expected_revision=1)
    assert cleared["scope"] == "global_default"
