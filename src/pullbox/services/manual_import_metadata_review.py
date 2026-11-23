"""Read-only descriptive review for a manually selected source archive."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, TypeAdapter

from pullbox.core.archive import ArchiveError
from pullbox.core.archive_metadata import (
    MAX_METADATA_BYTES,
    ArchiveMetadataFiles,
    MetadataFile,
    read_archive_metadata,
)
from pullbox.core.library_file_ownership import (
    build_file_identity_signature,
    build_managed_placement_signature,
)
from pullbox.schemas.issue_file_metadata import (  # noqa: TC001 - Pydantic field
    FileMetadataChange,
    FileMetadataConflict,
    SharedXmlChoices,
)
from pullbox.schemas.issue_metadata_editor import (
    IssueMetadataEditorField,  # noqa: TC001 - Pydantic field
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
    from pullbox.core.metadata_identity import ExternalIdentityRef
    from pullbox.schemas.issue_file_metadata import FileMetadataChoices
    from pullbox.schemas.issue_metadata_editor import (
        EditorXmlField,
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


class ManualSourcePreview(ManualImportMetadataPreview):
    editor_available: bool = True
    editor_fields: list[IssueMetadataEditorField] = Field(default_factory=list)
    changes: list[FileMetadataChange] = Field(default_factory=list)
    existing_file_id: int | None = None


@dataclass(frozen=True)
class ReviewedManualMetadata:
    series: MetadataSnapshot
    issue: MetadataSnapshot
    primary: ExternalIdentityRef | None
    preview: ManualImportMetadataPreview
    shared_choices: SharedXmlChoices | None = None
    reviewed_xml_fields: frozenset[EditorXmlField] = frozenset()
    approved_pair: RenderedArchiveMetadata | None = None
    replacement_signature: dict[str, int | str] | None = None
    editor_fields: list[IssueMetadataEditorField] = field(default_factory=list)
    editor_changed: bool = False
    changes: list[FileMetadataChange] = field(default_factory=list)


async def preview_manual_source_metadata(
    plan: ManualMetadataPlan,
    source: Path,
    choices: FileMetadataChoices,
    *,
    editor: IssueMetadataEditorReview | None = None,
) -> ReviewedManualMetadata:
    """Issue corrections cannot create a private alternative to the series catalog."""
    from pullbox.services.issue_metadata_editor import IssueMetadataEditError

    note = "Keep the library value here. To change it for the series, use the series editor."
    if editor is not None and any(
        key.startswith("series.") and value != "library" for key, value in choices.items()
    ):
        raise IssueMetadataEditError(note)
    reviewed = await preview_manual_import_metadata(plan, source, choices, editor=editor)
    if editor is not None:
        for conflict in reviewed.preview.conflicts:
            if conflict.key.startswith("series."):
                conflict.note = note
                for option in conflict.options:
                    if option.source != "library":
                        option.selectable = False
    return reviewed


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

    series, issue, editor_fields, conflicts, shared_choices, rendered, effective = (
        review_metadata_pair(plan.metadata, files, archive, series, issue, primary, choices, editor)
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
    replacement_signature = None
    if editor is not None and plan.existing_file is not None:
        target = Path(plan.existing_file[1])
        if target.is_symlink() or not target.is_file() or source.samefile(target):
            from pullbox.services.issue_metadata_editor import IssueMetadataEditError

            raise IssueMetadataEditError(
                "Choose a separate source file to replace an existing, available managed copy. "
                "To edit the current file in place, use Edit metadata on the issue page."
            )
        replacement_signature = build_managed_placement_signature(target)
        evidence += json.dumps(replacement_signature, sort_keys=True).encode()
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
        editor_changed=bool(effective and effective.edits),
        changes=preview_xml_changes(files, rendered)
        if editor is not None and rendered is not None
        else [],
        shared_choices=shared_choices,
        reviewed_xml_fields=effective.reviewed_xml_fields if effective is not None else frozenset(),
        approved_pair=rendered if editor is not None else None,
        replacement_signature=replacement_signature,
    )
