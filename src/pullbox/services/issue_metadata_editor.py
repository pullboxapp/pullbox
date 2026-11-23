"""Issue-only field inventory and corrections on an independently bound archive."""

from datetime import UTC, date, datetime
from typing import cast

from pydantic import ValidationError

from pullbox.core.archive_metadata import ArchiveMetadataFiles
from pullbox.core.issue_community_rating import (
    RATING_HELP,
    RATING_LOCKED,
    annotated_rating,
    embedded_score,
    shared_score,
)
from pullbox.core.issue_notes import UneditableNotesError, document_notes, safe_notes_prose
from pullbox.schemas.issue_metadata_editor import (
    EDITOR_XML_FIELDS,
    EditableIssueField,
    EditorXmlField,
    IssueMetadataEditorField,
    IssueMetadataEditorOption,
    IssueMetadataEditorReview,
)
from pullbox.schemas.metadata_issue_arcs import IssueArcEntry, comicinfo_arcs
from pullbox.schemas.metadata_labels import LABEL_XML_PATHS, selectable_labels
from pullbox.schemas.metadata_snapshot import (
    FieldOrigin,
    MetadataSnapshot,
    MetadataValues,
    SharedAgeRating,
    field_domain,
    selectable_imprint,
)
from pullbox.services.archive_metadata_preservation import (
    ArchiveMetadataRenderError,
    editor_arc_value,
    embedded_labels,
    preserved_roots,
)
from pullbox.services.archive_metadata_reconciliation import ArchiveMetadataReconciliation
from pullbox.services.metadata_editor_origins import saved_field_sources
from pullbox.services.metadata_series_refresh_state import (
    ISSUE_FIELDS,
    SERIES_FIELDS,
    SeriesRefreshState,
)

EDITABLE_FIELDS = {
    "title": ("Title", "text"),
    "description": ("Summary", "textarea"),
    "notes": ("Notes", "textarea"),
    "cover_date": ("Cover date", "date"),
    "store_date": ("Store date", "date"),
    "page_count": ("Catalog page count", "number"),
    "credits": ("Creator credits", "credits"),
    "age_rating": ("Age rating", "text"),
    "community_rating": ("Community rating", "text"),
    "imprint": ("Imprint", "text"),
    "story_arcs": ("Embedded story arcs", "arcs"),
    **{name: (name.capitalize(), "textarea") for name in LABEL_XML_PATHS},
}

FIELD_HELP = {
    "notes": "Only the note text changes. Identity markers stay in their original XML document.",
    "story_arcs": "This edits the comic, not tracked arcs or their reading order. "
    "Positions are optional. If any are blank, only MetronInfo can store the known positions.",
    **{
        name: "One entry per line. Existing names keep their Metron IDs; new or renamed entries "
        "get no ID."
        for name in LABEL_XML_PATHS
    },
    "age_rating": "Only ratings with the same label in both formats are offered. "
    "Clear removes the rating; Unknown writes a rating of Unknown.",
    "page_count": "Catalog page count does not change the number of comic pages.",
    "imprint": "Changes this issue only. The series publisher stays the same. "
    "A renamed imprint does not inherit the previous imprint's ID.",
    "credits": "One creator per line: Name | Role. Existing resource identifiers remain protected.",
}


class IssueMetadataEditError(ValueError):
    """A safe field-specific validation message without echoing submitted values."""


def display_editor_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, tuple):
        if value and isinstance(value[0], IssueArcEntry):
            return "\n".join(
                f"{e.name} (position {e.position})" if e.position is not None else e.name
                for e in value
            )
        if all(isinstance(item, str) for item in value):
            return "\n".join(value)
        return "\n".join(f"{item.name} | {item.role}" for item in value)
    return value.isoformat() if isinstance(value, date) else str(value)


def _selectable(field: str, value: object) -> bool:
    if field == "community_rating":
        return value is None or shared_score(value) is not None
    if field in LABEL_XML_PATHS:
        return selectable_labels(value)
    if field == "story_arcs":
        return not isinstance(value, str)
    if field == "imprint":
        return selectable_imprint(value)
    return field != "age_rating" or value is None or value in tuple(SharedAgeRating)


def _score_help(count: str, *, reviewed: bool, keeps_average: bool) -> str:
    if not count:
        return RATING_HELP
    votes = f" of {count} vote{'s' if count != '1' else ''}" if len(count) <= 16 else ""
    if reviewed:
        action = "keeps" if keeps_average else "removes"
        return f"{RATING_HELP} This edit {action} the count{votes} in MetronInfo."
    return (
        f"{RATING_HELP} Changing or clearing the MetronInfo average also removes the count{votes}."
    )


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
    if field in {"description", "title", "notes"}:
        # Use the text that both XML readers will return on the next preview.
        value = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if field == "notes" and not safe_notes_prose(value):
            raise IssueMetadataEditError(
                "Enter Notes as plain text of up to 4096 characters, without identity markers "
                "or legacy retailer references."
            )
    elif field == "community_rating":
        if shared_score(text) is None:
            raise IssueMetadataEditError(RATING_HELP)
    elif field == "page_count":
        if not text.isascii() or not text.isdecimal() or len(text) > 7:
            raise IssueMetadataEditError(
                "Catalog page count must be a whole number from 0 to 1000000."
            )
        value = int(text)
    elif field in LABEL_XML_PATHS:
        value = tuple(line.strip() for line in text.split("\n") if line.strip())
        if not selectable_labels(value):
            raise IssueMetadataEditError(
                f"Enter up to 128 distinct {field}, one per line, with no commas "
                "and no more than 255 characters per entry."
            )
    elif field == "imprint" and not selectable_imprint(text):
        raise IssueMetadataEditError(
            "Enter up to 255 printable characters on one line, without surrounding spaces."
        )
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
    metadata: SeriesRefreshState,
    archive: ArchiveMetadataReconciliation,
    assembled_series: MetadataSnapshot,
    assembled_issue: MetadataSnapshot,
    files: ArchiveMetadataFiles,
    review: IssueMetadataEditorReview,
) -> tuple[
    MetadataSnapshot, MetadataSnapshot, list[IssueMetadataEditorField], IssueMetadataEditorReview
]:
    """Never use client-supplied source values or adopt unselected descriptive changes."""
    series_state, issue_state = metadata.series, metadata.issues[0]
    issue_sources = saved_field_sources(issue_state)
    series = assembled_series.model_copy(update={"values": series_state.values})
    issue = assembled_issue.model_copy(update={"values": issue_state.values})
    origins = {origin.field: origin for origin in issue.origins}
    values = issue.values.model_dump()
    fields: list[IssueMetadataEditorField] = []
    effective = dict(review.edits)
    ci, mi = preserved_roots(files)
    for field, (label, input_type) in EDITABLE_FIELDS.items():
        current = getattr(issue_state.values, field)
        options: dict[str, object] = {"library": current}
        locked_reason = ""
        help_text = FIELD_HELP.get(field, "")
        if field == "community_rating" and annotated_rating(mi):
            locked_reason = RATING_LOCKED
        for source, document, root in (
            ("ComicInfo.xml", archive.comicinfo, ci),
            ("MetronInfo.xml", archive.metroninfo, mi),
        ):
            value: object
            if field == "notes":
                try:
                    value = document_notes(root).prose
                except UneditableNotesError as exc:
                    locked_reason = str(exc)
                    continue
            elif field == "community_rating":
                value = embedded_score(root)
            elif field in LABEL_XML_PATHS:
                ci_path, mi_path = LABEL_XML_PATHS[field]
                value = embedded_labels(root, ci_path if root is ci else mi_path) or None
            elif field == "age_rating":
                value = root.findtext("AgeRating")
            elif field == "imprint":
                value = root.findtext("Imprint" if root is ci else "Publisher/Imprint")
            elif field == "story_arcs":
                value = editor_arc_value(root, comicinfo=root is ci)
            else:
                value = getattr(document.issue, field)
            if value is not None:
                options[source] = value
        key = cast("EditableIssueField", field)
        edit = review.edits.get(key)
        if edit and locked_reason:
            raise IssueMetadataEditError(locked_reason)
        # Older select-all clients sent "keep library" even when no source had a value.
        if edit and edit.source == "library" and all(v in (None, "", ()) for v in options.values()):
            del effective[key]
            edit = None
        previous = origins.get(field)
        if edit is not None:
            if edit.source == "custom":
                values[field] = (
                    edit.value
                    if isinstance(edit.value, tuple)
                    else _custom_value(field, edit.value or "")
                )
            elif edit.source == "clear":
                values[field] = () if field == "credits" else None
            elif edit.source == "reset":
                if previous is None or not previous.user_override:
                    raise IssueMetadataEditError(f"{label} has no manual override to remove.")
            elif edit.source in options:
                if options[edit.source] in (None, "", ()):
                    raise IssueMetadataEditError(
                        f"{label} has no value in that source. Leave it unchanged "
                        "or choose Clear value to keep it empty."
                    )
                if field == "community_rating" and not _selectable(field, options[edit.source]):
                    raise IssueMetadataEditError(RATING_HELP)
                if field == "story_arcs" and isinstance(options[edit.source], str):
                    raise IssueMetadataEditError(
                        "These arc names and positions cannot be paired safely. "
                        "Enter corrections or choose Clear value."
                    )
                if field == "imprint" and not selectable_imprint(options[edit.source]):
                    raise IssueMetadataEditError(
                        "Enter an imprint of up to 255 printable characters, or choose Clear value."
                    )
                if field in LABEL_XML_PATHS and not selectable_labels(options[edit.source]):
                    raise IssueMetadataEditError(
                        f"These {field} cannot be written to both formats. "
                        f"Enter the {field} to keep or choose Clear value."
                    )
                if (
                    field == "age_rating"
                    and options[edit.source] is not None
                    and options[edit.source] not in tuple(SharedAgeRating)
                ):
                    raise IssueMetadataEditError(
                        "This rating has no exact equivalent in both formats. "
                        "Choose a shared rating or clear it."
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
        if field == "community_rating":
            help_text = _score_help(
                mi.findtext("CommunityRating/RatingCount", "").strip(),
                reviewed=edit is not None and edit.source != "reset",
                keeps_average=proposed.community_rating == options.get("MetronInfo.xml"),
            )
        differs = any(value != current for value in options.values())
        arc_draft: tuple[IssueArcEntry, ...] = ()
        if field == "story_arcs":
            left, right = options.get("ComicInfo.xml"), options.get("MetronInfo.xml")
            expected = issue_state.values.story_arcs or ()
            differs = (left or ()) != comicinfo_arcs(expected) or (right or ()) != expected
            if expected or (previous and previous.user_override):
                arc_draft = expected
            elif isinstance(right, tuple) and (
                left is None or left == right or left == comicinfo_arcs(right)
            ):
                arc_draft = right
            elif isinstance(left, tuple) and right is None:
                arc_draft = left
        fields.append(
            IssueMetadataEditorField(
                key=f"issue.{field}",
                xml_conflict_key=EDITOR_XML_FIELDS.get(cast("EditorXmlField", field)),
                label=label,
                current=display_editor_value(current),
                current_source_label=issue_sources.get(field),
                proposed=display_editor_value(getattr(proposed, field)),
                editable=not locked_reason,
                input_type=input_type,
                arc_draft=arc_draft,
                options=[
                    IssueMetadataEditorOption(
                        source=source,
                        value=display_editor_value(value),
                        selectable=_selectable(field, value),
                    )
                    for source, value in options.items()
                ],
                allowed_values=tuple(SharedAgeRating) if field == "age_rating" else (),
                overridden=bool(previous and previous.user_override),
                differs=differs
                or bool(field == "notes" and locked_reason and previous and previous.user_override)
                or (
                    field in EDITOR_XML_FIELDS
                    and current is not None
                    and bool(previous and previous.user_override)
                    and len(options) < 3
                ),
                selected=key in review.edits,
                origin=(
                    "Manual correction"
                    if previous and previous.user_override
                    else previous.source.value
                    if previous and previous.source
                    else "Library"
                ),
                reason=locked_reason
                or (
                    "Set the publisher in the series editor before adding or keeping an imprint. "
                    "To remove an existing imprint from this issue, choose Clear value."
                    if field == "imprint" and not series.values.publisher
                    else help_text
                ),
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
                if not any(item.startswith(f"archive:issue:{field}:") for field in effective)
            ),
        }
    )
    for kind, state, applicable, labels in (
        (
            "series",
            series_state,
            SERIES_FIELDS | {"archive_format"},
            saved_field_sources(series_state),
        ),
        ("issue", issue_state, ISSUE_FIELDS, issue_sources),
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
                    current_source_label=labels.get(field),
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
    return series, issue, fields, review.model_copy(update={"edits": effective})
