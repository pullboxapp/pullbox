"""Read explicit ComicVine identities from legacy and Mylar sidecars."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

_CV_HOSTS = frozenset(
    {"comicvine.gamespot.com", "www.comicvine.gamespot.com", "comicvine.com", "www.comicvine.com"}
)
_SERIES_KEYS = ("comicid", "comicvine_id", "comicvineid", "cv_vol_id", "cvid")
_URL_KEYS = ("url", "web", "comicvine_url")


def _volume_url_id(value: str) -> int | None:
    try:
        url = urlsplit(value.strip())
        if url.scheme not in {"http", "https"} or url.hostname not in _CV_HOSTS:
            return None
    except ValueError:
        return None
    match = re.search(r"(?:^|/)4050-([0-9]{1,15})(?:/|$)", url.path)
    return int(match[1]) if match and int(match[1]) > 0 else None


def _volume_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    match = re.fullmatch(r"(?:4050-)?([0-9]{1,15})", str(value).strip())
    return int(match[1]) if match and int(match[1]) > 0 else None


def parse_source_sidecar(raw_text: str) -> dict[str, Any]:
    """Keep local IDs out of the ComicVine namespace and retain true conflicts."""
    raw_text = raw_text.strip()
    if not raw_text:
        return {}
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        parsed = None
    candidates: list[tuple[str, int]] = []
    if isinstance(parsed, dict):
        data = {str(key).lower(): value for key, value in parsed.items()}
        nested = data.pop("metadata", None)
        layers = [("root", data)]
        if isinstance(nested, dict):
            metadata = {str(key).lower(): value for key, value in nested.items()}
            layers.append(("metadata", metadata))
            data = {**data, **metadata}
        for scope, layer in layers:
            for key in _SERIES_KEYS:
                cv_id = _volume_id(layer.get(key))
                if cv_id is not None:
                    candidates.append((f"{scope}.{key}", cv_id))
            for key in _URL_KEYS:
                cv_id = _volume_url_id(str(layer.get(key) or ""))
                if cv_id is not None:
                    candidates.append((f"{scope}.{key}", cv_id))
    else:
        data = {}
        for line in raw_text.splitlines():
            cv_id = _volume_url_id(line)
            if cv_id is not None:
                candidates.append(("comicvine_volume_url", cv_id))
                continue
            if ":" in line:
                key, value = line.split(":", 1)
            elif "=" in line:
                key, value = line.split("=", 1)
            else:
                continue
            key = key.strip().lower()
            data[key] = value.strip()
            cv_id = (
                _volume_id(value)
                if key in _SERIES_KEYS
                else _volume_url_id(value)
                if key in _URL_KEYS
                else None
            )
            if cv_id is not None:
                candidates.append((key, cv_id))
    # An unqualified series_id may belong to another application, not ComicVine.
    data.pop("series_id", None)
    data.pop("comicid", None)
    data["_identity_conflicts"] = []
    if candidates:
        first_source, first_id = candidates[0]
        data["comicid"] = first_id
        for source, cv_id in candidates[1:]:
            if cv_id != first_id:
                data["_identity_conflicts"].append(
                    {
                        "field": "comicvine_series_id",
                        "first": first_id,
                        "conflicting": cv_id,
                        "first_source": first_source,
                        "conflicting_source": source,
                    }
                )
    return data
