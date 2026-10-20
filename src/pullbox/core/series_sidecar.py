"""Bounded JSON output from the same canonical snapshot used by archive writers."""

import json
import math
import re
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from pullbox.core.metadata_identity import IdentityNamespace, MetadataEntityKind
from pullbox.core.source_sidecars import parse_source_sidecar
from pullbox.schemas.metadata_snapshot import MetadataSnapshot

MAX_SIDECAR_BYTES = 1024 * 1024
_PRIVATE_KEY = re.compile(r"(?:password|credential|api[_-]?key|access[_-]?token|secret)", re.I)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Existing series.json contains duplicate keys. Review it first.")
        result[key] = value
    return result


def _safe(value: object, *, depth: int = 0) -> None:
    if depth > 20:
        raise ValueError("Existing series.json is too deeply nested. Review it first.")
    if isinstance(value, dict):
        for key, item in value.items():
            if _PRIVATE_KEY.search(key):
                raise ValueError(
                    "Existing series.json contains private configuration. Review it first."
                )
            _safe(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _safe(item, depth=depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Existing series.json contains invalid numeric values. Review it first.")
    elif isinstance(value, str):
        if (
            PurePosixPath(value).is_absolute()
            or PureWindowsPath(value).is_absolute()
            or value.startswith(("../", "..\\", "file:"))
        ):
            raise ValueError("Existing series.json contains filesystem paths. Review it first.")
        try:
            url = urlsplit(value)
        except ValueError:
            return
        if url.scheme in {"http", "https"} and (
            url.username or url.password or _PRIVATE_KEY.search(url.query)
        ):
            raise ValueError("Existing series.json contains a private URL. Review it first.")


def read_series_sidecar(payload: bytes | None) -> dict[str, Any]:
    if payload is None:
        return {}
    if len(payload) > MAX_SIDECAR_BYTES:
        raise ValueError("Existing series.json exceeds the 1 MiB safety limit. Review it first.")
    try:
        result = json.loads(payload, object_pairs_hook=_pairs)
        if not isinstance(result, dict):
            raise ValueError("Expected an object")
        _safe(result)
        return result
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError(
            "Existing series.json is invalid. Repair it before writing metadata."
        ) from None


def render_series_sidecar(
    snapshot: MetadataSnapshot,
    aliases: tuple[str, ...],
    existing: bytes | None,
    *,
    updated_at: datetime,
    edited_fields: frozenset[str] = frozenset(),
) -> bytes:
    """Keep safe user keys; never turn sidecar IDs into verified library identities."""
    if snapshot.entity_kind is not MetadataEntityKind.SERIES:
        raise ValueError("A series sidecar requires a series snapshot.")
    data = read_series_sidecar(existing)
    refs = {ref.namespace.value: ref.external_id for ref in snapshot.identities}
    legacy = parse_source_sidecar(existing.decode("utf-8") if existing else "")
    if legacy.get("_identity_conflicts") or (
        legacy.get("comicid") is not None
        and str(legacy["comicid"]) != refs.get(IdentityNamespace.COMICVINE.value)
    ):
        raise ValueError(
            "Existing series.json identifies a different series. Review the match first."
        )
    prior = data.get("pullbox")
    if prior is not None:
        if not isinstance(prior, dict) or prior.get("schema_version") != 1:
            raise ValueError("Existing Pullbox sidecar version is unsupported. Review it first.")
        previous = MetadataSnapshot.model_validate(prior.get("snapshot"))
        if previous.entity_kind is not MetadataEntityKind.SERIES or not set(
            previous.identities
        ) <= set(snapshot.identities):
            raise ValueError(
                "Existing series.json provider identities disagree. Review the match first."
            )
    values = snapshot.values
    fields: dict[str, object] = {
        "name": values.title,
        "year": values.year_start,
        "publisher": values.publisher,
        "booktype": "Print" if values.series_type == "standard" else values.series_type,
        "status": values.status,
        "total_issues": values.issue_count,
    }
    if IdentityNamespace.COMICVINE.value in refs:
        fields["comicid"] = int(refs[IdentityNamespace.COMICVINE.value])
    metadata_keys = [key for key in data if key.casefold() == "metadata"]
    layers = [data]
    for key in metadata_keys:
        nested = data[key]
        if not isinstance(nested, dict):
            raise ValueError("Existing Mylar metadata is invalid. Review it first.")
        layers.append(nested)
    aliases_by_field = {
        "name": {"name", "series", "title"},
        "year": {"year", "year_start"},
        "status": {"status", "series_status"},
        "total_issues": {"total_issues", "issue_count"},
        "booktype": {"booktype", "series_type", "type"},
        "comicid": {"comicid", "comicvine_id", "comicvineid", "cv_vol_id", "cvid"},
    }
    # Only explicit edits claim these previously preserved legacy metadata keys.
    editable_aliases = {
        "description": {"description", "summary"},
        "sort_title": {"sort_title"},
        "year_end": {"year_end"},
        "volume": {"volume"},
        "language": {"language"},
        "archive_format": {"archive_format"},
    }
    for field in edited_fields & editable_aliases.keys():
        fields[field] = getattr(values, field)
        aliases_by_field[field] = editable_aliases[field]
    # Update every recognized layer, including cleared values and case variants.
    for layer in layers:
        for name, value in fields.items():
            for key in tuple(layer):
                if key.casefold() in aliases_by_field.get(name, {name}):
                    layer[key] = value
    destination = layers[-1]
    destination.update(fields)
    compiled = {
        **(
            {key: value for key, value in prior.items() if key != "updated_at"}
            if isinstance(prior, dict)
            else {}
        ),
        "schema_version": 1,
        "snapshot": snapshot.model_dump(mode="json"),
        "aliases": list(aliases),
        "links": {
            key: template.format(value)
            for key, template in {
                "comicvine": "https://comicvine.gamespot.com/volume/4050-{}/",
                "metron": "https://metron.cloud/series/{}/",
                "gcd": "https://www.comics.org/series/{}/",
            }.items()
            if (value := refs.get(key)) is not None
        },
    }
    # Fresh preview times are not a reason to rewrite an otherwise identical sidecar.
    if isinstance(prior, dict):
        comparison = {key: value for key, value in prior.items() if key != "updated_at"}
        if comparison == compiled:
            compiled["updated_at"] = prior.get("updated_at")
        else:
            compiled["updated_at"] = updated_at.isoformat()
    else:
        compiled["updated_at"] = updated_at.isoformat()
    data["pullbox"] = compiled
    _safe(data)
    encoded = (
        json.dumps(data, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()
    if len(encoded) > MAX_SIDECAR_BYTES:
        raise ValueError("Compiled series.json exceeds the 1 MiB safety limit.")
    return encoded
