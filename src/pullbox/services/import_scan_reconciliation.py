"""Fold stale Mylar references into inspected files before staging review rows."""

from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from pullbox.core.name_matcher import NameMatcher
from pullbox.core.source_metadata import MetadataSignal, SourceMetadata, SourceMetadataExtractor
from pullbox.services.import_path_identity import (
    reconciliation_evidence,
    same_trusted_issue,
    unchanged_same_folder_pair,
)
from pullbox.services.import_source_metadata import cached_mylar_sidecar_data

if TYPE_CHECKING:
    from pullbox.core.collection_scanner import DiscoveredFile, DiscoveredSeries


def reconcile_discovered_mylar_paths(discovered_list: list[DiscoveredSeries]) -> None:
    """Reuse archive evidence; never issue another provider or archive request."""
    extractor = SourceMetadataExtractor()
    for series in discovered_list:
        missing: dict[tuple[Path, int], list[DiscoveredFile]] = defaultdict(list)
        for file in series.files:
            block = file.metadata_diagnostics.get("file_safety")
            if (
                isinstance(block, dict)
                and block.get("code") == "source_missing"
                and file.metadata_signals.get("comicvine_issue_id") == "mylar3"
                and file.comicvine_issue_id
                and not file.source_signature
            ):
                missing[(Path(file.file_path).parent, file.comicvine_issue_id)].append(file)
        if not missing:
            continue
        candidates: dict[tuple[Path, int], list[tuple[DiscoveredFile, SourceMetadata]]] = (
            defaultdict(list)
        )
        for file in series.files:
            evidence = file.metadata_diagnostics.get("archive_member_evidence")
            path = Path(file.file_path)
            if not isinstance(evidence, dict) or evidence.get("member_index_scanned") is not True:
                continue
            sidecar = cached_mylar_sidecar_data(file.metadata_diagnostics)
            metadata = extractor.from_path(
                path,
                sidecar_data=sidecar or {},
                archive_member_evidence=evidence,
                include_archive_entry_issue_hint=False,
            )
            if metadata.comicvine_issue_id:
                candidates[(path.parent, metadata.comicvine_issue_id)].append((file, metadata))
        removed: set[str] = set()
        for key, records in missing.items():
            matches = candidates.get(key, [])
            if len(records) != 1 or len(matches) != 1:
                continue
            record = records[0]
            actual, metadata = matches[0]
            if actual.metadata_diagnostics.get("file_safety") or actual.metadata_diagnostics.get(
                "identity_conflicts"
            ):
                continue
            base = SourceMetadata(
                original_title=record.file_name,
                series_name=record.parsed_series,
                issue_number=record.parsed_issue_number,
                issue_type=record.issue_type,
                comicvine_issue_id=record.comicvine_issue_id,
                comicvine_series_id=record.comicvine_series_id,
                signals={"comicvine_issue_id": MetadataSignal.MYLAR3},
                diagnostics=record.metadata_diagnostics,
            )
            if not same_trusted_issue(base, metadata) or not unchanged_same_folder_pair(
                Path(record.file_path), Path(actual.file_path), dict(actual.source_signature)
            ):
                continue
            diagnostics = dict(actual.metadata_diagnostics)
            diagnostics.pop("mylar3_folder_scope_conflict", None)
            diagnostics.pop("mylar3_unrecorded_file", None)
            recorded_issue = record.metadata_diagnostics.get("mylar3_issue")
            if isinstance(recorded_issue, dict):
                diagnostics["mylar3_issue"] = dict(recorded_issue)
            diagnostics["mylar3_path_reconciliation"] = reconciliation_evidence(
                record.file_path,
                actual.file_path,
                key[1],
                recorded_series_name=base.series_name,
                actual_series_name=metadata.series_name,
            )
            actual.metadata_diagnostics = diagnostics
            actual.parsed_series = record.parsed_series
            actual.parsed_issue_number = record.parsed_issue_number
            actual.issue_number_raw = record.issue_number_raw
            actual.issue_type = record.issue_type
            actual.comicvine_issue_id = metadata.comicvine_issue_id
            actual.comicvine_series_id = record.comicvine_series_id
            actual.has_comicinfo = True
            signals = dict(actual.metadata_signals)
            signals["comicvine_issue_id"] = MetadataSignal.COMICINFO.value
            signals["comicvine_series_id"] = MetadataSignal.COMICINFO.value
            signals["issue_number"] = MetadataSignal.COMICINFO.value
            signals["series_name"] = MetadataSignal.MYLAR3.value
            actual.metadata_signals = signals
            removed.add(record.file_path)
        if removed:
            series.files = [file for file in series.files if file.file_path not in removed]
            _refresh_series_shape(series)
    _reconcile_cross_folder_mylar_paths(discovered_list, extractor)


def _content_hash(path: str) -> str | None:
    digest = sha256()
    try:
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _source_metadata(
    file: DiscoveredFile,
    extractor: SourceMetadataExtractor,
) -> SourceMetadata | None:
    evidence = file.metadata_diagnostics.get("archive_member_evidence")
    if not isinstance(evidence, dict) or evidence.get("member_index_scanned") is not True:
        return None
    sidecar = cached_mylar_sidecar_data(file.metadata_diagnostics)
    return extractor.from_path(
        Path(file.file_path),
        sidecar_data=sidecar or {},
        archive_member_evidence=evidence,
        include_archive_entry_issue_hint=False,
    )


def _recorded_metadata(file: DiscoveredFile) -> SourceMetadata:
    return SourceMetadata(
        original_title=file.file_name,
        series_name=file.parsed_series,
        issue_number=file.parsed_issue_number,
        issue_type=file.issue_type,
        comicvine_issue_id=file.comicvine_issue_id,
        comicvine_series_id=file.comicvine_series_id,
        signals={"comicvine_issue_id": MetadataSignal.MYLAR3},
        diagnostics=file.metadata_diagnostics,
    )


def _series_issue_filename_key(
    *,
    series_id: int | None,
    issue_number: float | None,
    file_name: str,
) -> tuple[int, float, str] | None:
    if series_id is None or issue_number is None or not file_name:
        return None
    return int(series_id), float(issue_number), file_name.casefold()


def _same_trusted_series_issue_filename(
    recorded: DiscoveredFile,
    actual_file: DiscoveredFile,
    actual: SourceMetadata,
) -> bool:
    """Accept a stale Mylar issue ID only when every independent slot signal agrees."""
    if (
        not recorded.comicvine_series_id
        or actual.comicvine_series_id != recorded.comicvine_series_id
        or actual.comicvine_issue_id is None
        or actual.signals.get("comicvine_series_id") is not MetadataSignal.COMICINFO
        or actual.signals.get("comicvine_issue_id") is not MetadataSignal.COMICINFO
        or actual.signals.get("series_name") is not MetadataSignal.COMICINFO
        or actual.signals.get("issue_number") is not MetadataSignal.COMICINFO
        or recorded.file_name.casefold() != actual_file.file_name.casefold()
        or recorded.parsed_issue_number != actual.issue_number
        or recorded.issue_type != actual.issue_type
        or recorded.metadata_diagnostics.get("identity_conflicts")
        or actual.diagnostics.get("identity_conflicts")
    ):
        return False
    return bool(
        recorded.parsed_series
        and actual.series_name
        and NameMatcher.normalize(recorded.parsed_series)
        == NameMatcher.normalize(actual.series_name)
    )


def _choose_cross_folder_canonical(
    recorded: DiscoveredFile,
    candidates: list[tuple[DiscoveredSeries, DiscoveredFile, SourceMetadata]],
) -> (
    tuple[
        tuple[DiscoveredSeries, DiscoveredFile, SourceMetadata],
        list[tuple[DiscoveredSeries, DiscoveredFile, SourceMetadata]],
    ]
    | None
):
    exact_name = [item for item in candidates if item[1].file_name == recorded.file_name]
    preferred = exact_name if exact_name else candidates
    if len(preferred) == 1:
        canonical = preferred[0]
    else:
        hashes = {_content_hash(item[1].file_path) for item in preferred}
        if None in hashes or len(hashes) != 1:
            return None
        canonical = min(
            preferred, key=lambda item: (item[1].file_name.casefold(), item[1].file_path)
        )

    canonical_hash = _content_hash(canonical[1].file_path)
    identical: list[tuple[DiscoveredSeries, DiscoveredFile, SourceMetadata]] = []
    if canonical_hash is not None:
        for item in candidates:
            if item is canonical or item[1].file_size != canonical[1].file_size:
                continue
            if _content_hash(item[1].file_path) == canonical_hash:
                identical.append(item)
    return canonical, identical


def _apply_cross_folder_identity(
    file: DiscoveredFile,
    recorded: DiscoveredFile,
    metadata: SourceMetadata,
    *,
    source_series: DiscoveredSeries,
    role: str,
    method: str = "verified_cross_folder_issue_identity",
    canonical_path: str | None = None,
) -> None:
    diagnostics = dict(file.metadata_diagnostics)
    diagnostics.pop("mylar3_folder_scope_conflict", None)
    diagnostics.pop("mylar3_unrecorded_file", None)
    recorded_issue = recorded.metadata_diagnostics.get("mylar3_issue")
    if isinstance(recorded_issue, dict):
        diagnostics["mylar3_issue"] = dict(recorded_issue)
    actual_issue_id = int(metadata.comicvine_issue_id or recorded.comicvine_issue_id or 0)
    recorded_issue_id = int(recorded.comicvine_issue_id or 0)
    evidence: dict[str, object] = {
        "recorded_path": recorded.file_path,
        "actual_path": file.file_path,
        "comicvine_issue_id": actual_issue_id,
        "comicvine_series_id": int(recorded.comicvine_series_id or 0),
        "method": method,
        "role": role,
        "source_series": source_series.raw_series_name,
    }
    if recorded_issue_id != actual_issue_id:
        evidence["recorded_comicvine_issue_id"] = recorded_issue_id
    if (
        recorded.parsed_series
        and metadata.series_name
        and NameMatcher.normalize(recorded.parsed_series)
        != NameMatcher.normalize(metadata.series_name)
    ):
        evidence["series_name_alias"] = {
            "recorded": recorded.parsed_series,
            "actual": metadata.series_name,
            "accepted_by": "exact_comicvine_series_and_issue_identity",
        }
    if canonical_path is not None:
        evidence["canonical_path"] = canonical_path
    diagnostics["mylar3_cross_folder_reconciliation"] = evidence
    file.metadata_diagnostics = diagnostics
    file.parsed_series = recorded.parsed_series
    file.parsed_issue_number = recorded.parsed_issue_number
    file.issue_number_raw = recorded.issue_number_raw
    file.issue_type = recorded.issue_type
    file.comicvine_issue_id = actual_issue_id or None
    file.comicvine_series_id = recorded.comicvine_series_id
    file.has_comicinfo = True
    signals = dict(file.metadata_signals)
    signals["comicvine_issue_id"] = MetadataSignal.COMICINFO.value
    signals["comicvine_series_id"] = signals.get("comicvine_series_id", MetadataSignal.MYLAR3.value)
    signals["issue_number"] = signals.get("issue_number", MetadataSignal.MYLAR3.value)
    signals["series_name"] = signals.get("series_name", MetadataSignal.MYLAR3.value)
    file.metadata_signals = signals


def _has_diagnostic_value(
    file: DiscoveredFile,
    diagnostic_name: str,
    key: str,
    value: str,
) -> bool:
    block = file.metadata_diagnostics.get(diagnostic_name)
    return isinstance(block, dict) and block.get(key) == value


def _refresh_series_shape(series: DiscoveredSeries) -> None:
    conflicts = [
        file for file in series.files if "mylar3_folder_scope_conflict" in file.metadata_diagnostics
    ]
    diagnostics = dict(series.diagnostics)
    if conflicts:
        diagnostics["mylar3_folder_scope"] = {
            "review_required": True,
            "unrecorded_file_count": sum(
                "mylar3_issue" not in file.metadata_diagnostics for file in series.files
            ),
            "conflicting_file_count": len(conflicts),
            "examples": [file.file_name for file in conflicts[:5]],
        }
    else:
        diagnostics.pop("mylar3_folder_scope", None)

    recovered_files = [
        file
        for file in series.files
        if _has_diagnostic_value(
            file,
            "mylar3_cross_folder_reconciliation",
            "role",
            "canonical",
        )
    ]
    missing_files = [
        file
        for file in series.files
        if _has_diagnostic_value(file, "file_safety", "code", "source_missing")
    ]
    if (
        recovered_files
        and diagnostics.get("kind") == "mylar3_path_incompatible"
        and diagnostics.get("reason") == "source_missing"
    ):
        diagnostics.pop("kind", None)
        diagnostics.pop("reason", None)
        diagnostics.pop("rejection_reason", None)
        path_details = diagnostics.get("mylar3_path")
        if isinstance(path_details, dict):
            path_details = dict(path_details)
            path_details["status"] = "partially_reconciled" if missing_files else "reconciled"
            diagnostics["mylar3_path"] = path_details
        diagnostics["mylar3_path_recovery"] = {
            "status": "partial" if missing_files else "complete",
            "recovered_file_count": len(recovered_files),
            "remaining_missing_file_count": len(missing_files),
        }
    series.diagnostics = diagnostics
    series.file_count = len(series.files)
    series.sample_paths = [file.file_path for file in series.files[:5]]
    series.has_files = bool(series.files)


def _reconcile_cross_folder_mylar_paths(
    discovered_list: list[DiscoveredSeries],
    extractor: SourceMetadataExtractor,
) -> None:
    """Link proven misplaced files to one missing Mylar issue without moving sources."""
    missing_by_issue: dict[int, list[tuple[DiscoveredSeries, DiscoveredFile]]] = defaultdict(list)
    missing_by_slot: dict[
        tuple[int, float, str],
        list[tuple[DiscoveredSeries, DiscoveredFile]],
    ] = defaultdict(list)
    candidates_by_issue: dict[
        int,
        list[tuple[DiscoveredSeries, DiscoveredFile, SourceMetadata]],
    ] = defaultdict(list)
    candidates_by_slot: dict[
        tuple[int, float, str],
        list[tuple[DiscoveredSeries, DiscoveredFile, SourceMetadata]],
    ] = defaultdict(list)
    for series in discovered_list:
        for file in series.files:
            block = file.metadata_diagnostics.get("file_safety")
            if (
                isinstance(block, dict)
                and block.get("code") == "source_missing"
                and file.comicvine_issue_id
                and file.comicvine_series_id
                and file.metadata_signals.get("comicvine_issue_id") == MetadataSignal.MYLAR3.value
                and not file.source_signature
            ):
                missing_by_issue[int(file.comicvine_issue_id)].append((series, file))
                slot = _series_issue_filename_key(
                    series_id=file.comicvine_series_id,
                    issue_number=file.parsed_issue_number,
                    file_name=file.file_name,
                )
                if slot is not None:
                    missing_by_slot[slot].append((series, file))
                continue
            if (
                not file.source_signature
                or "mylar3_folder_scope_conflict" not in file.metadata_diagnostics
                or file.metadata_diagnostics.get("file_safety")
                or file.metadata_diagnostics.get("identity_conflicts")
            ):
                continue
            metadata = _source_metadata(file, extractor)
            if (
                metadata is None
                or metadata.comicvine_issue_id is None
                or metadata.signals.get("comicvine_issue_id") is not MetadataSignal.COMICINFO
            ):
                continue
            candidates_by_issue[int(metadata.comicvine_issue_id)].append((series, file, metadata))
            slot = _series_issue_filename_key(
                series_id=metadata.comicvine_series_id,
                issue_number=metadata.issue_number,
                file_name=file.file_name,
            )
            if slot is not None:
                candidates_by_slot[slot].append((series, file, metadata))

    changed_series: dict[int, DiscoveredSeries] = {}
    used_paths: set[str] = set()
    resolved_records: set[int] = set()

    def apply_choice(
        target_series: DiscoveredSeries,
        recorded: DiscoveredFile,
        canonical: tuple[DiscoveredSeries, DiscoveredFile, SourceMetadata],
        identical: list[tuple[DiscoveredSeries, DiscoveredFile, SourceMetadata]],
        *,
        method: str,
    ) -> None:
        source_series, canonical_file, metadata = canonical
        moved = [canonical, *identical]
        _apply_cross_folder_identity(
            canonical_file,
            recorded,
            metadata,
            source_series=source_series,
            role="canonical",
            method=method,
        )
        for duplicate_series, duplicate, duplicate_metadata in identical:
            _apply_cross_folder_identity(
                duplicate,
                recorded,
                duplicate_metadata,
                source_series=duplicate_series,
                role="identical_duplicate",
                method=method,
                canonical_path=canonical_file.file_path,
            )
        target_series.files = [file for file in target_series.files if file is not recorded]
        for owner, file, _metadata in moved:
            owner.files = [owned_file for owned_file in owner.files if owned_file is not file]
            changed_series[id(owner)] = owner
            used_paths.add(file.file_path)
        target_series.files.extend(item[1] for item in moved)
        changed_series[id(target_series)] = target_series
        resolved_records.add(id(recorded))

    for issue_id, records in missing_by_issue.items():
        candidates = [
            item
            for item in candidates_by_issue.get(issue_id, [])
            if item[1].file_path not in used_paths
        ]
        if len(records) != 1 or not candidates:
            continue
        target_series, recorded = records[0]
        recorded_metadata = _recorded_metadata(recorded)
        verified = [
            item
            for item in candidates
            if same_trusted_issue(recorded_metadata, item[2])
            and (
                item[2].comicvine_series_id is None
                or item[2].comicvine_series_id == recorded.comicvine_series_id
            )
        ]
        choice = _choose_cross_folder_canonical(recorded, verified) if verified else None
        if choice is None:
            continue
        canonical, identical = choice
        apply_choice(
            target_series,
            recorded,
            canonical,
            identical,
            method="verified_cross_folder_issue_identity",
        )

    for slot, records in missing_by_slot.items():
        unresolved = [item for item in records if id(item[1]) not in resolved_records]
        candidates = [
            item for item in candidates_by_slot.get(slot, []) if item[1].file_path not in used_paths
        ]
        if len(unresolved) != 1 or len(candidates) != 1:
            continue
        target_series, recorded = unresolved[0]
        candidate = candidates[0]
        if not _same_trusted_series_issue_filename(recorded, candidate[1], candidate[2]):
            continue
        apply_choice(
            target_series,
            recorded,
            candidate,
            [],
            method="verified_cross_folder_series_issue_filename",
        )

    for series in changed_series.values():
        _refresh_series_shape(series)
