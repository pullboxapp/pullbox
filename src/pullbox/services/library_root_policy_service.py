"""Read, preview, and mutate explicit naming policies for library roots."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

from sqlalchemy import select

from pullbox.core.exceptions import PullboxError
from pullbox.core.library_naming import build_series_relative_path, compute_target_filename
from pullbox.core.library_policy import load_effective_library_ingest_policy
from pullbox.models.issue import Issue, IssueType
from pullbox.models.library import LibraryRoot, LibraryRootPolicy, LibraryRootPolicySource
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series
from pullbox.services.import_root_policy_activation import (
    RootPolicyActivationConflictError,
    apply_future_root_policy_to_ingest_policy,
    normalize_root_policy_definition,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.library_policy import LibraryIngestPolicy


class LibraryRootNotFoundError(PullboxError):
    """The selected library root does not exist."""

    def __init__(self) -> None:
        super().__init__(
            message="Library root not found.",
            code="LIBRARY_ROOT_NOT_FOUND",
            status_code=404,
        )


async def get_library_root_policy_state(
    session: AsyncSession,
    library_root_id: int,
) -> dict[str, object]:
    """Return the root's explicit scope and complete effective naming policy."""
    root = await _load_root(session, library_root_id)
    return await _serialize_state(session, root)


async def update_library_root_policy(
    session: AsyncSession,
    library_root_id: int,
    *,
    expected_revision: int,
    definition: Mapping[str, object],
) -> dict[str, object]:
    """Create or replace a root override after an optimistic revision check."""
    root = await _load_root(session, library_root_id, for_update=True)
    current = await _load_explicit_policy(session, root.id, for_update=True)
    _assert_revision(current, expected_revision)
    proposal = normalize_root_policy_definition(definition)
    next_revision = (current.revision if current is not None else 0) + 1

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
            source=LibraryRootPolicySource.MANUAL,
            source_import_job_id=None,
            revision=next_revision,
        )
        session.add(current)
    else:
        _assign_definition(current, proposal)
        current.source = LibraryRootPolicySource.MANUAL
        current.source_import_job_id = None
        current.revision = next_revision

    await session.flush()
    return await _serialize_state(session, root)


async def clear_library_root_policy(
    session: AsyncSession,
    library_root_id: int,
    *,
    expected_revision: int,
) -> dict[str, object]:
    """Remove a root override and resume inheritance from global defaults."""
    root = await _load_root(session, library_root_id, for_update=True)
    current = await _load_explicit_policy(session, root.id, for_update=True)
    _assert_revision(current, expected_revision)
    if current is not None:
        await session.delete(current)
        await session.flush()
    return await _serialize_state(session, root)


async def preview_library_root_policy(
    session: AsyncSession,
    library_root_id: int,
    *,
    definition: Mapping[str, object],
    examples: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """Render current and proposed outputs without persisting the proposal."""
    root = await _load_root(session, library_root_id)
    current = await load_effective_library_ingest_policy(session, root)
    proposal = normalize_root_policy_definition(definition)
    proposed = apply_future_root_policy_to_ingest_policy(current, proposal)
    subjects = _preview_subjects(examples)

    return {
        "current_scope": _scope(current),
        "current_series_paths": _series_path_examples(current, subjects),
        "proposed_series_paths": _series_path_examples(proposed, subjects),
        "current_file_names": _file_name_examples(current, subjects),
        "proposed_file_names": _file_name_examples(proposed, subjects),
    }


async def _load_root(
    session: AsyncSession,
    library_root_id: int,
    *,
    for_update: bool = False,
) -> LibraryRoot:
    statement = select(LibraryRoot).where(LibraryRoot.id == library_root_id)
    if for_update:
        statement = statement.with_for_update()
    root = await session.scalar(statement)
    if root is None:
        raise LibraryRootNotFoundError()
    return root


async def _load_explicit_policy(
    session: AsyncSession,
    library_root_id: int,
    *,
    for_update: bool = False,
) -> LibraryRootPolicy | None:
    statement = select(LibraryRootPolicy).where(
        LibraryRootPolicy.library_root_id == library_root_id
    )
    if for_update:
        statement = statement.with_for_update()
    return cast("LibraryRootPolicy | None", await session.scalar(statement))


def _assert_revision(policy: LibraryRootPolicy | None, expected_revision: int) -> None:
    current_revision = policy.revision if policy is not None else 0
    if expected_revision != current_revision:
        raise RootPolicyActivationConflictError(
            "Library root policy changed after it was loaded; refresh and try again."
        )


def _assign_definition(
    policy: LibraryRootPolicy,
    proposal: Mapping[str, object],
) -> None:
    policy.schema_version = 1
    policy.series_path_template = str(proposal["series_path_template"])
    policy.comic_file_template = str(proposal["comic_file_template"])
    policy.annual_file_template = str(proposal["annual_file_template"])
    policy.non_standard_file_template = str(proposal["non_standard_file_template"])
    policy.single_non_standard_file_template = str(proposal["single_non_standard_file_template"])
    policy.replace_illegal_characters = bool(proposal["replace_illegal_characters"])
    policy.colon_replacement = str(proposal["colon_replacement"])


async def _serialize_state(
    session: AsyncSession,
    root: LibraryRoot,
) -> dict[str, object]:
    policy = await load_effective_library_ingest_policy(session, root)
    return {
        "library_root_id": root.id,
        "library_root_name": root.name,
        "scope": _scope(policy),
        "policy_id": policy.root_policy_id,
        "revision": policy.policy_revision,
        "effective_policy": {
            "schema_version": 1,
            "series_path_template": policy.series_path_template or policy.series_folder_template,
            "series_folder_template": policy.series_folder_template,
            "comic_file_template": policy.comic_file_template,
            "annual_file_template": policy.annual_file_template,
            "non_standard_file_template": policy.non_standard_file_template,
            "single_non_standard_file_template": policy.single_non_standard_file_template,
            "replace_illegal_characters": policy.replace_illegal_characters,
            "colon_replacement": policy.colon_replacement,
            "source": str(policy.policy_source),
            "source_import_job_id": policy.source_import_job_id,
        },
    }


def _scope(policy: LibraryIngestPolicy) -> str:
    return "root_override" if policy.root_policy_id is not None else "global_default"


def _sample_series() -> Series:
    publisher = Publisher(name="DC Comics")
    return Series(
        title="Batman",
        sort_title="batman",
        year_start=2024,
        publisher=publisher,
    )


def _preview_subjects(
    examples: Sequence[Mapping[str, object]],
) -> list[tuple[Series, Issue]]:
    if examples:
        subjects: list[tuple[Series, Issue]] = []
        for example in examples:
            publisher_value = example.get("publisher")
            publisher = (
                Publisher(name=publisher_value)
                if isinstance(publisher_value, str) and publisher_value
                else None
            )
            series_value = example.get("series")
            year_value = example.get("year")
            issue_number_value = example.get("issue_number")
            issue_title_value = example.get("issue_title")
            if not isinstance(series_value, str) or not series_value:
                continue
            if isinstance(issue_number_value, bool) or not isinstance(
                issue_number_value, int | float
            ):
                continue
            series = Series(
                title=series_value,
                sort_title=series_value.casefold(),
                year_start=year_value if isinstance(year_value, int) else None,
                publisher=publisher,
            )
            issue = Issue(
                series=series,
                issue_number=float(issue_number_value),
                title=issue_title_value if isinstance(issue_title_value, str) else None,
                issue_type=IssueType.ISSUE,
            )
            subjects.append((series, issue))
        if subjects:
            return subjects

    series = _sample_series()
    return [
        (
            series,
            Issue(
                series=series,
                issue_number=number,
                title=title,
                issue_type=issue_type,
            ),
        )
        for number, title, issue_type in (
            (17.0, "The Brave and the Bold", IssueType.ISSUE),
            (1.0, "Annual Adventure", IssueType.ANNUAL),
            (1.0, "The Long Halloween", IssueType.ONE_SHOT),
        )
    ]


def _series_path_examples(
    policy: LibraryIngestPolicy,
    subjects: Sequence[tuple[Series, Issue]],
) -> list[str]:
    paths: list[str] = []
    for series, _issue in subjects:
        path = build_series_relative_path(series, policy).as_posix()
        if path not in paths:
            paths.append(path)
    return paths


def _file_name_examples(
    policy: LibraryIngestPolicy,
    subjects: Sequence[tuple[Series, Issue]],
) -> list[str]:
    return [
        compute_target_filename(
            issue,
            series,
            Path("sample.cbz"),
            policy,
        )
        for series, issue in subjects
    ]
