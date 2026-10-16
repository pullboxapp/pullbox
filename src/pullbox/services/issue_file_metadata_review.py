"""Explicit descriptive choices from inspected values, never identity overrides."""

from datetime import UTC, date, datetime
from typing import cast

from pullbox.core.archive_metadata_fields import canonical_archive_format, comicinfo_credits
from pullbox.core.metadata_identity import MetadataEntityKind
from pullbox.schemas.issue_file_metadata import (
    FileMetadataChoices,
    FileMetadataConflict,
    FileMetadataField,
    FileMetadataOption,
    FileMetadataSource,
)
from pullbox.schemas.metadata_snapshot import FieldOrigin, MetadataSnapshot, field_domain
from pullbox.services.archive_metadata_binding import ArchiveMetadataBinding
from pullbox.services.archive_metadata_preservation import ArchiveMetadataRenderError
from pullbox.services.archive_metadata_reconciliation import ArchiveMetadataReconciliation
from pullbox.services.archive_metadata_rendering import _series_separator_key
from pullbox.services.metadata_series_refresh_state import SeriesRefreshState

_FIELDS = {
    "series": (
        "title",
        "sort_title",
        "publisher",
        "language",
        "year_start",
        "volume",
        "series_type",
        "issue_count",
    ),
    "issue": ("title", "description", "cover_date", "store_date", "page_count", "credits"),
}


def _display(value: object) -> str:
    if value is None:
        return "Not set"
    if isinstance(value, tuple):
        return "\n".join(f"{item.name} ({item.role})" for item in value)
    return value.isoformat() if isinstance(value, date) else str(value)


def _equal(field: str, source: FileMetadataSource, old: object, new: object) -> bool:
    if field == "series_type":
        old = canonical_archive_format(str(old)) if old is not None else None
        new = canonical_archive_format(str(new)) if new is not None else None
    if field == "credits" and source == "ComicInfo.xml" and isinstance(new, tuple):
        new = comicinfo_credits(new)
    return old == new


def review_metadata_fields(
    binding: ArchiveMetadataBinding | SeriesRefreshState,
    archive: ArchiveMetadataReconciliation,
    series: MetadataSnapshot,
    issue: MetadataSnapshot,
    choices: FileMetadataChoices,
) -> tuple[MetadataSnapshot, MetadataSnapshot, list[FileMetadataConflict]]:
    """A choice names a source; its actual value comes only from this bound read."""
    conflicts: list[FileMetadataConflict] = []
    snapshots = {"series": series, "issue": issue}
    seen: set[str] = set()
    metadata = binding.metadata if isinstance(binding, ArchiveMetadataBinding) else binding
    for kind, fields in _FIELDS.items():
        current = metadata.series if kind == "series" else metadata.issues[0]
        snapshot = snapshots[kind]
        values = snapshot.values.model_dump()
        origins = {item.field: item for item in snapshot.origins}
        for field in fields:
            key = cast("FileMetadataField", f"{kind}.{field}")
            options: dict[FileMetadataSource, object] = {"library": getattr(current.values, field)}
            for source, document in (
                ("ComicInfo.xml", archive.comicinfo),
                ("MetronInfo.xml", archive.metroninfo),
            ):
                old = getattr(getattr(document, kind), field)
                if old is not None:
                    options[cast("FileMetadataSource", source)] = old
            proposed = getattr(snapshot.values, field)
            disagrees = any(
                source != "library" and not _equal(field, source, old, proposed)
                for source, old in options.items()
            )
            # Partial ComicInfo dates are evidence, but not a selectable complete date.
            parts = archive.comicinfo.publication_date_parts
            if kind == "issue" and field == "cover_date" and any(p is not None for p in parts):
                new = (
                    (proposed.year, proposed.month, proposed.day)
                    if isinstance(proposed, date)
                    else (None, None, None)
                )
                disagrees = disagrees or (
                    proposed is not None
                    and any(
                        old is not None and old != value
                        for old, value in zip(parts, new, strict=True)
                    )
                )
            if (
                kind == "series"
                and field == "title"
                and isinstance(proposed, str)
                and any(item.identity in issue.identities for item in archive.evidence)
                and all(
                    isinstance(old, str)
                    and _series_separator_key(old) == _series_separator_key(proposed)
                    for source, old in options.items()
                    if source != "library"
                )
            ):
                disagrees = False
            if not disagrees:
                continue
            seen.add(key)
            selected = choices.get(key)
            allowed = {
                source for source in options if field != "issue_count" or source == "library"
            }
            if kind == "series" and field in {"title", "sort_title"}:
                allowed = {source for source in allowed if options[source]}
            if selected is not None and selected not in allowed:
                raise ArchiveMetadataRenderError("invalid_choice", field)
            display = [
                FileMetadataOption(
                    source=source, value=_display(value), selectable=source in allowed
                )
                for source, value in options.items()
            ]
            if (
                kind == "issue"
                and field == "cover_date"
                and "ComicInfo.xml" not in options
                and any(p is not None for p in parts)
            ):
                display.append(
                    FileMetadataOption(
                        source="ComicInfo.xml",
                        value="-".join(str(p) if p is not None else "?" for p in parts),
                        selectable=False,
                    )
                )
            conflicts.append(
                FileMetadataConflict(
                    key=key,
                    label=f"{kind.title()} {field.replace('_', ' ')}",
                    options=display,
                    selected=selected,
                    note="Issue count comes from the library catalog; file counts cannot change it."
                    if field == "issue_count"
                    else "",
                )
            )
            if selected is not None:
                values[field] = options[selected]
                origins[field] = FieldOrigin(
                    field=field,
                    domain=field_domain(snapshot.entity_kind, field),
                    observed_at=datetime.now(UTC),
                    user_override=True,
                )
        snapshots[kind] = MetadataSnapshot.model_validate(
            {
                **snapshot.model_dump(),
                "values": values,
                "origins": tuple(origins.values()),
            }
        )
    if set(choices) - seen:
        raise ArchiveMetadataRenderError("choices_changed")
    assert snapshots["series"].entity_kind is MetadataEntityKind.SERIES
    return snapshots["series"], snapshots["issue"], conflicts
