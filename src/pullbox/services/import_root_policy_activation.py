"""Transactional activation and rollback for import-adopted root policies."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import select

from pullbox.core.exceptions import PullboxError, ValidationError
from pullbox.core.library_naming import validate_series_path_template
from pullbox.models.import_job import (
    ImportJob,
    ImportJobAction,
    ImportJobActionStatus,
)
from pullbox.models.library import (
    LibraryRoot,
    LibraryRootPolicy,
    LibraryRootPolicySource,
)
from pullbox.services.import_job_actions import record_action

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.library_policy import LibraryIngestPolicy

_POLICY_ACTION_TYPE = "library_root_policy_applied"
_TEMPLATE_KEYS = (
    "series_path_template",
    "comic_file_template",
    "annual_file_template",
    "non_standard_file_template",
    "single_non_standard_file_template",
)
_COLON_REPLACEMENTS = frozenset({"dash", "space", "empty", "smart"})


class RootPolicyActivationConflictError(PullboxError):
    """A newer root-policy edit won an optimistic comparison."""

    def __init__(self, message: str) -> None:
        super().__init__(message=message, code="ROOT_POLICY_CONFLICT", status_code=409)


def build_future_root_policy_snapshot(
    proposal: Mapping[str, object],
    baseline: LibraryIngestPolicy,
) -> dict[str, object]:
    """Freeze a proposal together with its explicit-policy comparison baseline."""
    normalized = _normalized_proposal(proposal)
    normalized.update(
        {
            "expected_root_policy_id": baseline.root_policy_id,
            "expected_root_policy_revision": baseline.policy_revision,
            "prior_policy": _explicit_policy_snapshot_from_effective(baseline),
        }
    )
    return normalized


def apply_future_root_policy_to_ingest_policy(
    baseline: LibraryIngestPolicy,
    snapshot: Mapping[str, object],
    *,
    source_import_job_id: int | None = None,
) -> LibraryIngestPolicy:
    """Use a frozen proposal for this job's managed placement before activation."""
    proposal = _normalized_proposal(snapshot)
    expected_revision = _snapshot_int(
        snapshot.get("expected_root_policy_revision", baseline.policy_revision),
        field_name="expected_root_policy_revision",
    )
    return replace(
        baseline,
        series_folder_template=str(proposal["series_path_template"]),
        series_path_template=str(proposal["series_path_template"]),
        comic_file_template=str(proposal["comic_file_template"]),
        annual_file_template=str(proposal["annual_file_template"]),
        non_standard_file_template=str(proposal["non_standard_file_template"]),
        single_non_standard_file_template=str(proposal["single_non_standard_file_template"]),
        replace_illegal_characters=bool(proposal["replace_illegal_characters"]),
        colon_replacement=str(proposal["colon_replacement"]),
        policy_source=LibraryRootPolicySource.IMPORT_ADOPTION,
        root_policy_id=None,
        policy_revision=expected_revision + 1,
        source_import_job_id=source_import_job_id,
    )


async def activate_future_root_policy(
    session: AsyncSession,
    job: ImportJob,
    *,
    successful_registration_count: int,
) -> ImportJobAction | None:
    """Apply a proposed root policy once a job has registered at least one file."""
    if (
        not job.future_layout_requested
        or successful_registration_count < 1
        or not job.future_root_policy_snapshot
    ):
        return None

    existing_action = await _load_policy_action(session, job.id)
    if job.future_root_policy_applied_at is not None:
        return existing_action

    if job.target_library_root_id is None:
        raise ValidationError("Future library layout requires a target library root.")

    root = await session.scalar(
        select(LibraryRoot).where(LibraryRoot.id == job.target_library_root_id).with_for_update()
    )
    if root is None or not root.enabled:
        raise ValidationError("Future library layout requires an enabled target library root.")

    snapshot = dict(job.future_root_policy_snapshot)
    proposal = _normalized_proposal(snapshot)
    current = await session.scalar(
        select(LibraryRootPolicy)
        .where(LibraryRootPolicy.library_root_id == root.id)
        .with_for_update()
    )

    if current is not None and _policy_matches_job(current, job, proposal):
        action = existing_action or await _record_policy_action(
            session,
            job,
            current,
            prior_policy=snapshot.get("prior_policy"),
        )
        job.future_root_policy_applied_at = job.future_root_policy_applied_at or datetime.now(UTC)
        await session.flush()
        return action

    _assert_expected_policy(current, snapshot)
    prior_policy = _serialize_root_policy(current)
    applied_revision = int(current.revision if current is not None else 0) + 1
    if current is None:
        current = LibraryRootPolicy(
            library_root_id=root.id,
            schema_version=1,
            series_path_template=str(proposal["series_path_template"]),
            comic_file_template=str(proposal["comic_file_template"]),
            annual_file_template=str(proposal["annual_file_template"]),
            non_standard_file_template=str(proposal["non_standard_file_template"]),
            single_non_standard_file_template=str(proposal["single_non_standard_file_template"]),
            replace_illegal_characters=bool(proposal["replace_illegal_characters"]),
            colon_replacement=str(proposal["colon_replacement"]),
            source=LibraryRootPolicySource.IMPORT_ADOPTION,
            source_import_job_id=job.id,
            revision=applied_revision,
        )
        session.add(current)
    else:
        _assign_proposal(current, proposal)
        current.source = LibraryRootPolicySource.IMPORT_ADOPTION
        current.source_import_job_id = job.id
        current.revision = applied_revision
    await session.flush()

    action = existing_action or await _record_policy_action(
        session,
        job,
        current,
        prior_policy=prior_policy,
    )
    job.future_root_policy_applied_at = datetime.now(UTC)
    await session.flush()
    return action


async def rollback_future_root_policy(
    session: AsyncSession,
    *,
    job: ImportJob,
    action: ImportJobAction,
) -> None:
    """Restore a policy only while the job still owns the applied revision."""
    if action.status == ImportJobActionStatus.ROLLED_BACK:
        return

    payload = dict(action.payload or {})
    root_id = int(payload.get("library_root_id") or 0)
    current = await session.scalar(
        select(LibraryRootPolicy)
        .where(LibraryRootPolicy.library_root_id == root_id)
        .with_for_update()
    )
    applied_policy_id = int(payload.get("applied_policy_id") or 0)
    applied_revision = int(payload.get("applied_revision") or 0)
    if (
        current is None
        or current.id != applied_policy_id
        or current.revision != applied_revision
        or current.source_import_job_id != job.id
        or current.source != LibraryRootPolicySource.IMPORT_ADOPTION
    ):
        raise RootPolicyActivationConflictError(
            "Library root policy has a newer edit; rollback preserved the current policy."
        )

    prior_policy = payload.get("prior_policy")
    if prior_policy is None:
        await session.delete(current)
    elif isinstance(prior_policy, dict):
        _restore_prior_policy(
            current,
            prior_policy,
            next_revision=applied_revision + 1,
        )
    else:
        raise ValidationError("Root policy rollback journal is invalid.")

    job.future_root_policy_applied_at = None
    action.status = ImportJobActionStatus.ROLLED_BACK
    action.rolled_back_at = datetime.now(UTC)
    await session.flush()


async def _load_policy_action(
    session: AsyncSession,
    job_id: int,
) -> ImportJobAction | None:
    return cast(
        "ImportJobAction | None",
        await session.scalar(
            select(ImportJobAction)
            .where(
                ImportJobAction.import_job_id == job_id,
                ImportJobAction.action_type == _POLICY_ACTION_TYPE,
            )
            .order_by(ImportJobAction.sequence_no.desc())
            .limit(1)
        ),
    )


async def _record_policy_action(
    session: AsyncSession,
    job: ImportJob,
    policy: LibraryRootPolicy,
    *,
    prior_policy: object,
) -> ImportJobAction:
    return await record_action(
        session,
        job,
        phase="import",
        action_type=_POLICY_ACTION_TYPE,
        payload={
            "library_root_id": policy.library_root_id,
            "prior_policy": prior_policy,
            "applied_policy_id": policy.id,
            "applied_revision": policy.revision,
            "applied_policy": _serialize_root_policy(policy),
        },
    )


def _assert_expected_policy(
    current: LibraryRootPolicy | None,
    snapshot: Mapping[str, object],
) -> None:
    expected_id_raw = snapshot.get("expected_root_policy_id")
    expected_id = (
        None
        if expected_id_raw is None
        else _snapshot_int(expected_id_raw, field_name="expected_root_policy_id")
    )
    expected_revision = _snapshot_int(
        snapshot.get("expected_root_policy_revision"),
        field_name="expected_root_policy_revision",
    )
    current_id = current.id if current is not None else None
    current_revision = int(current.revision if current is not None else 0)
    if current_id != expected_id or current_revision != expected_revision:
        raise RootPolicyActivationConflictError(
            "Library root policy changed after import setup; the proposed policy was not applied."
        )


def _policy_matches_job(
    current: LibraryRootPolicy,
    job: ImportJob,
    proposal: Mapping[str, object],
) -> bool:
    return (
        current.source == LibraryRootPolicySource.IMPORT_ADOPTION
        and current.source_import_job_id == job.id
        and all(getattr(current, key) == proposal[key] for key in _TEMPLATE_KEYS)
        and current.replace_illegal_characters == proposal["replace_illegal_characters"]
        and current.colon_replacement == proposal["colon_replacement"]
    )


def _assign_proposal(
    policy: LibraryRootPolicy,
    proposal: Mapping[str, object],
) -> None:
    policy.schema_version = 1
    for key in _TEMPLATE_KEYS:
        setattr(policy, key, str(proposal[key]))
    policy.replace_illegal_characters = bool(proposal["replace_illegal_characters"])
    policy.colon_replacement = str(proposal["colon_replacement"])


def _restore_prior_policy(
    policy: LibraryRootPolicy,
    prior: Mapping[str, object],
    *,
    next_revision: int,
) -> None:
    proposal = _normalized_proposal(prior)
    _assign_proposal(policy, proposal)
    policy.source = LibraryRootPolicySource(str(prior["source"]))
    source_job_id = prior.get("source_import_job_id")
    policy.source_import_job_id = (
        None
        if source_job_id is None
        else _snapshot_int(source_job_id, field_name="source_import_job_id")
    )
    policy.revision = next_revision


def _normalized_proposal(value: Mapping[str, object]) -> dict[str, object]:
    if value.get("schema_version", 1) != 1:
        raise ValidationError("Unsupported future root policy schema version.")
    result: dict[str, object] = {"schema_version": 1}
    for key in _TEMPLATE_KEYS:
        raw = value.get(key)
        if not isinstance(raw, str) or not raw.strip():
            raise ValidationError("Future root policy templates must be complete.")
        result[key] = raw
    replace_illegal = value.get("replace_illegal_characters")
    if not isinstance(replace_illegal, bool):
        raise ValidationError("Future root policy has an invalid replacement setting.")
    result["replace_illegal_characters"] = replace_illegal
    colon_replacement = value.get("colon_replacement")
    if colon_replacement not in _COLON_REPLACEMENTS:
        raise ValidationError("Future root policy has an invalid colon replacement.")
    result["colon_replacement"] = colon_replacement
    try:
        validate_series_path_template(str(result["series_path_template"]))
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return result


def _snapshot_int(value: object, *, field_name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValidationError(f"Future root policy {field_name} must be an integer.")
    try:
        return int(value)
    except ValueError as exc:
        raise ValidationError(f"Future root policy {field_name} must be an integer.") from exc


def _explicit_policy_snapshot_from_effective(
    baseline: LibraryIngestPolicy,
) -> dict[str, object] | None:
    if baseline.root_policy_id is None:
        return None
    return {
        "id": baseline.root_policy_id,
        "schema_version": 1,
        "series_path_template": baseline.series_path_template or baseline.series_folder_template,
        "comic_file_template": baseline.comic_file_template,
        "annual_file_template": baseline.annual_file_template,
        "non_standard_file_template": baseline.non_standard_file_template,
        "single_non_standard_file_template": baseline.single_non_standard_file_template,
        "replace_illegal_characters": baseline.replace_illegal_characters,
        "colon_replacement": baseline.colon_replacement,
        "source": str(baseline.policy_source),
        "source_import_job_id": baseline.source_import_job_id,
        "revision": baseline.policy_revision,
    }


def _serialize_root_policy(policy: LibraryRootPolicy | None) -> dict[str, object] | None:
    if policy is None:
        return None
    return {
        "id": policy.id,
        "schema_version": policy.schema_version,
        "series_path_template": policy.series_path_template,
        "comic_file_template": policy.comic_file_template,
        "annual_file_template": policy.annual_file_template,
        "non_standard_file_template": policy.non_standard_file_template,
        "single_non_standard_file_template": policy.single_non_standard_file_template,
        "replace_illegal_characters": policy.replace_illegal_characters,
        "colon_replacement": policy.colon_replacement,
        "source": str(policy.source),
        "source_import_job_id": policy.source_import_job_id,
        "revision": policy.revision,
    }
