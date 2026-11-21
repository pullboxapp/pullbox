"""Local exact-identity targets for deferred native-provider folder imports."""

from __future__ import annotations

from collections import defaultdict
from itertools import batched
from typing import TYPE_CHECKING

from sqlalchemy import or_, select, tuple_

from pullbox.models.issue import Issue
from pullbox.models.metadata_identity import IssueExternalIdentity
from pullbox.services.import_file_match_targets import FileMatchTargetIndex
from pullbox.services.import_metron_matching import (
    file_metron_evidence,
    load_metron_issue_targets,
    metron_file_candidate,
    verified_native_import_series,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportedFile, ImportedSeries
    from pullbox.models.series import Series


def native_recovery_row(item: ImportedSeries) -> bool:
    """Keep native review evidence out of legacy filename/catalog fallback."""
    return isinstance((item.diagnostics or {}).get("metron_import"), dict) or bool(
        (item.diagnostics or {}).get("manual_library_series")
    )


async def native_recovery_targets(
    session: AsyncSession, rows: list[tuple[ImportedFile, ImportedSeries]]
) -> dict[int, tuple[Issue, Series]]:
    """Reuse scan identity proof without provider calls, guesses, or new links."""
    groups: dict[int, list[ImportedFile]] = defaultdict(list)
    parents = {}
    for file, parent in rows:
        if native_recovery_row(parent) and not parent.diagnostics.get("manual_library_series"):
            groups[parent.id].append(file)
            parents[parent.id] = parent
    targets: dict[int, tuple[Issue, Series]] = {}
    for parent_id, files in groups.items():
        series = await verified_native_import_series(session, parents[parent_id])
        if series is None:
            continue
        for batch in batched(files, 100):
            refs = {
                (ref["namespace"], ref["id"])
                for file in batch
                for ref in (file_metron_evidence(file) or {}).get("identities", [])
                if ref.get("kind") == "issue"
            }
            if not refs:
                continue
            linked = select(IssueExternalIdentity.issue_id).where(
                tuple_(
                    IssueExternalIdentity.identity_namespace, IssueExternalIdentity.external_id
                ).in_(refs)
            )
            cv_ids = [
                int(value)
                for namespace, value in refs
                if namespace == "comicvine" and value.isdigit()
            ]
            issues = list(
                await session.scalars(
                    select(Issue).where(
                        Issue.series_id == series.id,
                        or_(Issue.id.in_(linked), Issue.comicvine_id.in_(cv_ids)),
                    )
                )
            )
            index = FileMatchTargetIndex(
                existing_series=series, issue_entries=[(issue, False) for issue in issues]
            )
            for issue in issues:
                if issue.comicvine_id is not None:
                    index.cv_id_map[issue.comicvine_id] = (
                        issue.id,
                        issue.comicvine_id,
                        False,
                        issue,
                        issue.title,
                    )
            await load_metron_issue_targets(session, index, list(batch))
            for file in batch:
                candidate, _ = metron_file_candidate(file, index)
                if candidate is not None:
                    issue = next(
                        issue for issue in issues if issue.id == candidate.matched_issue_id
                    )
                    if file.matched_issue_id in (None, issue.id):
                        targets[file.id] = issue, series
    return targets
