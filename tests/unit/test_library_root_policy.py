"""Tests for complete per-root library naming policies."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from pullbox.core.library_policy import load_effective_library_ingest_policy
from pullbox.models.config import SystemConfig
from pullbox.models.library import (
    LibraryRoot,
    LibraryRootPolicy,
    LibraryRootPolicySource,
)


def _root_policy(
    root: LibraryRoot,
    *,
    series_path_template: str,
) -> LibraryRootPolicy:
    return LibraryRootPolicy(
        library_root=root,
        schema_version=1,
        series_path_template=series_path_template,
        comic_file_template="{Series} {IssueTitle} Issue {Issue:03d}",
        annual_file_template="{Series} Annual Issue {Issue:03d}",
        non_standard_file_template="{Series} {Type} {Volume:02d} - {IssueTitle}",
        single_non_standard_file_template="{Series} {Type} - {IssueTitle}",
        replace_illegal_characters=True,
        colon_replacement="dash",
        source=LibraryRootPolicySource.MANUAL,
        revision=1,
    )


@pytest.mark.asyncio
async def test_effective_root_policy_falls_back_to_complete_global_policy(db_session) -> None:  # type: ignore[no-untyped-def]
    root = LibraryRoot(name="Global fallback", path="/library/global", enabled=True)
    db_session.add_all(
        [
            root,
            SystemConfig(
                key="series_folder_template",
                value="{Series} [{Year}]",
                value_type="string",
            ),
            SystemConfig(
                key="comic_file_template",
                value="{Series} #{Issue:04d}",
                value_type="string",
            ),
        ]
    )
    await db_session.flush()

    policy = await load_effective_library_ingest_policy(db_session, root)

    assert policy.series_path_template == "{Series} [{Year}]"
    assert policy.series_folder_template == "{Series} [{Year}]"
    assert policy.comic_file_template == "{Series} #{Issue:04d}"
    assert policy.policy_source == LibraryRootPolicySource.GLOBAL_DEFAULT
    assert policy.root_policy_id is None
    assert policy.policy_revision == 0


@pytest.mark.asyncio
async def test_effective_root_policy_isolated_to_owning_root(db_session) -> None:  # type: ignore[no-untyped-def]
    publisher_root = LibraryRoot(name="By publisher", path="/library/publisher", enabled=True)
    global_root = LibraryRoot(name="Global", path="/library/global", enabled=True)
    db_session.add_all([publisher_root, global_root])
    await db_session.flush()
    stored = _root_policy(
        publisher_root,
        series_path_template="{Publisher}/{Series} ({Year})",
    )
    db_session.add(stored)
    await db_session.flush()

    publisher_policy = await load_effective_library_ingest_policy(db_session, publisher_root)
    global_policy = await load_effective_library_ingest_policy(db_session, global_root)

    assert publisher_policy.series_path_template == "{Publisher}/{Series} ({Year})"
    assert publisher_policy.comic_file_template == ("{Series} {IssueTitle} Issue {Issue:03d}")
    assert publisher_policy.policy_source == LibraryRootPolicySource.MANUAL
    assert publisher_policy.root_policy_id == stored.id
    assert publisher_policy.policy_revision == 1
    assert global_policy.series_path_template == global_policy.series_folder_template
    assert global_policy.policy_source == LibraryRootPolicySource.GLOBAL_DEFAULT


@pytest.mark.asyncio
async def test_library_root_policy_is_unique_per_root(db_session) -> None:  # type: ignore[no-untyped-def]
    root = LibraryRoot(name="Unique", path="/library/unique", enabled=True)
    db_session.add(root)
    await db_session.flush()
    db_session.add_all(
        [
            _root_policy(root, series_path_template="{Series}"),
            _root_policy(root, series_path_template="{Publisher}/{Series}"),
        ]
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()
