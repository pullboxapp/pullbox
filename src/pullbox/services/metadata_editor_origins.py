"""Describe the source of a saved field, never of a value only proposed by review."""

from pullbox.schemas.metadata_snapshot import FieldOrigin
from pullbox.services.metadata_assembly import missing_metadata_value
from pullbox.services.metadata_series_refresh_state import RefreshEntityState
from pullbox.services.series_metadata_links import SOURCE_LABELS


def _label(origin: FieldOrigin) -> str | None:
    if origin.source is not None:
        return SOURCE_LABELS[origin.source]
    if origin.passive_release is not None:
        return "LOCG release context"
    if origin.embedded_documents:
        return " and ".join(origin.embedded_documents)
    if origin.derivation is not None:
        return {
            "catalog": "Pullbox (issue catalog)",
            "lifecycle": "Pullbox (publication history)",
            "classification": "Pullbox (series classification)",
            "normalization": "Pullbox (normalized value)",
        }[origin.derivation]
    return None


def saved_field_sources(state: RefreshEntityState) -> dict[str, str]:
    baseline = state.baseline
    labels: dict[str, str] = {}
    if baseline is None:
        return labels
    for origin in baseline.origins:
        current = getattr(state.values, origin.field)
        if (
            origin.user_override
            or origin.field in state.overrides
            or missing_metadata_value(current)
            or current != getattr(baseline.values, origin.field)
        ):
            continue
        if label := _label(origin):
            labels[origin.field] = label
    return labels
