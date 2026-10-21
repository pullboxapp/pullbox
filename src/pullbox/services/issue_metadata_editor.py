"""Issue-only field inventory and corrections on an independently bound archive."""

from datetime import UTC, date, datetime
from typing import cast
from xml.etree import ElementTree as ET

from pydantic import ValidationError

from pullbox.core.archive_metadata import ArchiveMetadataFiles
from pullbox.schemas.issue_metadata_editor import (
    EditableIssueField,
    IssueMetadataEditorField,
    IssueMetadataEditorOption,
    IssueMetadataEditorReview,
)
from pullbox.schemas.metadata_snapshot import (
    FieldOrigin,
    MetadataSnapshot,
    MetadataValues,
    field_domain,
)
from pullbox.services.archive_metadata_binding import ArchiveMetadataBinding
from pullbox.services.archive_metadata_preservation import (
    ArchiveMetadataRenderError,
    review_preserved_fields,
)
from pullbox.services.archive_metadata_reconciliation import ArchiveMetadataReconciliation
from pullbox.services.metadata_series_refresh_state import ISSUE_FIELDS, SERIES_FIELDS

EDITABLE_FIELDS = {
    "title": ("Title", "text"),
    "description": ("Summary", "textarea"),
    "cover_date": ("Cover date", "date"),
    "store_date": ("Store date", "date"),
    "page_count": ("Catalog page count", "number"),
    "credits": ("Creator credits", "credits"),
}
SHARED_TAGS = {
    "AgeRating": ("Age rating", "AgeRating"),
    "Genre": ("Genre", "Genres/Genre"),
    "Tags": ("Tags", "Tags/Tag"),
    "Characters": ("Characters", "Characters/Character"),
    "Teams": ("Teams", "Teams/Team"),
    "Locations": ("Locations", "Locations/Location"),
    "Imprint": ("Imprint", "Publisher/Imprint"),
    "StoryArc": ("Story arc", "Arcs/Arc/Name"),
    "StoryArcNumber": ("Story arc position", "Arcs/Arc/Number"),
}


class IssueMetadataEditError(ValueError):
    """A safe field-specific validation message without echoing submitted values."""


def display_editor_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, tuple):
        return "\n".join(f"{item.name} | {item.role}" for item in value)
    return value.isoformat() if isinstance(value, date) else str(value)


def _custom_value(field: str, text: str) -> object:
    if not text.strip():
        raise IssueMetadataEditError(
            f"Enter a {EDITABLE_FIELDS[field][0].lower()} or choose Clear value."
        )
    if any(ord(c) < 32 and c not in "\n\r\t" for c in text):
        raise IssueMetadataEditError(
            f"{EDITABLE_FIELDS[field][0]} contains unsupported control characters."
        )
    value: object = text
    if field == "page_count":
        if not text.isascii() or not text.isdecimal() or len(text) > 7:
            raise IssueMetadataEditError(
                "Catalog page count must be a whole number from 0 to 1000000."
            )
        value = int(text)
    elif field == "credits":
        credits = []
        for line in text.splitlines():
            if not line.strip():
                continue
            name, separator, role = line.rpartition("|")
            if not separator or not name.strip():
                raise IssueMetadataEditError(
                    "Enter each credit as Name | Role, one creator per line."
                )
            credits.append({"name": name.strip(), "role": role.strip()})
        value = credits
    try:
        return getattr(MetadataValues.model_validate({field: value}), field)
    except ValidationError:
        raise IssueMetadataEditError(
            f"Enter a valid {EDITABLE_FIELDS[field][0].lower()}."
        ) from None


def prepare_issue_editor(
    binding: ArchiveMetadataBinding,
    archive: ArchiveMetadataReconciliation,
    assembled_series: MetadataSnapshot,
    assembled_issue: MetadataSnapshot,
    files: ArchiveMetadataFiles,
    review: IssueMetadataEditorReview,
) -> tuple[MetadataSnapshot, MetadataSnapshot, list[IssueMetadataEditorField]]:
    """Never use client-supplied source values or adopt unselected descriptive changes."""
    series_state, issue_state = binding.metadata.series, binding.metadata.issues[0]
    series = assembled_series.model_copy(update={"values": series_state.values})
    issue = assembled_issue.model_copy(update={"values": issue_state.values})
    origins = {origin.field: origin for origin in issue.origins}
    values = issue.values.model_dump()
    fields: list[IssueMetadataEditorField] = []
    for field, (label, input_type) in EDITABLE_FIELDS.items():
        current = getattr(issue_state.values, field)
        options: dict[str, object] = {"library": current}
        for source, document in (
            ("ComicInfo.xml", archive.comicinfo),
            ("MetronInfo.xml", archive.metroninfo),
        ):
            value = getattr(document.issue, field)
            if value is not None:
                options[source] = value
        edit = review.edits.get(cast("EditableIssueField", field))
        previous = origins.get(field)
        if edit is not None:
            if edit.source == "custom":
                values[field] = _custom_value(field, edit.value or "")
            elif edit.source == "clear":
                values[field] = () if field == "credits" else None
            elif edit.source == "reset":
                if previous is None or not previous.user_override:
                    raise IssueMetadataEditError(f"{label} has no manual override to remove.")
            elif edit.source in options:
                if options[edit.source] in (None, "", ()) and any(
                    value not in (None, "", ()) for value in options.values()
                ):
                    raise IssueMetadataEditError(
                        f"{label} has no value in that source. Choose Clear value to remove it."
                    )
                values[field] = options[edit.source]
            else:
                raise ArchiveMetadataRenderError("choices_changed", field)
            origins[field] = FieldOrigin(
                field=field,
                domain=field_domain(issue.entity_kind, field),
                observed_at=datetime.now(UTC),
                user_override=edit.source != "reset",
                provider_managed=edit.source == "reset",
            )
        proposed = MetadataValues.model_validate(values)
        fields.append(
            IssueMetadataEditorField(
                key=f"issue.{field}",
                label=label,
                current=display_editor_value(current),
                proposed=display_editor_value(getattr(proposed, field)),
                editable=True,
                input_type=input_type,
                options=[
                    IssueMetadataEditorOption(source=source, value=display_editor_value(value))
                    for source, value in options.items()
                ],
                overridden=bool(previous and previous.user_override),
                differs=any(value != current for value in options.values()),
                selected=edit is not None,
                origin=(
                    "Manual correction"
                    if previous and previous.user_override
                    else previous.source.value
                    if previous and previous.source
                    else "Library"
                ),
                reason="Catalog page count does not change the number of comic pages."
                if field == "page_count"
                else (
                    "One creator per line: Name | Role. "
                    "Existing resource identifiers remain protected."
                )
                if field == "credits"
                else "",
            )
        )
    issue = MetadataSnapshot.model_validate(
        {
            **issue.model_dump(),
            "values": values,
            "origins": tuple(origins.values()),
            "diagnostics": tuple(
                item
                for item in issue.diagnostics
                if not any(item.startswith(f"archive:issue:{field}:") for field in review.edits)
            ),
        }
    )
    for kind, state, applicable in (
        ("series", series_state, SERIES_FIELDS | {"archive_format"}),
        ("issue", issue_state, ISSUE_FIELDS),
    ):
        for field in MetadataValues.model_fields:
            if field not in applicable or (kind == "issue" and field in EDITABLE_FIELDS):
                continue
            value = display_editor_value(getattr(state.values, field))
            fields.append(
                IssueMetadataEditorField(
                    key=f"{kind}.{field}",
                    label=f"{kind.title()} {field.replace('_', ' ')}",
                    current=value,
                    proposed=value,
                    reason=(
                        "Series-wide field: use the series workflow; "
                        "this editor changes one issue only."
                    )
                    if kind == "series"
                    else (
                        "Protected identity or artwork field. "
                        "Use provider links or the appropriate artwork workflow."
                    ),
                )
            )
        for identity in state.identities:
            fields.append(
                IssueMetadataEditorField(
                    key=f"{kind}.id.{identity.namespace}",
                    label=f"{kind.title()} {identity.namespace} ID",
                    current=identity.external_id,
                    proposed=identity.external_id,
                    reason="Identity is protected. Review provider links to change a match.",
                )
            )
    # The reconciliation parser has already validated bounded XML before this display-only read.
    ci = (
        ET.fromstring(files.comicinfo.payload)
        if files.comicinfo.payload
        else ET.Element("ComicInfo")
    )
    mi = (
        ET.fromstring(files.metroninfo.payload)
        if files.metroninfo.payload
        else ET.Element("MetronInfo")
    )
    shared_conflicts = {item.key for item in review_preserved_fields(files, {})}
    for tag, (label, mi_path) in SHARED_TAGS.items():
        left = ci.findtext(tag) or ""
        right = ", ".join(node.text or "" for node in mi.findall(mi_path))
        value = "\n".join(
            f"{source}: {value}"
            for source, value in (("ComicInfo.xml", left), ("MetronInfo.xml", right))
            if value
        )
        fields.append(
            IssueMetadataEditorField(
                key=f"xml.{tag}",
                label=label,
                current=value,
                proposed=value,
                differs=f"xml.{tag}" in shared_conflicts,
                reason=(
                    "Preserved XML-only field. Manual editing of this field is not available "
                    "in this milestone; existing conflicts use Write file metadata."
                ),
            )
        )
    return series, issue, fields
