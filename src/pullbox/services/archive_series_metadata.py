"""Project embedded publication formats without changing library classification."""

from pullbox.core.archive_metadata_fields import canonical_archive_format
from pullbox.models.series import SeriesStatus, SeriesType
from pullbox.schemas.metadata_snapshot import MetadataSnapshot, MetadataValues
from pullbox.services.archive_metadata_preservation import ArchiveMetadataRenderError


def project_archive_series(snapshot: MetadataSnapshot, current: MetadataValues) -> MetadataSnapshot:
    """Validate database enums before publication; also accept pre-fix journals."""
    values = snapshot.values.model_dump()
    origins = list(snapshot.origins)
    format_value = values["series_type"]
    if format_value in {"Limited Series", "Digital Chapter"}:
        values["archive_format"] = format_value
        values["series_type"] = current.series_type
        origins = [
            origin.model_copy(update={"field": "archive_format"})
            if origin.field == "series_type"
            else origin
            for origin in origins
            if origin.field != "archive_format"
        ]
    elif format_value is not None:
        values["series_type"] = canonical_archive_format(format_value)
    try:
        if values["series_type"] is not None:
            SeriesType(values["series_type"])
        if values["status"] is not None:
            SeriesStatus(values["status"])
    except ValueError as exc:
        raise ArchiveMetadataRenderError("invalid_database_metadata") from exc
    return MetadataSnapshot.model_validate(
        {**snapshot.model_dump(), "values": values, "origins": origins}
    )


def archive_render_series(snapshot: MetadataSnapshot) -> MetadataSnapshot:
    """Use the retained embedded format only when rendering XML, never in the ORM."""
    if snapshot.values.archive_format is None:
        return snapshot
    return snapshot.model_copy(
        update={
            "values": snapshot.values.model_copy(
                update={"series_type": snapshot.values.archive_format}
            ),
            "origins": tuple(
                origin.model_copy(update={"field": "series_type"})
                if origin.field == "archive_format"
                else origin
                for origin in snapshot.origins
                if origin.field != "series_type"
            ),
        }
    )
