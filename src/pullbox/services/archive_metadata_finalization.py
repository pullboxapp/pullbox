"""Finalize proven paired publication in the caller's database transaction."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.models import Issue, LibraryFile, Series
from pullbox.models.archive_metadata_publication import PublicationState
from pullbox.schemas.metadata_snapshot import MetadataSnapshot
from pullbox.services.archive_metadata_binding import (
    ArchiveMetadataBinding,
    revalidate_archive_metadata_target,
)
from pullbox.services.archive_metadata_publication import (
    ArchivePublicationError,
    ArchivePublicationInspection,
    ArchivePublicationReceipt,
    _clean_session,
    _directories_unchanged,
    _file_work,
    _lock_binding,
    _locked_row,
    _published,
    _receipt,
    _stat_fingerprint,
    load_archive_publication,
)
from pullbox.services.import_archive_publication import (
    acknowledge_import_archive_publication,
    require_import_archive_owner,
)
from pullbox.services.metadata_baselines import MetadataBaselineWrite, save_metadata_baselines
from pullbox.services.metadata_credits import write_issue_credits
from pullbox.services.metadata_entity_values import (
    apply_issue_metadata_values,
    apply_series_metadata_values,
)
from pullbox.services.metadata_writer_identity import metadata_write_scope


async def apply_bound_archive_metadata(
    session: AsyncSession,
    binding: ArchiveMetadataBinding,
    series_snapshot: MetadataSnapshot,
    issue_snapshot: MetadataSnapshot,
) -> None:
    """Apply a proven publication's descriptive values without changing identities/counts."""
    bound = binding.metadata
    if (
        not series_snapshot.values.title
        or not series_snapshot.values.sort_title
        or series_snapshot.values.issue_count != bound.series.values.issue_count
        or issue_snapshot.values.issue_number_text != bound.issues[0].values.issue_number_text
    ):
        raise ArchivePublicationError("invalid_plan")
    async with metadata_write_scope(session):
        series = await session.get(Series, bound.series.local_id)
        issue = await session.get(Issue, bound.issues[0].local_id)
        assert series is not None and issue is not None
        await apply_series_metadata_values(session, series, series_snapshot.values)
        apply_issue_metadata_values(issue, issue_snapshot.values)
        await write_issue_credits(session, {issue.id: issue_snapshot.values.credits})
        await save_metadata_baselines(
            session,
            [
                MetadataBaselineWrite(entity.local_id, snapshot, entity.baseline_revision)
                for entity, snapshot in (
                    (bound.series, series_snapshot),
                    (bound.issues[0], issue_snapshot),
                )
                if entity.baseline != snapshot
            ],
        )


def _check_output(
    receipt: ArchivePublicationReceipt, inspection: ArchivePublicationInspection
) -> datetime:
    try:
        _directories_unchanged(receipt.plan)
        info = receipt.plan.target.path.lstat()
        if _stat_fingerprint(info) != inspection.fingerprint or not _published(
            receipt.plan, inspection
        ):
            raise ArchivePublicationError("inspection_changed")
        # Match existing registration/binding conversion, not ns-to-float division
        # which can round a submicrosecond boundary differently from stat.st_mtime.
        return datetime.fromtimestamp(info.st_mtime, UTC)
    except OSError:
        raise ArchivePublicationError("inspection_changed") from None


async def finalize_archive_publication(
    session: AsyncSession,
    receipt: ArchivePublicationReceipt,
    inspection: ArchivePublicationInspection,
) -> ArchivePublicationReceipt:
    """Adopt only an independently inspected, still-current publication.

    Hashing belongs to inspection outside this transaction. Short file rechecks
    are offloaded under the journal lock. A completed replay is historical proof,
    not permission to reapply metadata or proof of current filesystem ownership.
    Workflow rollback journals and legacy writers still need owner integration.
    """
    _clean_session(session)
    if (inspection.operation_id, inspection.revision) != (receipt.operation_id, receipt.revision):
        raise ArchivePublicationError("publication_changed")
    async with metadata_write_scope(session):
        known = await load_archive_publication(session, receipt.operation_id)
        if known is None:
            raise ArchivePublicationError("publication_missing")
        if known.plan != receipt.plan:
            raise ArchivePublicationError("publication_changed")
        await _lock_binding(session, known.plan)
        row = await _locked_row(session, receipt.operation_id)
        current = _receipt(row)
        if current.state is PublicationState.FINALIZED and (
            current == receipt
            or (
                receipt.state is PublicationState.PUBLISHED
                and current.revision == receipt.revision + 1
                and current.plan == receipt.plan
            )
        ):
            return current
        if current != receipt:
            raise ArchivePublicationError("publication_changed")
        if current.state is not PublicationState.PUBLISHED:
            raise ArchivePublicationError("publication_not_published")
        plan = receipt.plan
        await require_import_archive_owner(session, plan)
        await revalidate_archive_metadata_target(session, plan.target)
        modified_at = await _file_work(lambda stop: _check_output(receipt, inspection))
        await apply_bound_archive_metadata(session, plan.target.binding, plan.series, plan.issue)
        file = await session.get(LibraryFile, plan.target.binding.library_file_id)
        assert file is not None
        assert inspection.fingerprint is not None
        file.file_size = inspection.fingerprint[2]
        file.file_modified_at = modified_at
        file.file_hash = plan.output_digest
        file.has_comicinfo = True
        await acknowledge_import_archive_publication(session, receipt)
        await session.flush()
        await _file_work(lambda stop: _check_output(receipt, inspection))
        row.state = PublicationState.FINALIZED
        row.revision += 1
        row.active_file_id = None
        row.active_path_key = None
        await session.flush()
        return _receipt(row)
