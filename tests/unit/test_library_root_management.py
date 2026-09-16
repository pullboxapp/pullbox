"""Focused contracts for explicit multi-library root management."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from pullbox.core.exceptions import ValidationError
from pullbox.models.config import SystemConfig
from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcPlacement,
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
)
from pullbox.services import library_root_management as root_management
from pullbox.services.library_root_management import (
    create_library_root,
    list_library_roots,
    preview_library_root,
    preview_library_root_rebind,
    rebind_library_root,
    update_library_root,
    validate_managed_library_root,
    validate_reference_library_root,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_preview_is_write_free_and_reports_live_capabilities(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "preview-library"
    root_path.mkdir()
    before = set(root_path.iterdir())

    preview = await preview_library_root(
        db_session,
        name="Archive",
        path=str(root_path),
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=False,
    )

    assert preview["can_create"] is True
    assert preview["available"] is True
    assert preview["readable"] is True
    assert preview["writable"] is True
    assert preview["is_default_managed_destination"] is True
    assert "first managed root" in " ".join(preview["warnings"]).lower()
    assert preview["blocking_reasons"] == []
    assert set(root_path.iterdir()) == before
    assert list((await db_session.scalars(select(LibraryRoot))).all()) == []


@pytest.mark.asyncio
async def test_first_writable_root_becomes_default_and_syncs_legacy_config(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "managed-library"
    root_path.mkdir()

    state = await create_library_root(
        db_session,
        name="Managed",
        path=str(root_path),
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=False,
    )

    assert state["is_default_managed_destination"] is True
    assert state["can_disable"] is False
    config = await db_session.get(SystemConfig, "comics_directory")
    assert config is not None
    assert config.value == str(root_path)


@pytest.mark.asyncio
async def test_default_switch_is_atomic_and_old_default_can_then_be_disabled(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    first = await create_library_root(
        db_session,
        name="First",
        path=str(first_path),
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=False,
    )
    second = await create_library_root(
        db_session,
        name="Second",
        path=str(second_path),
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=False,
    )

    await update_library_root(
        db_session,
        int(second["id"]),
        {"is_default_managed_destination": True},
    )
    await update_library_root(db_session, int(first["id"]), {"enabled": False})

    roots = list((await db_session.scalars(select(LibraryRoot).order_by(LibraryRoot.id))).all())
    assert [root.is_default_managed_destination for root in roots] == [False, True]
    assert roots[0].enabled is False
    config = await db_session.get(SystemConfig, "comics_directory")
    assert config is not None
    assert config.value == str(second_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": False},
        {"allow_managed_writes": False},
        {"is_default_managed_destination": False},
    ],
)
async def test_current_default_cannot_be_disabled_or_demoted_directly(
    db_session: AsyncSession,
    tmp_path: Path,
    changes: dict[str, bool],
) -> None:
    root_path = tmp_path / f"default-{next(iter(changes))}"
    root_path.mkdir()
    state = await create_library_root(
        db_session,
        name="Default",
        path=str(root_path),
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=True,
    )

    with pytest.raises(ValidationError, match="another root"):
        await update_library_root(db_session, int(state["id"]), changes)


@pytest.mark.asyncio
async def test_mutation_is_blocked_while_an_import_is_active(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "blocked-during-import"
    root_path.mkdir()
    db_session.add(
        ImportJob(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.REVIEW,
        )
    )
    await db_session.flush()

    with pytest.raises(ValidationError, match="import is active"):
        await create_library_root(
            db_session,
            name="Blocked",
            path=str(root_path),
            allow_referenced_registrations=True,
            allow_managed_writes=True,
            is_default_managed_destination=False,
        )


@pytest.mark.asyncio
async def test_update_is_blocked_while_an_import_is_active(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "existing-before-import"
    root_path.mkdir()
    state = await create_library_root(
        db_session,
        name="Existing",
        path=str(root_path),
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=False,
    )
    db_session.add(
        ImportJob(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.IMPORTING,
        )
    )
    await db_session.flush()

    with pytest.raises(ValidationError, match="import is active"):
        await update_library_root(
            db_session,
            int(state["id"]),
            {"name": "Changed"},
        )


@pytest.mark.asyncio
async def test_name_alias_and_nested_root_conflicts_are_rejected(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    parent = tmp_path / "library"
    nested = parent / "nested"
    sibling = tmp_path / "library-sibling"
    alias = tmp_path / "library-alias"
    nested.mkdir(parents=True)
    sibling.mkdir()
    alias.symlink_to(parent, target_is_directory=True)
    await create_library_root(
        db_session,
        name="Primary",
        path=str(parent),
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=False,
    )

    for name, path, message in (
        ("primary", sibling, "name"),
        ("Nested", nested, "overlaps"),
        ("Alias", alias, "same physical directory"),
    ):
        with pytest.raises(ValidationError, match=message):
            await create_library_root(
                db_session,
                name=name,
                path=str(path),
                allow_referenced_registrations=True,
                allow_managed_writes=True,
                is_default_managed_destination=False,
            )

    sibling_state = await create_library_root(
        db_session,
        name="Sibling",
        path=str(sibling),
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=False,
    )
    assert sibling_state["path"] == str(sibling)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["relative/library", "/tmp/../tmp/library", "/etc", "/"])
async def test_unsafe_root_paths_are_rejected(
    db_session: AsyncSession,
    path: str,
) -> None:
    with pytest.raises(ValidationError):
        await preview_library_root(
            db_session,
            name="Unsafe",
            path=path,
            allow_referenced_registrations=True,
            allow_managed_writes=True,
            is_default_managed_destination=False,
        )


@pytest.mark.asyncio
async def test_create_rejects_missing_file_and_roleless_roots(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    regular_file = tmp_path / "not-a-directory"
    regular_file.write_text("fixture")

    for path in (missing, regular_file):
        with pytest.raises(ValidationError, match="existing directory"):
            await create_library_root(
                db_session,
                name=f"Invalid {path.name}",
                path=str(path),
                allow_referenced_registrations=True,
                allow_managed_writes=False,
                is_default_managed_destination=False,
            )

    roleless = tmp_path / "roleless"
    roleless.mkdir()
    with pytest.raises(ValidationError, match="at least one role"):
        await create_library_root(
            db_session,
            name="Roleless",
            path=str(roleless),
            allow_referenced_registrations=False,
            allow_managed_writes=False,
            is_default_managed_destination=False,
        )


@pytest.mark.asyncio
async def test_runtime_managed_destination_validator_reprobes_stale_state(
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "runtime-managed"
    root_path.mkdir()
    root = LibraryRoot(
        name="Runtime",
        path=str(root_path),
        enabled=True,
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=True,
    )

    capabilities = await validate_managed_library_root(root)
    assert capabilities["available"] is True
    assert capabilities["writable"] is True

    root_path.rmdir()
    with pytest.raises(ValidationError, match="existing directory"):
        await validate_managed_library_root(root)

    root.enabled = False
    with pytest.raises(ValidationError, match="disabled"):
        await validate_managed_library_root(root)


@pytest.mark.asyncio
async def test_runtime_reference_validator_accepts_reference_only_and_rejects_managed_only(
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "runtime-reference"
    root_path.mkdir()
    root = LibraryRoot(
        name="Reference archive",
        path=str(root_path),
        enabled=True,
        allow_referenced_registrations=True,
        allow_managed_writes=False,
    )

    capabilities = await validate_reference_library_root(root)
    assert capabilities["available"] is True
    assert capabilities["readable"] is True

    root.allow_referenced_registrations = False
    root.allow_managed_writes = True
    with pytest.raises(ValidationError, match="referenced registrations"):
        await validate_reference_library_root(root)


@pytest.mark.asyncio
async def test_list_surfaces_legacy_overlap_as_a_warning(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    parent = tmp_path / "legacy"
    nested = parent / "nested"
    nested.mkdir(parents=True)
    db_session.add_all(
        [
            LibraryRoot(name="Legacy", path=str(parent), enabled=True),
            LibraryRoot(name="Legacy nested", path=str(nested), enabled=True),
        ]
    )
    await db_session.flush()

    states = await list_library_roots(db_session)

    assert len(states) == 2
    assert all("overlaps" in " ".join(state["warnings"]) for state in states)


@pytest.mark.asyncio
async def test_read_only_root_is_reference_only_and_cannot_accept_managed_writes(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "read-only"
    root_path.mkdir()
    monkeypatch.setattr(root_management, "_can_write", lambda _path, *, create_probe: False)

    preview = await preview_library_root(
        db_session,
        name="Reference archive",
        path=str(root_path),
        allow_referenced_registrations=True,
        allow_managed_writes=False,
        is_default_managed_destination=False,
    )
    assert preview["can_create"] is True
    assert preview["status"] == "read_only"

    with pytest.raises(ValidationError, match="Managed writes require"):
        await create_library_root(
            db_session,
            name="Managed archive",
            path=str(root_path),
            allow_referenced_registrations=True,
            allow_managed_writes=True,
            is_default_managed_destination=False,
        )


@pytest.mark.asyncio
async def test_rebind_preview_is_write_free_and_reports_root_usage_impact(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    current_path = tmp_path / "current-library"
    replacement_path = tmp_path / "replacement-library"
    current_path.mkdir()
    replacement_path.mkdir()
    replacement_before = set(replacement_path.iterdir())
    state = await create_library_root(
        db_session,
        name="Primary",
        path=str(current_path),
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=True,
    )
    root_id = int(state["id"])
    db_session.add_all(
        [
            Series(
                title="Future one",
                sort_title="future one",
                preferred_library_root_id=root_id,
            ),
            Series(
                title="Future two",
                sort_title="future two",
                preferred_library_root_id=root_id,
            ),
        ]
    )
    await db_session.flush()

    preview = await preview_library_root_rebind(
        db_session,
        root_id,
        replacement_path=str(replacement_path),
        actor_id=7,
    )

    assert preview["can_rebind"] is True
    assert preview["preview_token"]
    assert preview["current_path"] == str(current_path)
    assert preview["replacement_path"] == str(replacement_path)
    assert preview["impact"] == {
        "library_file_count": 0,
        "series_count": 0,
        "preferred_series_count": 2,
        "story_arc_placement_count": 0,
        "library_file_blocking_count": 0,
        "series_blocking_count": 0,
        "story_arc_placement_blocking_count": 0,
        "affects_default_destination": True,
        "affects_preferred_series": True,
    }
    root = await db_session.get(LibraryRoot, root_id)
    assert root is not None
    assert root.path == str(current_path)
    assert set(replacement_path.iterdir()) == replacement_before


@pytest.mark.asyncio
async def test_rebind_preview_rejects_alias_and_overlap_with_other_roots(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    current_path = tmp_path / "current"
    other_path = tmp_path / "other"
    nested_path = other_path / "nested"
    alias_path = tmp_path / "other-alias"
    current_path.mkdir()
    nested_path.mkdir(parents=True)
    alias_path.symlink_to(other_path, target_is_directory=True)
    current = await create_library_root(
        db_session,
        name="Current",
        path=str(current_path),
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=True,
    )
    await create_library_root(
        db_session,
        name="Other",
        path=str(other_path),
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=False,
    )

    overlap = await preview_library_root_rebind(
        db_session,
        int(current["id"]),
        replacement_path=str(nested_path),
        actor_id=7,
    )
    alias = await preview_library_root_rebind(
        db_session,
        int(current["id"]),
        replacement_path=str(alias_path),
        actor_id=7,
    )

    assert overlap["can_rebind"] is False
    assert overlap["preview_token"] is None
    assert "overlaps" in " ".join(overlap["blocking_reasons"])
    assert alias["can_rebind"] is False
    assert alias["preview_token"] is None
    assert "same physical directory" in " ".join(alias["blocking_reasons"])


@pytest.mark.asyncio
async def test_rebind_preview_rejects_unsafe_replacement_path(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    current_path = tmp_path / "current"
    current_path.mkdir()
    current = await create_library_root(
        db_session,
        name="Current",
        path=str(current_path),
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=True,
    )

    with pytest.raises(ValidationError, match="absolute container-visible"):
        await preview_library_root_rebind(
            db_session,
            int(current["id"]),
            replacement_path="relative/library",
            actor_id=7,
        )


@pytest.mark.asyncio
async def test_confirmed_rebind_updates_only_root_identity_and_rechecks_capability(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    current_path = tmp_path / "current-library"
    replacement_path = tmp_path / "replacement-library"
    current_path.mkdir()
    replacement_path.mkdir()
    state = await create_library_root(
        db_session,
        name="Primary",
        path=str(current_path),
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=True,
    )
    root_id = int(state["id"])
    series = Series(
        title="Series",
        sort_title="series",
        preferred_library_root_id=root_id,
    )
    db_session.add(series)
    await db_session.flush()
    preview = await preview_library_root_rebind(
        db_session,
        root_id,
        replacement_path=str(replacement_path),
        actor_id=7,
    )

    rebound = await rebind_library_root(
        db_session,
        root_id,
        replacement_path=str(replacement_path),
        preview_token=str(preview["preview_token"]),
        actor_id=7,
    )

    assert rebound["path"] == str(replacement_path)
    assert rebound["available"] is True
    assert rebound["readable"] is True
    assert rebound["writable"] is True
    assert series.path is None
    assert series.library_root_id is None
    assert series.preferred_library_root_id == root_id
    config = await db_session.get(SystemConfig, "comics_directory")
    assert config is not None
    assert config.value == str(replacement_path)


@pytest.mark.asyncio
async def test_rebind_blocks_path_associations_outside_replacement_root(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    current_path = tmp_path / "current-library"
    replacement_path = tmp_path / "replacement-library"
    current_series_path = current_path / "Series"
    current_series_path.mkdir(parents=True)
    replacement_path.mkdir()
    library_file_path = current_series_path / "Series 001.cbz"
    placement_path = current_series_path / "Arc 001.cbz"
    library_file_path.write_bytes(b"library")
    placement_path.write_bytes(b"arc")
    state = await create_library_root(
        db_session,
        name="Primary",
        path=str(current_path),
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=True,
    )
    root_id = int(state["id"])
    arc = StoryArc(name="Arc")
    db_session.add(arc)
    await db_session.flush()
    membership = IssueStoryArc(
        story_arc_id=arc.id,
        sequence_number=1,
        source_ordinal=1,
    )
    db_session.add(membership)
    await db_session.flush()
    db_session.add_all(
        [
            Series(
                title="Series",
                sort_title="series",
                path=str(current_series_path),
                library_root_id=root_id,
            ),
            LibraryFile(
                file_path=str(library_file_path),
                file_name=library_file_path.name,
                file_size=library_file_path.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(UTC),
                library_root_id=root_id,
            ),
            StoryArcPlacement(
                issue_story_arc_id=membership.id,
                library_root_id=root_id,
                placement_path=str(placement_path),
                mode=StoryArcPlacementMode.REFERENCE_ONLY,
                ownership=StoryArcPlacementOwnership.REFERENCED,
            ),
        ]
    )
    await db_session.flush()

    preview = await preview_library_root_rebind(
        db_session,
        root_id,
        replacement_path=str(replacement_path),
        actor_id=7,
    )

    assert preview["can_rebind"] is False
    assert preview["preview_token"] is None
    assert preview["impact"]["library_file_blocking_count"] == 1
    assert preview["impact"]["series_blocking_count"] == 1
    assert preview["impact"]["story_arc_placement_blocking_count"] == 1
    assert preview["impact"]["story_arc_placement_count"] == 1
    reasons = " ".join(preview["blocking_reasons"])
    assert "path migration or repair" in reasons
    assert "library file" in reasons
    assert "series" in reasons
    assert "Story Arc placement" in reasons


@pytest.mark.asyncio
async def test_rebind_requires_live_containment_for_lexically_nested_file_paths(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    current_path = tmp_path / "current-library"
    replacement_path = tmp_path / "replacement-library"
    outside_path = tmp_path / "outside.cbz"
    current_path.mkdir()
    replacement_path.mkdir()
    outside_path.write_bytes(b"outside")
    escaped_symlink = replacement_path / "escaped.cbz"
    escaped_symlink.symlink_to(outside_path)
    missing_path = replacement_path / "missing.cbz"
    state = await create_library_root(
        db_session,
        name="Primary",
        path=str(current_path),
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=True,
    )
    root_id = int(state["id"])
    db_session.add_all(
        [
            LibraryFile(
                file_path=str(escaped_symlink),
                file_name=escaped_symlink.name,
                file_size=outside_path.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(UTC),
                library_root_id=root_id,
            ),
            LibraryFile(
                file_path=str(missing_path),
                file_name=missing_path.name,
                file_size=1,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(UTC),
                library_root_id=root_id,
            ),
        ]
    )
    await db_session.flush()

    preview = await preview_library_root_rebind(
        db_session,
        root_id,
        replacement_path=str(replacement_path),
        actor_id=7,
    )

    assert preview["can_rebind"] is False
    assert preview["impact"]["library_file_count"] == 2
    assert preview["impact"]["library_file_blocking_count"] == 2


@pytest.mark.asyncio
async def test_rebind_allows_live_path_associations_already_within_replacement(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    current_path = tmp_path / "current-library"
    replacement_path = tmp_path / "replacement-library"
    replacement_series_path = replacement_path / "Series"
    current_path.mkdir()
    replacement_series_path.mkdir(parents=True)
    library_file_path = replacement_series_path / "Series 001.cbz"
    placement_path = replacement_series_path / "Arc 001.cbz"
    library_file_path.write_bytes(b"library")
    placement_path.write_bytes(b"arc")
    state = await create_library_root(
        db_session,
        name="Primary",
        path=str(current_path),
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=True,
    )
    root_id = int(state["id"])
    arc = StoryArc(name="Arc")
    db_session.add(arc)
    await db_session.flush()
    membership = IssueStoryArc(
        story_arc_id=arc.id,
        sequence_number=1,
        source_ordinal=1,
    )
    db_session.add(membership)
    await db_session.flush()
    series = Series(
        title="Series",
        sort_title="series",
        path=str(replacement_series_path),
        library_root_id=root_id,
    )
    db_session.add_all(
        [
            series,
            LibraryFile(
                file_path=str(library_file_path),
                file_name=library_file_path.name,
                file_size=library_file_path.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(UTC),
                library_root_id=root_id,
            ),
            StoryArcPlacement(
                issue_story_arc_id=membership.id,
                library_root_id=root_id,
                placement_path=str(placement_path),
                mode=StoryArcPlacementMode.REFERENCE_ONLY,
                ownership=StoryArcPlacementOwnership.REFERENCED,
            ),
        ]
    )
    await db_session.flush()

    preview = await preview_library_root_rebind(
        db_session,
        root_id,
        replacement_path=str(replacement_path),
        actor_id=7,
    )

    assert preview["can_rebind"] is True
    assert preview["preview_token"]
    assert preview["impact"]["library_file_blocking_count"] == 0
    assert preview["impact"]["series_blocking_count"] == 0
    assert preview["impact"]["story_arc_placement_blocking_count"] == 0

    outside_series_path = current_path / "Series"
    outside_series_path.mkdir()
    series.path = str(outside_series_path)
    await db_session.flush()
    with pytest.raises(ValidationError, match="no longer safe"):
        await rebind_library_root(
            db_session,
            root_id,
            replacement_path=str(replacement_path),
            preview_token=str(preview["preview_token"]),
            actor_id=7,
        )


@pytest.mark.asyncio
async def test_rebind_confirmation_rejects_tampering_and_preview_drift(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    current_path = tmp_path / "current-library"
    replacement_path = tmp_path / "replacement-library"
    other_path = tmp_path / "other-library"
    current_path.mkdir()
    replacement_path.mkdir()
    other_path.mkdir()
    state = await create_library_root(
        db_session,
        name="Primary",
        path=str(current_path),
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=True,
    )
    root_id = int(state["id"])
    preview = await preview_library_root_rebind(
        db_session,
        root_id,
        replacement_path=str(replacement_path),
        actor_id=7,
    )
    token = str(preview["preview_token"])
    tampered_token = ("A" if token[0] != "A" else "B") + token[1:]

    with pytest.raises(ValidationError, match="invalid"):
        await rebind_library_root(
            db_session,
            root_id,
            replacement_path=str(replacement_path),
            preview_token=tampered_token,
            actor_id=7,
        )

    with pytest.raises(ValidationError, match=r"invalid|match"):
        await rebind_library_root(
            db_session,
            root_id,
            replacement_path=str(other_path),
            preview_token=token,
            actor_id=7,
        )

    root = await db_session.get(LibraryRoot, root_id)
    assert root is not None
    root.name = "Changed after preview"
    await db_session.flush()
    with pytest.raises(ValidationError, match="changed"):
        await rebind_library_root(
            db_session,
            root_id,
            replacement_path=str(replacement_path),
            preview_token=token,
            actor_id=7,
        )
    assert root.path == str(current_path)
