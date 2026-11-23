"""Read-only descriptive review for a manually selected source archive."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
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
from pullbox.services.archive_metadata_reconciliation import reconcile_archive_metadata
from pullbox.services.archive_metadata_rendering import (
    ArchiveMetadataRenderError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pullbox.core.metadata_identity import ExternalIdentityRef
    from pullbox.schemas.issue_file_metadata import FileMetadataChange, FileMetadataChoices
    from pullbox.schemas.issue_metadata_editor import (
        EditorXmlField,
        IssueMetadataEditorField,
        IssueMetadataEditorReview,
    )
    from pullbox.schemas.metadata_snapshot import MetadataSnapshot
    from pullbox.services.archive_metadata_rendering import RenderedArchiveMetadata
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
    reviewed_xml_fields: frozenset[EditorXmlField] = frozenset()
    approved_pair: RenderedArchiveMetadata | None = None
    editor_fields: list[IssueMetadataEditorField] = field(default_factory=list)
    changes: list[FileMetadataChange] = field(default_factory=list)


async def preview_manual_import_metadata(
    plan: ManualMetadataPlan,
    source: Path,
    choices: FileMetadataChoices,
    *,
    editor: IssueMetadataEditorReview | None = None,
) -> ReviewedManualMetadata:
    """Read bounded XML off-thread; no publication, database writes or provider calls."""
    try:
        return await asyncio.to_thread(_preview, plan, source, choices, editor=editor)
    except ArchiveError as exc:
        raise ArchiveMetadataRenderError("archive_unreadable") from exc


def _preview(
    plan: ManualMetadataPlan,
    source: Path,
    choices: FileMetadataChoices,
    *,
    editor: IssueMetadataEditorReview | None = None,
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
    from pullbox.services.issue_file_metadata import preview_xml_changes, review_metadata_pair

    series, issue, editor_fields, conflicts, shared_choices, rendered = review_metadata_pair(
        plan.metadata, files, archive, series, issue, primary, choices, editor
    )
    for conflict in conflicts if editor is None else ():
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
    if editor is not None:
        evidence += json.dumps(editor.model_dump(mode="json"), sort_keys=True).encode()
        if rendered is not None:
            evidence += rendered.comicinfo + rendered.metroninfo
    if build_file_identity_signature(source) != fingerprint:
        raise ArchiveMetadataRenderError("source_changed")
    return ReviewedManualMetadata(
        series,
        issue,
        primary,
        ManualImportMetadataPreview(
            ready=rendered is not None,
            review_key=hashlib.sha256(evidence).hexdigest(),
            context_key=context_key,
            conflicts=conflicts,
        ),
        editor_fields=editor_fields,
        changes=preview_xml_changes(files, rendered)
        if editor is not None and rendered is not None
        else [],
        shared_choices=shared_choices,
        reviewed_xml_fields=editor.reviewed_xml_fields if editor is not None else frozenset(),
        approved_pair=rendered if editor is not None else None,
    )
