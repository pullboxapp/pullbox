"""Capture selected canonical series values without refreshing issue metadata."""

import hashlib
import json

from pydantic import TypeAdapter

from pullbox.core.archive_metadata import ArchiveMetadataFiles
from pullbox.core.metadata_identity import MetadataEntityKind
from pullbox.schemas.issue_file_metadata import FileMetadataChange, FileMetadataPreview
from pullbox.schemas.metadata_snapshot import MetadataSnapshot
from pullbox.services.archive_metadata_binding import ArchiveMetadataTarget
from pullbox.services.archive_metadata_rendering import ArchiveMetadataRenderError
from pullbox.services.issue_file_metadata import PreparedFileMetadata, _fields
from pullbox.services.metadata_series_refresh_state import RefreshEntityState
from pullbox.services.series_metadata_propagation_rendering import render_series_propagation


def canonical_snapshot(state: RefreshEntityState, kind: MetadataEntityKind) -> MetadataSnapshot:
    return MetadataSnapshot(
        entity_kind=kind,
        identities=state.identities,
        values=state.values,
        origins=state.baseline.origins if state.baseline else (),
    )


def prepare_projection(
    target: ArchiveMetadataTarget,
    files: ArchiveMetadataFiles,
    fields: frozenset[str],
) -> PreparedFileMetadata:
    bound = target.binding.metadata
    protected = (fields - {"title", "sort_title", "year_start", "volume", "format"}) | (
        {"series_type", "archive_format"} if "format" in fields else set()
    )
    origins = bound.issues[0].baseline.origins if bound.issues[0].baseline else ()
    if protected & (bound.issues[0].overrides | {o.field for o in origins if o.user_override}):
        raise ArchiveMetadataRenderError("propagation_issue_override")
    series = canonical_snapshot(bound.series, MetadataEntityKind.SERIES)
    issue = canonical_snapshot(bound.issues[0], MetadataEntityKind.ISSUE)
    pair = render_series_propagation(series, issue, files, fields)
    changes: list[FileMetadataChange] = []
    for name, original, rendered in (
        ("ComicInfo.xml", files.comicinfo.payload, pair.comicinfo),
        ("MetronInfo.xml", files.metroninfo.payload, pair.metroninfo),
    ):
        old, new = _fields(original), _fields(rendered)
        changes.extend(
            FileMetadataChange(document=name, field=key, before=old.get(key), after=new.get(key))
            for key in sorted(old.keys() | new.keys())
            if old.get(key) != new.get(key)
        )
    evidence = TypeAdapter(ArchiveMetadataTarget).dump_json(target)
    evidence += json.dumps(sorted(fields)).encode()
    key = hashlib.sha256(evidence + pair.comicinfo + pair.metroninfo).hexdigest()
    return PreparedFileMetadata(
        target,
        series,
        issue,
        None,
        FileMetadataPreview(
            file_id=target.binding.library_file_id,
            file_name=target.path.name,
            changes=changes,
            review_key=key,
            unchanged=not changes,
        ),
    )
