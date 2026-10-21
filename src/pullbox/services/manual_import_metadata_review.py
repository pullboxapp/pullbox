"""Read-only descriptive review for a manually selected source archive."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, TypeAdapter

from pullbox.core.archive import ArchiveError
from pullbox.core.archive_metadata import (
    MAX_METADATA_BYTES,
    ArchiveMetadataFiles,
    MetadataFile,
    read_archive_metadata,
)
from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.schemas.issue_file_metadata import (  # noqa: TC001 - Pydantic field
    FileMetadataConflict,
    SharedXmlChoices,
)
from pullbox.services.archive_metadata_binding import (
    assemble_archive_metadata_state,
    select_archive_primary_identity,
)
from pullbox.services.archive_metadata_preservation import (
    review_preserved_fields,
    shared_xml_choices,
)
from pullbox.services.archive_metadata_reconciliation import reconcile_archive_metadata
from pullbox.services.archive_metadata_rendering import (
    ArchiveMetadataRenderError,
    render_archive_metadata,
)
from pullbox.services.issue_file_metadata_review import review_metadata_fields

if TYPE_CHECKING:
    from pathlib import Path

    from pullbox.core.metadata_identity import ExternalIdentityRef
    from pullbox.schemas.issue_file_metadata import FileMetadataChoices
    from pullbox.schemas.metadata_snapshot import MetadataSnapshot
    from pullbox.services.manual_paired_metadata import ManualMetadataPlan


class ManualImportMetadataPreview(BaseModel):
    ready: bool
    review_key: str
    context_key: str
    conflicts: list[FileMetadataConflict]


@dataclass(frozen=True)
class ReviewedManualMetadata:
    series: MetadataSnapshot
    issue: MetadataSnapshot
    primary: ExternalIdentityRef | None
    preview: ManualImportMetadataPreview
    shared_choices: SharedXmlChoices | None = None


async def preview_manual_import_metadata(
    plan: ManualMetadataPlan, source: Path, choices: FileMetadataChoices
) -> ReviewedManualMetadata:
    """Read bounded XML off-thread; no publication, database writes or provider calls."""
    try:
        return await asyncio.to_thread(_preview, plan, source, choices)
    except ArchiveError as exc:
        raise ArchiveMetadataRenderError("archive_unreadable") from exc


def _preview(
    plan: ManualMetadataPlan, source: Path, choices: FileMetadataChoices
) -> ReviewedManualMetadata:
    fingerprint = build_file_identity_signature(source)
    kind = source.suffix.lstrip(".").casefold()
    files = (
        ArchiveMetadataFiles(MetadataFile("ComicInfo.xml"), MetadataFile("MetronInfo.xml"))
        if kind == "pdf"
        else read_archive_metadata(
            source,
            "cbz" if kind == "zip" else kind,
            max_solid_scan_bytes=MAX_METADATA_BYTES if kind in {"cbz", "zip"} else plan.limit,
        )
    )
    archive = reconcile_archive_metadata(files)
    series, issue = assemble_archive_metadata_state(plan.metadata, archive, now=datetime.now(UTC))
    primary = select_archive_primary_identity(plan.metadata, archive)
    series, issue, conflicts = review_metadata_fields(
        plan.metadata, archive, series, issue, choices
    )
    shared_choices = shared_xml_choices(choices)
    conflicts.extend(review_preserved_fields(files, shared_choices))
    ready = all(item.selected is not None for item in conflicts)
    try:
        render_archive_metadata(
            series,
            issue,
            files,
            primary_identity=primary,
            previous_series=plan.metadata.series.baseline,
            previous_issue=plan.metadata.issues[0].baseline,
            shared_choices=shared_choices,
        )
    except ArchiveMetadataRenderError as exc:
        if (
            ready
            or exc.code != "unreconciled_field"
            or not any(
                item.key.split(".")[1] == exc.field and item.selected is None for item in conflicts
            )
        ):
            raise
    for conflict in conflicts:
        if conflict.key == "issue.page_count":
            conflict.note = (
                "Archive page counts can include covers and extras; catalog counts may differ. "
                "Choose the value for this library copy. The catalog record stays unchanged."
            )
        elif conflict.key == "issue.cover_date":
            conflict.note = (
                "The file date may describe publication rather than the catalog cover date. "
                "Pullbox will not guess a missing day or change the store date."
            )
    # Bind approval to the exact plan, source, XML and choices, not observation timestamps.
    evidence = TypeAdapter(type(plan)).dump_json(plan)
    evidence += json.dumps(
        {"path": str(source), "fingerprint": fingerprint}, sort_keys=True
    ).encode()
    evidence += (files.comicinfo.payload or b"") + (files.metroninfo.payload or b"")
    context_key = hashlib.sha256(evidence).hexdigest()
    evidence += json.dumps(choices, sort_keys=True).encode()
    if build_file_identity_signature(source) != fingerprint:
        raise ArchiveMetadataRenderError("source_changed")
    return ReviewedManualMetadata(
        series,
        issue,
        primary,
        ManualImportMetadataPreview(
            ready=ready,
            review_key=hashlib.sha256(evidence).hexdigest(),
            context_key=context_key,
            conflicts=conflicts,
        ),
        shared_choices=shared_choices,
    )
