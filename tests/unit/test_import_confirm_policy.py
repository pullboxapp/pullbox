"""Tests for confirm-import policy helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pullbox.core.exceptions import ValidationError
from pullbox.models.config import SystemConfig
from pullbox.models.import_job import (
    ImportFileHandlingMode,
    ImportJob,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.schemas.import_job import ConfirmImportRequest
from pullbox.services.import_confirm_policy import apply_confirm_import_policy

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _make_job() -> ImportJob:
    return ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
        monitored=False,
    )


async def test_apply_confirm_policy_uses_global_search_on_add(
    db_session: AsyncSession,
) -> None:
    db_session.add(SystemConfig(key="search_on_add_default", value="true", value_type="bool"))
    await db_session.flush()
    job = _make_job()

    await apply_confirm_import_policy(
        db_session,
        job,
        ConfirmImportRequest(series_ids=[1], monitored=False, target_library_root_id=7),
    )

    assert job.search_on_add is True
    assert job.monitored is True
    assert job.target_library_root_id == 7
    assert job.move_to_library is True


async def test_apply_confirm_policy_rejects_conflicting_search_override(
    db_session: AsyncSession,
) -> None:
    db_session.add(SystemConfig(key="search_on_add_default", value="false", value_type="bool"))
    await db_session.flush()

    with pytest.raises(ValidationError, match="global import policy"):
        await apply_confirm_import_policy(
            db_session,
            _make_job(),
            ConfirmImportRequest(series_ids=[1], search_on_add=True),
        )


async def test_apply_confirm_policy_rejects_deprecated_move_override(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(ValidationError, match="no longer supported"):
        await apply_confirm_import_policy(
            db_session,
            _make_job(),
            ConfirmImportRequest(series_ids=[1], move_to_library=False),
        )


async def test_apply_confirm_policy_persists_ingest_defaults(
    db_session: AsyncSession,
) -> None:
    db_session.add(SystemConfig(key="post_processing_method", value="copy", value_type="string"))
    db_session.add(
        SystemConfig(
            key="convert_to_preferred_format_on_import",
            value="true",
            value_type="bool",
        )
    )
    db_session.add(
        SystemConfig(
            key="update_embedded_comicinfo_from_match_on_import",
            value="true",
            value_type="bool",
        )
    )
    await db_session.flush()
    job = _make_job()

    await apply_confirm_import_policy(
        db_session,
        job,
        ConfirmImportRequest(
            series_ids=[1],
            update_embedded_comicinfo_from_match=True,
        ),
    )

    assert job.transfer_method == "copy"
    assert job.convert_to_preferred_format is True
    assert job.update_embedded_comicinfo_from_match is True


async def test_apply_confirm_policy_preserves_in_place_no_mutation_snapshot(
    db_session: AsyncSession,
) -> None:
    db_session.add_all(
        [
            SystemConfig(
                key="convert_to_preferred_format_on_import",
                value="true",
                value_type="bool",
            ),
            SystemConfig(
                key="update_embedded_comicinfo_from_match_on_import",
                value="true",
                value_type="bool",
            ),
        ]
    )
    await db_session.flush()
    job = _make_job()
    job.file_handling_mode = ImportFileHandlingMode.IN_PLACE

    await apply_confirm_import_policy(
        db_session,
        job,
        ConfirmImportRequest(series_ids=[1]),
    )

    assert job.move_to_library is False
    assert job.effective_transfer_method == "leave_in_place"
    assert job.source_preserved is True
    assert job.convert_to_preferred_format is False
    assert job.update_embedded_comicinfo_from_match is False


async def test_apply_confirm_policy_rejects_managed_legacy_override_for_in_place(
    db_session: AsyncSession,
) -> None:
    job = _make_job()
    job.file_handling_mode = ImportFileHandlingMode.IN_PLACE

    with pytest.raises(ValidationError, match="conflicts with the selected in-place mode"):
        await apply_confirm_import_policy(
            db_session,
            job,
            ConfirmImportRequest(series_ids=[1], move_to_library=True),
        )


async def test_apply_confirm_policy_allows_convert_with_source_preserving_collection_import(
    db_session: AsyncSession,
) -> None:
    db_session.add(
        SystemConfig(key="post_processing_method", value="hardlink", value_type="string")
    )
    db_session.add(
        SystemConfig(
            key="convert_to_preferred_format_on_import",
            value="true",
            value_type="bool",
        )
    )
    await db_session.flush()
    job = _make_job()

    await apply_confirm_import_policy(
        db_session,
        job,
        ConfirmImportRequest(series_ids=[1]),
    )

    assert job.transfer_method == "hardlink"
    assert job.effective_transfer_method == "copy"
    assert job.source_preserved is True
    assert job.convert_to_preferred_format is True


async def test_apply_confirm_policy_allows_comicinfo_update_with_source_preserving_import(
    db_session: AsyncSession,
) -> None:
    db_session.add(SystemConfig(key="post_processing_method", value="symlink", value_type="string"))
    db_session.add(
        SystemConfig(
            key="update_embedded_comicinfo_from_match_on_import",
            value="true",
            value_type="bool",
        )
    )
    await db_session.flush()
    job = _make_job()

    await apply_confirm_import_policy(
        db_session,
        job,
        ConfirmImportRequest(
            series_ids=[1],
            update_embedded_comicinfo_from_match=True,
        ),
    )

    assert job.transfer_method == "symlink"
    assert job.effective_transfer_method == "copy"
    assert job.source_preserved is True
    assert job.update_embedded_comicinfo_from_match is True
