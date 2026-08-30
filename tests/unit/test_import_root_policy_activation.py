"""Tests for import-driven root-policy activation and conditional rollback."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from pullbox.core.exceptions import ValidationError
from pullbox.core.library_policy import LibraryIngestPolicy, load_library_ingest_policy
from pullbox.models.import_job import (
    ImportJob,
    ImportJobAction,
    ImportJobActionStatus,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.models.library import (
    LibraryRoot,
    LibraryRootPolicy,
    LibraryRootPolicySource,
)
from pullbox.services.import_job_actions import rollback_action as rollback_import_action
from pullbox.services.import_root_policy_activation import (
    RootPolicyActivationConflictError,
    activate_future_root_policy,
    build_future_root_policy_snapshot,
    rollback_future_root_policy,
)


def _proposal() -> dict[str, object]:
    return {
        "schema_version": 1,
        "series_path_template": "{Publisher}/{Series} ({Year})",
        "comic_file_template": "{Series} {IssueTitle} Issue {Issue:03d}",
        "annual_file_template": "{Series} Annual Issue {Issue:03d}",
        "non_standard_file_template": "{Series} {Type} {Volume:02d} - {IssueTitle}",
        "single_non_standard_file_template": "{Series} {Type} - {IssueTitle}",
        "replace_illegal_characters": True,
        "colon_replacement": "dash",
    }


def test_future_root_policy_snapshot_rejects_unsafe_series_path() -> None:
    global_policy = LibraryIngestPolicy(
        rename_on_import=True,
        series_folder_template="{Series} ({Year})",
        comic_file_template="{Series} #{Issue:03d}",
        annual_file_template="{Series} Annual #{Issue:03d}",
        non_standard_file_template="{Series} {Type} {Volume:02d}",
        single_non_standard_file_template="{Series} {Type}",
        replace_illegal_characters=True,
        colon_replacement="dash",
        post_processing_method="move",
        torrent_import_strategy="standard",
        normalize_imported_archives_to_cbz=False,
        skip_existing_files=False,
        update_embedded_comicinfo_from_match=False,
        series_path_template="{Series} ({Year})",
    )
    proposal = _proposal()
    proposal["series_path_template"] = "../../{Series}"

    with pytest.raises(ValidationError, match="unsafe segment"):
        build_future_root_policy_snapshot(proposal, global_policy)


async def _job_with_proposal(db_session, root: LibraryRoot) -> ImportJob:  # type: ignore[no-untyped-def]
    baseline = await load_library_ingest_policy(db_session)
    job = ImportJob(
        source_path="/imports/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.IMPORTING,
        target_library_root_id=root.id,
        future_layout_requested=True,
        future_root_policy_snapshot=build_future_root_policy_snapshot(
            _proposal(),
            baseline,
        ),
    )
    db_session.add(job)
    await db_session.flush()
    return job


@pytest.mark.asyncio
async def test_future_root_policy_does_not_activate_without_successful_registration(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    root = LibraryRoot(name="Comics", path="/library", enabled=True)
    db_session.add(root)
    await db_session.flush()
    job = await _job_with_proposal(db_session, root)

    action = await activate_future_root_policy(
        db_session,
        job,
        successful_registration_count=0,
    )

    assert action is None
    assert job.future_root_policy_applied_at is None
    assert await db_session.scalar(select(func.count()).select_from(LibraryRootPolicy)) == 0


@pytest.mark.asyncio
async def test_future_root_policy_activates_once_and_journals_prior_state(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    root = LibraryRoot(name="Comics", path="/library", enabled=True)
    db_session.add(root)
    await db_session.flush()
    job = await _job_with_proposal(db_session, root)

    action = await activate_future_root_policy(
        db_session,
        job,
        successful_registration_count=1,
    )
    repeated = await activate_future_root_policy(
        db_session,
        job,
        successful_registration_count=2,
    )

    policy = await db_session.scalar(
        select(LibraryRootPolicy).where(LibraryRootPolicy.library_root_id == root.id)
    )
    assert policy is not None
    assert policy.series_path_template == "{Publisher}/{Series} ({Year})"
    assert policy.source == LibraryRootPolicySource.IMPORT_ADOPTION
    assert policy.source_import_job_id == job.id
    assert policy.revision == 1
    assert action is not None
    assert repeated is not None and repeated.id == action.id
    assert action.action_type == "library_root_policy_applied"
    assert action.payload["prior_policy"] is None
    assert action.payload["applied_policy_id"] == policy.id
    assert action.payload["applied_revision"] == 1
    assert job.future_root_policy_applied_at is not None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ImportJobAction)
            .where(
                ImportJobAction.import_job_id == job.id,
                ImportJobAction.action_type == "library_root_policy_applied",
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_future_root_policy_activation_rejects_concurrent_edit(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    root = LibraryRoot(name="Comics", path="/library", enabled=True)
    db_session.add(root)
    await db_session.flush()
    job = await _job_with_proposal(db_session, root)
    concurrent = LibraryRootPolicy(
        library_root=root,
        schema_version=1,
        series_path_template="{Series}",
        comic_file_template="{Series} #{Issue:03d}",
        annual_file_template="{Series} Annual #{Issue:03d}",
        non_standard_file_template="{Series} {Type} {Volume:02d}",
        single_non_standard_file_template="{Series} {Type}",
        replace_illegal_characters=True,
        colon_replacement="space",
        source=LibraryRootPolicySource.MANUAL,
        revision=1,
    )
    db_session.add(concurrent)
    await db_session.flush()

    with pytest.raises(RootPolicyActivationConflictError, match="changed after import setup"):
        await activate_future_root_policy(
            db_session,
            job,
            successful_registration_count=1,
        )

    assert concurrent.series_path_template == "{Series}"
    assert concurrent.source == LibraryRootPolicySource.MANUAL
    assert job.future_root_policy_applied_at is None


@pytest.mark.asyncio
async def test_future_root_policy_rollback_restores_prior_only_when_job_still_owns_version(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    root = LibraryRoot(name="Comics", path="/library", enabled=True)
    prior = LibraryRootPolicy(
        library_root=root,
        schema_version=1,
        series_path_template="{Series} ({Year})",
        comic_file_template="{Series} #{Issue:03d}",
        annual_file_template="{Series} Annual #{Issue:03d}",
        non_standard_file_template="{Series} {Type} {Volume:02d}",
        single_non_standard_file_template="{Series} {Type}",
        replace_illegal_characters=True,
        colon_replacement="space",
        source=LibraryRootPolicySource.MANUAL,
        revision=4,
    )
    db_session.add_all([root, prior])
    await db_session.flush()
    baseline = await load_library_ingest_policy(db_session)
    baseline = replace(
        baseline,
        series_path_template=prior.series_path_template,
        series_folder_template=prior.series_path_template,
        comic_file_template=prior.comic_file_template,
        annual_file_template=prior.annual_file_template,
        non_standard_file_template=prior.non_standard_file_template,
        single_non_standard_file_template=prior.single_non_standard_file_template,
        replace_illegal_characters=prior.replace_illegal_characters,
        colon_replacement=prior.colon_replacement,
        policy_source=prior.source,
        root_policy_id=prior.id,
        policy_revision=prior.revision,
    )
    job = ImportJob(
        source_path="/imports/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.IMPORTING,
        target_library_root_id=root.id,
        future_layout_requested=True,
        future_root_policy_snapshot=build_future_root_policy_snapshot(
            _proposal(),
            baseline,
        ),
    )
    db_session.add(job)
    await db_session.flush()
    action = await activate_future_root_policy(
        db_session,
        job,
        successful_registration_count=1,
    )
    assert action is not None

    async def unused_delete_series(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("policy rollback must not delete a series")

    await rollback_import_action(
        db_session,
        action_id=action.id,
        action_type=action.action_type,
        payload=dict(action.payload),
        delete_series=unused_delete_series,
    )

    await db_session.refresh(prior)
    assert prior.series_path_template == "{Series} ({Year})"
    assert prior.source == LibraryRootPolicySource.MANUAL
    assert prior.source_import_job_id is None
    assert prior.revision == 6
    assert job.future_root_policy_applied_at is None
    assert action.status == ImportJobActionStatus.ROLLED_BACK
    assert action.rolled_back_at is not None


@pytest.mark.asyncio
async def test_future_root_policy_rollback_preserves_newer_manual_edit(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    root = LibraryRoot(name="Comics", path="/library", enabled=True)
    db_session.add(root)
    await db_session.flush()
    job = await _job_with_proposal(db_session, root)
    action = await activate_future_root_policy(
        db_session,
        job,
        successful_registration_count=1,
    )
    assert action is not None
    policy = await db_session.scalar(
        select(LibraryRootPolicy).where(LibraryRootPolicy.library_root_id == root.id)
    )
    assert policy is not None
    policy.series_path_template = "Manual/{Series}"
    policy.source = LibraryRootPolicySource.MANUAL
    policy.source_import_job_id = None
    policy.revision += 1
    policy.updated_at = datetime.now(UTC)
    await db_session.flush()

    with pytest.raises(RootPolicyActivationConflictError, match="newer edit"):
        await rollback_future_root_policy(db_session, job=job, action=action)

    assert policy.series_path_template == "Manual/{Series}"
    assert policy.source == LibraryRootPolicySource.MANUAL
    assert policy.revision == 2
