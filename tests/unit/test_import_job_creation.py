"""Tests for import job creation helpers."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select

from pullbox.core.exceptions import ValidationError
from pullbox.models.config import SystemConfig
from pullbox.models.import_job import (
    ImportFileHandlingMode,
    ImportJob,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.models.library import (
    LibraryRoot,
    LibraryRootPolicy,
    LibraryRootPolicySource,
)
from pullbox.models.story_arc_import import ImportedStoryArc
from pullbox.schemas.import_job import FutureRootPolicyPayload, ImportJobCreate, ImportJobRead
from pullbox.schemas.import_layout import SourceLayoutSpecPayload
from pullbox.services.import_job_creation import create_job

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


async def _log_event(
    _session: AsyncSession,
    _job_id: int,
    _level: str,
    _event: str,
    message: str | None = None,
    **kwargs: Any,
) -> None:
    _ = message, kwargs


@pytest.fixture(autouse=True)
async def default_managed_root(
    db_session: AsyncSession,
    tmp_path: Path,
) -> LibraryRoot:
    """Model the startup invariant that every configured install has a default root."""
    root_path = tmp_path / "default-managed-root"
    root_path.mkdir(exist_ok=True)
    root = LibraryRoot(
        name="Default managed root",
        path=str(root_path),
        enabled=True,
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=True,
    )
    db_session.add(root)
    await db_session.flush()
    return root


async def test_create_job_uses_global_search_on_add_and_logs_event(
    db_session: AsyncSession,
    tmp_path: object,
) -> None:
    db_session.add(SystemConfig(key="search_on_add_default", value="true", value_type="bool"))
    await db_session.flush()
    events: list[tuple[str, dict[str, Any]]] = []

    async def log_event(
        _session: AsyncSession,
        _job_id: int,
        _level: str,
        event: str,
        message: str | None = None,
        **kwargs: Any,
    ) -> None:
        events.append((event, {"message": message, **kwargs}))

    request = ImportJobCreate(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
        monitored=False,
    )

    job = await create_job(db_session, request, log_event=log_event)

    assert job.status == ImportJobStatus.PENDING
    assert job.monitored is True
    assert job.search_on_add is True
    assert job.source_path == str(tmp_path)
    assert job.progress_snapshot["phase"] == "inventory"
    assert job.ingest_policy_snapshot["post_processing_method"] == "move"
    assert "comic_file_template" in job.ingest_policy_snapshot
    assert job.transfer_method == "move"
    assert job.effective_transfer_method == "copy"
    assert job.source_preserved is True
    assert job.file_handling_mode == ImportFileHandlingMode.MANAGED_COPY
    assert job.source_layout_snapshot == SourceLayoutSpecPayload().model_dump(mode="json")
    assert job.future_layout_requested is False
    assert job.future_root_policy_snapshot is None
    assert job.future_root_policy_applied_at is None
    assert job.story_arc_import_requested is False
    assert job.story_arc_materialization_requested is False

    response = ImportJobRead.model_validate(job)
    assert response.file_handling_mode == ImportFileHandlingMode.MANAGED_COPY
    assert response.source_layout_snapshot == SourceLayoutSpecPayload()
    assert response.future_layout_requested is False
    assert response.future_root_policy_snapshot is None
    assert response.future_root_policy_applied_at is None
    assert response.story_arc_import_requested is False
    assert response.story_arc_materialization_requested is False
    assert events == [
        (
            "import_job_created",
            {
                "message": "Import job created for filesystem source",
                "source_path": str(tmp_path),
                "selected_file_count": 0,
            },
        )
    ]


async def test_create_job_persists_story_arc_intent_without_authorizing_review_rows(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    request = ImportJobCreate(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
        story_arc_import_requested=True,
        story_arc_materialization_requested=True,
    )

    job = await create_job(db_session, request, log_event=_log_event)

    assert job.story_arc_import_requested is True
    assert job.story_arc_materialization_requested is True
    assert (
        await db_session.scalar(
            select(func.count(ImportedStoryArc.id)).where(ImportedStoryArc.import_job_id == job.id)
        )
        == 0
    )
    response = ImportJobRead.model_validate(job)
    assert response.story_arc_import_requested is True
    assert response.story_arc_materialization_requested is True


async def test_create_job_freezes_selected_root_policy(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "library"
    root_path.mkdir()
    root = LibraryRoot(name="Publisher layout", path=str(root_path), enabled=True)
    root_policy = LibraryRootPolicy(
        library_root=root,
        schema_version=1,
        series_path_template="{Publisher}/{Series} ({Year})",
        comic_file_template="{Series} {IssueTitle} Issue {Issue:03d}",
        annual_file_template="{Series} Annual Issue {Issue:03d}",
        non_standard_file_template="{Series} {Type} {Volume:02d} - {IssueTitle}",
        single_non_standard_file_template="{Series} {Type} - {IssueTitle}",
        replace_illegal_characters=True,
        colon_replacement="dash",
        source=LibraryRootPolicySource.MANUAL,
        revision=3,
    )
    db_session.add_all([root, root_policy])
    await db_session.flush()

    job = await create_job(
        db_session,
        ImportJobCreate(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
            target_library_root_id=root.id,
        ),
        log_event=_log_event,
    )

    assert job.ingest_policy_snapshot["series_path_template"] == ("{Publisher}/{Series} ({Year})")
    assert job.ingest_policy_snapshot["comic_file_template"] == (
        "{Series} {IssueTitle} Issue {Issue:03d}"
    )
    assert job.ingest_policy_snapshot["root_policy_id"] == root_policy.id
    assert job.ingest_policy_snapshot["policy_source"] == "manual"
    assert job.ingest_policy_snapshot["policy_revision"] == 3


async def test_create_job_uses_live_default_managed_destination(
    db_session: AsyncSession,
    tmp_path: Path,
    default_managed_root: LibraryRoot,
) -> None:
    source_path = tmp_path / "imports"
    source_path.mkdir()

    job = await create_job(
        db_session,
        ImportJobCreate(
            source_path=str(source_path),
            source_type=ImportSourceType.FILESYSTEM,
        ),
        log_event=_log_event,
    )

    assert job.target_library_root_id == default_managed_root.id


async def test_create_managed_copy_job_rejects_missing_managed_destination(
    db_session: AsyncSession,
    tmp_path: Path,
    default_managed_root: LibraryRoot,
) -> None:
    source_path = tmp_path / "imports-without-destination"
    source_path.mkdir()
    await db_session.delete(default_managed_root)
    await db_session.flush()

    with pytest.raises(ValidationError, match="No managed library destination"):
        await create_job(
            db_session,
            ImportJobCreate(
                source_path=str(source_path),
                source_type=ImportSourceType.FILESYSTEM,
                file_handling_mode=ImportFileHandlingMode.MANAGED_COPY,
            ),
            log_event=_log_event,
        )


async def test_create_in_place_job_does_not_require_optional_managed_destination(
    db_session: AsyncSession,
    tmp_path: Path,
    default_managed_root: LibraryRoot,
) -> None:
    await db_session.delete(default_managed_root)
    await db_session.flush()
    reference_path = tmp_path / "reference-library"
    source_path = reference_path / "Existing Layout"
    source_path.mkdir(parents=True)
    reference_root = LibraryRoot(
        name="Reference library",
        path=str(reference_path),
        enabled=True,
        allow_referenced_registrations=True,
        allow_managed_writes=False,
    )
    db_session.add(reference_root)
    await db_session.flush()

    job = await create_job(
        db_session,
        ImportJobCreate(
            source_path=str(source_path),
            source_type=ImportSourceType.FILESYSTEM,
            file_handling_mode=ImportFileHandlingMode.IN_PLACE,
        ),
        log_event=_log_event,
    )

    assert job.file_handling_mode == ImportFileHandlingMode.IN_PLACE
    assert job.target_library_root_id is None


async def test_create_job_rejects_reference_only_managed_destination(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    reference_path = tmp_path / "reference-only"
    reference_path.mkdir()
    source_path = tmp_path / "imports"
    source_path.mkdir()
    reference_root = LibraryRoot(
        name="Reference only",
        path=str(reference_path),
        enabled=True,
        allow_referenced_registrations=True,
        allow_managed_writes=False,
    )
    db_session.add(reference_root)
    await db_session.flush()

    with pytest.raises(ValidationError, match="managed writes"):
        await create_job(
            db_session,
            ImportJobCreate(
                source_path=str(source_path),
                source_type=ImportSourceType.FILESYSTEM,
                target_library_root_id=reference_root.id,
            ),
            log_event=_log_event,
        )


async def test_create_job_rejects_conflicting_compat_search_on_add(
    db_session: AsyncSession,
    tmp_path: object,
) -> None:
    db_session.add(SystemConfig(key="search_on_add_default", value="true", value_type="bool"))
    await db_session.flush()
    request = ImportJobCreate(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
        search_on_add=False,
    )

    with pytest.raises(ValidationError, match="global import policy"):
        await create_job(db_session, request, log_event=_log_event)


async def test_create_job_rejects_existing_active_import(
    db_session: AsyncSession,
    tmp_path: object,
) -> None:
    db_session.add(
        ImportJob(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.REVIEW,
        )
    )
    await db_session.flush()
    request = ImportJobCreate(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
    )

    with pytest.raises(ValidationError, match="Only one import can be active"):
        await create_job(db_session, request, log_event=_log_event)


async def test_create_job_accepts_confirmed_mylar_map_inside_enabled_root(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mylar.db"
    library_path = tmp_path / "library"
    (library_path / "Series").mkdir(parents=True)
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE comics (ComicLocation TEXT)")
    connection.execute(
        "INSERT INTO comics (ComicLocation) VALUES (?)",
        ("/mylar/comics/Series",),
    )
    connection.commit()
    connection.close()
    root = LibraryRoot(name="Existing library", path=str(library_path), enabled=True)
    db_session.add(root)
    await db_session.flush()

    job = await create_job(
        db_session,
        ImportJobCreate(
            source_path=str(db_path),
            source_type=ImportSourceType.MYLAR3,
            mylar3_path_map={"/mylar/comics": str(library_path)},
            mylar3_path_map_confirmed=True,
        ),
        log_event=_log_event,
    )

    assert job.mylar3_path_map == {"/mylar/comics": str(library_path)}


async def test_create_job_accepts_managed_copy_mylar_map_outside_library_roots(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mylar.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE comics (ComicLocation TEXT)")
    connection.execute("INSERT INTO comics (ComicLocation) VALUES ('/mylar/comics/Series')")
    connection.commit()
    connection.close()
    library_path = tmp_path / "library"
    library_path.mkdir()
    outside_path = tmp_path / "outside"
    outside_path.mkdir()
    destination = LibraryRoot(
        name="Managed destination",
        path=str(library_path),
        enabled=True,
        allow_referenced_registrations=False,
        allow_managed_writes=True,
    )
    db_session.add(destination)
    await db_session.flush()

    request = ImportJobCreate(
        source_path=str(db_path),
        source_type=ImportSourceType.MYLAR3,
        target_library_root_id=destination.id,
        mylar3_path_map={"/mylar/comics": str(outside_path)},
        mylar3_path_map_confirmed=True,
    )

    job = await create_job(db_session, request, log_event=_log_event)

    assert job.target_library_root_id == destination.id
    assert job.mylar3_path_map == {"/mylar/comics": str(outside_path)}


async def test_create_job_rejects_mylar_map_inside_a_managed_only_root(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mylar.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE comics (ComicLocation TEXT)")
    connection.execute("INSERT INTO comics (ComicLocation) VALUES ('/mylar/comics/Series')")
    connection.commit()
    connection.close()
    managed_only_path = tmp_path / "managed-only"
    managed_only_path.mkdir()
    managed_only = LibraryRoot(
        name="Managed only",
        path=str(managed_only_path),
        enabled=True,
        allow_referenced_registrations=False,
        allow_managed_writes=True,
    )
    db_session.add(managed_only)
    await db_session.flush()

    request = ImportJobCreate(
        source_path=str(db_path),
        source_type=ImportSourceType.MYLAR3,
        file_handling_mode=ImportFileHandlingMode.IN_PLACE,
        target_library_root_id=managed_only.id,
        mylar3_path_map={"/mylar/comics": str(managed_only_path)},
        mylar3_path_map_confirmed=True,
    )

    with pytest.raises(ValidationError, match="inside an enabled library root"):
        await create_job(db_session, request, log_event=_log_event)


async def test_create_job_revalidates_confirmed_mylar_mapping_snapshot(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mylar.db"
    library_path = tmp_path / "library"
    library_path.mkdir()
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE comics (ComicLocation TEXT)")
    connection.execute("INSERT INTO comics (ComicLocation) VALUES ('/mylar/comics/../escaped')")
    connection.commit()
    connection.close()
    db_session.add(LibraryRoot(name="Existing library", path=str(library_path), enabled=True))
    await db_session.flush()

    request = ImportJobCreate(
        source_path=str(db_path),
        source_type=ImportSourceType.MYLAR3,
        mylar3_path_map={"/mylar/comics": str(library_path)},
        mylar3_path_map_confirmed=True,
    )

    with pytest.raises(ValidationError, match="preview is blocked"):
        await create_job(db_session, request, log_event=_log_event)


async def test_create_job_uses_default_root_for_future_layout_without_explicit_target(
    db_session: AsyncSession,
    tmp_path: Path,
    default_managed_root: LibraryRoot,
) -> None:
    request = ImportJobCreate(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
        future_layout_requested=True,
        future_root_policy=FutureRootPolicyPayload(
            series_path_template="{Publisher}/{Series} ({Year})",
            comic_file_template="{Series} {IssueTitle} Issue {Issue:03d}",
            annual_file_template="{Series} Annual Issue {Issue:03d}",
            non_standard_file_template="{Series} {Type} {Volume:02d} - {IssueTitle}",
            single_non_standard_file_template="{Series} {Type} - {IssueTitle}",
            replace_illegal_characters=True,
            colon_replacement="dash",
        ),
    )

    job = await create_job(db_session, request, log_event=_log_event)

    assert job.target_library_root_id == default_managed_root.id


async def test_create_job_accepts_in_place_source_inside_enabled_root(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "library" / "Existing Layout"
    source_path.mkdir(parents=True)
    root = LibraryRoot(
        name="Existing Library",
        path=str(tmp_path / "library"),
        enabled=True,
        allow_referenced_registrations=True,
        allow_managed_writes=False,
    )
    db_session.add(root)
    await db_session.flush()

    job = await create_job(
        db_session,
        ImportJobCreate(
            source_path=str(source_path),
            source_type=ImportSourceType.FILESYSTEM,
            file_handling_mode=ImportFileHandlingMode.IN_PLACE,
        ),
        log_event=_log_event,
    )

    assert job.file_handling_mode == ImportFileHandlingMode.IN_PLACE
    assert job.target_library_root_id is None
    assert job.move_to_library is False
    assert job.effective_transfer_method == "leave_in_place"
    assert job.source_preserved is True
    assert job.convert_to_preferred_format is False
    assert job.update_embedded_comicinfo_from_match is False


async def test_create_job_keeps_in_place_source_root_separate_from_future_destination(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "legacy-library" / "Existing Layout"
    source_path.mkdir(parents=True)
    managed_path = tmp_path / "managed-library"
    managed_path.mkdir()
    reference_root = LibraryRoot(
        name="Legacy reference",
        path=str(source_path.parent),
        enabled=True,
        allow_referenced_registrations=True,
        allow_managed_writes=False,
    )
    managed_root = LibraryRoot(
        name="Future managed",
        path=str(managed_path),
        enabled=True,
        allow_referenced_registrations=False,
        allow_managed_writes=True,
    )
    db_session.add_all([reference_root, managed_root])
    await db_session.flush()

    job = await create_job(
        db_session,
        ImportJobCreate(
            source_path=str(source_path),
            source_type=ImportSourceType.FILESYSTEM,
            file_handling_mode=ImportFileHandlingMode.IN_PLACE,
            target_library_root_id=managed_root.id,
        ),
        log_event=_log_event,
    )

    assert job.target_library_root_id == managed_root.id


async def test_create_job_freezes_future_policy_baseline_and_job_placement_policy(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "future-library"
    root_path.mkdir()
    root = LibraryRoot(name="Future layout", path=str(root_path), enabled=True)
    db_session.add(root)
    await db_session.flush()
    proposal = FutureRootPolicyPayload(
        series_path_template="{Publisher}/{Series} ({Year})",
        comic_file_template="{Series} {IssueTitle} Issue {Issue:03d}",
        annual_file_template="{Series} Annual Issue {Issue:03d}",
        non_standard_file_template="{Series} {Type} {Volume:02d} - {IssueTitle}",
        single_non_standard_file_template="{Series} {Type} - {IssueTitle}",
        replace_illegal_characters=True,
        colon_replacement="dash",
    )

    job = await create_job(
        db_session,
        ImportJobCreate(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
            target_library_root_id=root.id,
            future_layout_requested=True,
            future_root_policy=proposal,
        ),
        log_event=_log_event,
    )

    assert job.future_layout_requested is True
    assert job.future_root_policy_snapshot is not None
    assert job.future_root_policy_snapshot["expected_root_policy_id"] is None
    assert job.future_root_policy_snapshot["expected_root_policy_revision"] == 0
    assert job.future_root_policy_snapshot["prior_policy"] is None
    assert job.ingest_policy_snapshot["series_path_template"] == ("{Publisher}/{Series} ({Year})")
    assert job.ingest_policy_snapshot["policy_source"] == "import_adoption"
    assert job.ingest_policy_snapshot["policy_revision"] == 1
    assert job.ingest_policy_snapshot["source_import_job_id"] == job.id


async def test_create_job_rejects_in_place_source_outside_enabled_root(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library_path = tmp_path / "library"
    library_path.mkdir()
    source_path = tmp_path / "imports"
    source_path.mkdir()
    db_session.add(LibraryRoot(name="Library", path=str(library_path), enabled=True))
    await db_session.flush()

    with pytest.raises(ValidationError, match="allows referenced registrations"):
        await create_job(
            db_session,
            ImportJobCreate(
                source_path=str(source_path),
                source_type=ImportSourceType.FILESYSTEM,
                file_handling_mode=ImportFileHandlingMode.IN_PLACE,
            ),
            log_event=_log_event,
        )


async def test_create_job_freezes_normalized_selected_layout(
    db_session: AsyncSession,
    tmp_path: object,
) -> None:
    request = ImportJobCreate(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
        source_layout=SourceLayoutSpecPayload(
            mode="preset",
            preset="publisher_series",
        ),
    )

    job = await create_job(db_session, request, log_event=_log_event)

    assert job.source_layout_snapshot == {
        "schema_version": 1,
        "mode": "preset",
        "preset": "publisher_series",
        "series_path_template": "{Publisher}/{Series}",
        "issue_filename_template": None,
        "selected_cluster_id": None,
        "fallback_to_auto": True,
    }


async def test_create_job_freezes_selected_layout_without_automatic_fallback(
    db_session: AsyncSession,
    tmp_path: object,
) -> None:
    request = ImportJobCreate(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
        source_layout=SourceLayoutSpecPayload(
            mode="preset",
            preset="publisher_series",
            fallback_to_auto=False,
        ),
    )

    job = await create_job(db_session, request, log_event=_log_event)

    assert job.source_layout_snapshot["fallback_to_auto"] is False


async def test_create_job_freezes_selected_layout_for_mylar_source(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    series_path = tmp_path / "Existing Series"
    series_path.mkdir()
    database = tmp_path / "mylar.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE comics (ComicLocation TEXT)")
    connection.execute(
        "INSERT INTO comics (ComicLocation) VALUES (?)",
        (str(series_path),),
    )
    connection.commit()
    connection.close()
    request = ImportJobCreate(
        source_path=str(database),
        source_type=ImportSourceType.MYLAR3,
        mylar3_path_map_confirmed=True,
        source_layout=SourceLayoutSpecPayload(
            mode="preset",
            preset="publisher_series",
        ),
    )

    job = await create_job(db_session, request, log_event=_log_event)

    assert job.source_layout_snapshot == {
        "schema_version": 1,
        "mode": "preset",
        "preset": "publisher_series",
        "series_path_template": "{Publisher}/{Series}",
        "issue_filename_template": None,
        "selected_cluster_id": None,
        "fallback_to_auto": True,
    }
