"""Build deterministic import fixtures from a read-only Local Comic Vine catalog."""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from scripts.iu9_acceptance_fixtures.shared import create_deterministic_cbz, sha256_file

FixtureProfile = Literal["balanced", "realistic-skew"]
LayoutProfile = Literal["series", "mixed"]
_INVALID_SEGMENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class FixtureRequest:
    """One exact fixture-generation request."""

    catalog_path: Path
    output_path: Path
    series_count: int
    file_count: int
    seed: int = 1300
    profile: FixtureProfile = "balanced"
    max_issues_per_series: int = 250
    layout_profile: LayoutProfile = "series"


@dataclass(frozen=True, slots=True)
class PlannedIssue:
    """A genuine Comic Vine issue selected for the fixture."""

    issue_id: int
    volume_id: int
    name: str | None
    issue_number: str
    year: int | None


@dataclass(frozen=True, slots=True)
class PlannedSeries:
    """A selected Comic Vine volume and its exact fixture issues."""

    volume_id: int
    name: str
    start_year: int | None
    publisher: str | None
    issues: tuple[PlannedIssue, ...]


@dataclass(frozen=True, slots=True)
class FixturePlan:
    """A deterministic plan that can be inspected before filesystem generation."""

    seed: int
    profile: FixtureProfile
    max_issues_per_series: int
    layout_profile: LayoutProfile
    series: tuple[PlannedSeries, ...]


@dataclass(frozen=True, slots=True)
class _CatalogSeries:
    volume_id: int
    name: str
    start_year: int | None
    publisher: str | None
    issue_count: int


def _validate_request(request: FixtureRequest) -> None:
    if request.series_count < 1:
        raise ValueError("series_count must be positive")
    if request.file_count < request.series_count:
        raise ValueError("file_count must be at least series_count")
    if request.max_issues_per_series < 1:
        raise ValueError("max_issues_per_series must be positive")
    if request.profile not in {"balanced", "realistic-skew"}:
        raise ValueError(f"Unsupported fixture profile: {request.profile}")
    if request.layout_profile not in {"series", "mixed"}:
        raise ValueError(f"Unsupported layout profile: {request.layout_profile}")
    if not request.catalog_path.is_file():
        raise FileNotFoundError(f"Comic Vine catalog does not exist: {request.catalog_path}")


def _open_catalog(path: Path) -> sqlite3.Connection:
    encoded_path = quote(str(path.expanduser().absolute()), safe="/")
    connection = sqlite3.connect(f"file:{encoded_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA temp_store = MEMORY")
    return connection


def _catalog_year(value: object) -> int | None:
    text = str(value).strip() if value is not None else ""
    if len(text) != 4 or not text.isdigit():
        return None
    return int(text)


def _load_catalog_series(connection: sqlite3.Connection) -> list[_CatalogSeries]:
    rows = connection.execute(
        """
        SELECT
            volume.id AS volume_id,
            volume.name AS volume_name,
            volume.start_year AS start_year,
            publisher.name AS publisher_name,
            COUNT(issue.id) AS issue_count
        FROM cv_volume AS volume
        JOIN cv_issue AS issue ON issue.volume_id = volume.id
        LEFT JOIN cv_publisher AS publisher ON publisher.id = volume.publisher_id
        WHERE TRIM(COALESCE(volume.name, '')) <> ''
          AND TRIM(COALESCE(issue.issue_number, '')) <> ''
        GROUP BY volume.id, volume.name, volume.start_year, publisher.name
        ORDER BY volume.id
        """
    ).fetchall()
    return [
        _CatalogSeries(
            volume_id=int(row["volume_id"]),
            name=str(row["volume_name"]),
            start_year=_catalog_year(row["start_year"]),
            publisher=str(row["publisher_name"]) if row["publisher_name"] else None,
            issue_count=int(row["issue_count"]),
        )
        for row in rows
    ]


def _balanced_assignments(
    candidates: list[_CatalogSeries], request: FixtureRequest
) -> list[tuple[_CatalogSeries, int]]:
    base = request.file_count // request.series_count
    shuffled = candidates.copy()
    random.Random(request.seed).shuffle(shuffled)
    selected: list[_CatalogSeries] = []
    selected_ids: set[int] = set()
    for threshold in range(base, 0, -1):
        for candidate in shuffled:
            if candidate.volume_id in selected_ids or candidate.issue_count < threshold:
                continue
            selected.append(candidate)
            selected_ids.add(candidate.volume_id)
            if len(selected) == request.series_count:
                break
        if len(selected) == request.series_count:
            break
    if len(selected) != request.series_count:
        raise ValueError(
            "Comic Vine catalog capacity is insufficient for the requested series count"
        )

    assignments = [min(base, candidate.issue_count) for candidate in selected]
    remaining = request.file_count - sum(assignments)
    while remaining:
        progressed = False
        for index, candidate in enumerate(selected):
            capacity = min(candidate.issue_count, request.max_issues_per_series)
            if assignments[index] >= capacity:
                continue
            assignments[index] += 1
            remaining -= 1
            progressed = True
            if remaining == 0:
                break
        if not progressed:
            raise ValueError(
                "Comic Vine catalog capacity is insufficient for the requested file count"
            )
    return list(zip(selected, assignments, strict=True))


def _realistic_skew_assignments(
    candidates: list[_CatalogSeries], request: FixtureRequest
) -> list[tuple[_CatalogSeries, int]]:
    shuffled = candidates.copy()
    random.Random(request.seed).shuffle(shuffled)
    if len(shuffled) < request.series_count:
        raise ValueError(
            "Comic Vine catalog capacity is insufficient for the requested series count"
        )
    selected = shuffled[: request.series_count]
    unselected = shuffled[request.series_count :]

    def capacity(candidate: _CatalogSeries) -> int:
        return min(candidate.issue_count, request.max_issues_per_series)

    while sum(capacity(candidate) for candidate in selected) < request.file_count:
        lowest_index = min(range(len(selected)), key=lambda index: capacity(selected[index]))
        if not unselected:
            break
        highest_index = max(range(len(unselected)), key=lambda index: capacity(unselected[index]))
        if capacity(unselected[highest_index]) <= capacity(selected[lowest_index]):
            break
        selected[lowest_index], unselected[highest_index] = (
            unselected[highest_index],
            selected[lowest_index],
        )

    capacities = [capacity(candidate) for candidate in selected]
    if sum(capacities) < request.file_count:
        raise ValueError(
            "Comic Vine catalog capacity is insufficient for the requested file count and cap"
        )
    assignments = [1] * len(selected)
    remaining = request.file_count - len(selected)
    available = [item - 1 for item in capacities]
    total_available = sum(available)
    quotas = [remaining * item / total_available for item in available]
    floors = [int(quota) for quota in quotas]
    assignments = [current + extra for current, extra in zip(assignments, floors, strict=True)]
    residual = remaining - sum(floors)
    remainder_order = sorted(
        range(len(selected)),
        key=lambda index: (quotas[index] - floors[index], -index),
        reverse=True,
    )
    for index in remainder_order[:residual]:
        assignments[index] += 1
    return list(zip(selected, assignments, strict=True))


def _stable_issue_seed(seed: int, volume_id: int) -> int:
    digest = hashlib.sha256(f"{seed}:{volume_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _issue_year(value: object, fallback: int | None) -> int | None:
    if isinstance(value, str) and len(value) >= 4 and value[:4].isdigit():
        return int(value[:4])
    return fallback


def _load_selected_issues(
    connection: sqlite3.Connection,
    assignments: list[tuple[_CatalogSeries, int]],
    *,
    seed: int,
) -> dict[int, tuple[PlannedIssue, ...]]:
    candidates_by_volume: dict[int, list[PlannedIssue]] = defaultdict(list)
    series_by_id = {candidate.volume_id: candidate for candidate, _count in assignments}
    volume_ids = list(series_by_id)
    for offset in range(0, len(volume_ids), 500):
        batch = volume_ids[offset : offset + 500]
        placeholders = ",".join("?" for _item in batch)
        rows = connection.execute(
            f"""
            SELECT id, volume_id, name, issue_number, cover_date
            FROM cv_issue
            WHERE volume_id IN ({placeholders})
              AND TRIM(COALESCE(issue_number, '')) <> ''
            ORDER BY volume_id, id
            """,
            batch,
        ).fetchall()
        for row in rows:
            volume_id = int(row["volume_id"])
            candidates_by_volume[volume_id].append(
                PlannedIssue(
                    issue_id=int(row["id"]),
                    volume_id=volume_id,
                    name=str(row["name"]) if row["name"] else None,
                    issue_number=str(row["issue_number"]),
                    year=_issue_year(row["cover_date"], series_by_id[volume_id].start_year),
                )
            )

    selected: dict[int, tuple[PlannedIssue, ...]] = {}
    for candidate, requested_count in assignments:
        issues = candidates_by_volume[candidate.volume_id]
        random.Random(_stable_issue_seed(seed, candidate.volume_id)).shuffle(issues)
        chosen = tuple(sorted(issues[:requested_count], key=lambda issue: issue.issue_id))
        if len(chosen) != requested_count:
            raise ValueError(
                f"Comic Vine catalog capacity changed for volume {candidate.volume_id}"
            )
        selected[candidate.volume_id] = chosen
    return selected


def plan_import_scale_fixture(request: FixtureRequest) -> FixturePlan:
    """Select an exact deterministic workload without modifying the filesystem."""
    _validate_request(request)
    with _open_catalog(request.catalog_path) as connection:
        candidates = _load_catalog_series(connection)
        if request.profile == "balanced":
            assignments = _balanced_assignments(candidates, request)
        else:
            assignments = _realistic_skew_assignments(candidates, request)
        issues_by_volume = _load_selected_issues(connection, assignments, seed=request.seed)
    series = tuple(
        PlannedSeries(
            volume_id=candidate.volume_id,
            name=candidate.name,
            start_year=candidate.start_year,
            publisher=candidate.publisher,
            issues=issues_by_volume[candidate.volume_id],
        )
        for candidate, _count in assignments
    )
    return FixturePlan(
        seed=request.seed,
        profile=request.profile,
        max_issues_per_series=request.max_issues_per_series,
        layout_profile=request.layout_profile,
        series=series,
    )


def _xml_safe(value: str | None) -> str | None:
    if value is None:
        return None
    return "".join(
        character
        for character in value
        if character in "\t\n\r"
        or "\x20" <= character <= "\ud7ff"
        or "\ue000" <= character <= "\ufffd"
    )


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    encoded = encoded[:max_bytes]
    while encoded:
        try:
            return encoded.decode("utf-8").rstrip(" .")
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return "Unknown"


def _safe_segment(value: str | None, *, max_bytes: int = 100) -> str:
    normalized = unicodedata.normalize("NFC", _xml_safe(value) or "Unknown")
    cleaned = _INVALID_SEGMENT.sub("-", normalized)
    cleaned = " ".join(cleaned.split()).strip(" .") or "Unknown"
    if cleaned.casefold().split(".", maxsplit=1)[0].upper() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return _truncate_utf8(cleaned, max_bytes)


def _series_directory(
    series: PlannedSeries,
    index: int,
    *,
    layout_profile: LayoutProfile,
) -> Path:
    year = str(series.start_year) if series.start_year is not None else "Unknown Year"
    identity_suffix = f" ({year}) [cv-{series.volume_id}]"
    name_budget = max(1, 140 - len(identity_suffix.encode("utf-8")))
    series_name = _safe_segment(series.name, max_bytes=name_budget)
    series_segment = f"{series_name}{identity_suffix}"
    if layout_profile == "series":
        return Path(series_segment)
    layout = index % 10
    if layout < 7:
        return Path(series_segment)
    if layout < 9:
        return Path("Publishers") / _safe_segment(series.publisher) / series_segment
    initial = _safe_segment(series.name[:1].upper() or "#", max_bytes=8)
    return Path("Collections") / initial / series_segment


def _issue_filename(issue: PlannedIssue) -> str:
    number = _safe_segment(issue.issue_number, max_bytes=24)
    title = _safe_segment(issue.name or "Untitled", max_bytes=72)
    return f"Issue {number} - {title} [cv-issue-{issue.issue_id}].cbz"


def _selection_digest(plan: FixturePlan) -> str:
    digest = hashlib.sha256()
    for series in plan.series:
        digest.update(f"v:{series.volume_id}\n".encode())
        for issue in series.issues:
            digest.update(f"i:{issue.issue_id}:{issue.issue_number}\n".encode())
    return digest.hexdigest()


def generate_import_scale_fixture(request: FixtureRequest) -> dict[str, object]:
    """Generate an immutable-style source tree and streaming evidence manifest."""
    output = request.output_path.expanduser().absolute()
    if output.exists():
        raise FileExistsError(f"Fixture output must not already exist: {output}")
    plan = plan_import_scale_fixture(request)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.building-", dir=output.parent))
    manifest_path = staging / "fixture-manifest.jsonl"
    path_samples: list[str] = []
    issue_distribution: dict[str, int] = defaultdict(int)
    content_digest = hashlib.sha256()
    logical_bytes = 0
    try:
        with manifest_path.open("w", encoding="utf-8", newline="\n") as manifest:
            for series_index, series in enumerate(plan.series):
                directory = _series_directory(
                    series,
                    series_index,
                    layout_profile=request.layout_profile,
                )
                issue_distribution[str(len(series.issues))] += 1
                for issue in series.issues:
                    relative_path = directory / _issue_filename(issue)
                    relative_text = relative_path.as_posix()
                    archive_path = staging / "source" / relative_path
                    create_deterministic_cbz(
                        archive_path,
                        seed=request.seed,
                        case_id=f"cv-{series.volume_id}-{issue.issue_id}",
                        series=_xml_safe(series.name) or "Unknown",
                        number=_xml_safe(issue.issue_number) or "Unknown",
                        title=_xml_safe(issue.name),
                        year=issue.year,
                        publisher=_xml_safe(series.publisher),
                        comicvine_series_id=series.volume_id,
                        comicvine_issue_id=issue.issue_id,
                        page_count=1,
                    )
                    archive_digest = sha256_file(archive_path)
                    archive_size = archive_path.stat().st_size
                    logical_bytes += archive_size
                    row = {
                        "archive_sha256": archive_digest,
                        "archive_size": archive_size,
                        "issue_id": issue.issue_id,
                        "issue_number": _xml_safe(issue.issue_number),
                        "issue_title": _xml_safe(issue.name),
                        "publisher": _xml_safe(series.publisher),
                        "relative_path": relative_text,
                        "series_name": _xml_safe(series.name),
                        "start_year": series.start_year,
                        "volume_id": series.volume_id,
                    }
                    manifest.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                    content_digest.update(f"{relative_text}\0{archive_digest}\n".encode())
                    if len(path_samples) < 10:
                        path_samples.append(relative_text)
        summary: dict[str, object] = {
            "schema_version": 1,
            "fixture_kind": "comicvine-import-scale",
            "profile": request.profile,
            "seed": request.seed,
            "series_count": len(plan.series),
            "file_count": sum(len(series.issues) for series in plan.series),
            "max_issues_per_series": request.max_issues_per_series,
            "layout_profile": request.layout_profile,
            "issue_count_distribution": dict(
                sorted(issue_distribution.items(), key=lambda row: int(row[0]))
            ),
            "logical_bytes": logical_bytes,
            "selection_sha256": _selection_digest(plan),
            "content_sha256": content_digest.hexdigest(),
            "path_samples": path_samples,
        }
        (staging / "fixture-summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.rename(output)
        return summary
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
