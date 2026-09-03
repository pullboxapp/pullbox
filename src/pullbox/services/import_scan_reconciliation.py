"""Fold stale Mylar references into inspected files before staging review rows."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

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
            if sidecar is None:
                continue
            metadata = extractor.from_path(
                path,
                sidecar_data=sidecar,
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
            actual.metadata_diagnostics = {
                **actual.metadata_diagnostics,
                "mylar3_path_reconciliation": reconciliation_evidence(
                    record.file_path, actual.file_path, key[1]
                ),
            }
            removed.add(record.file_path)
        if removed:
            series.files = [file for file in series.files if file.file_path not in removed]
            series.file_count = len(series.files)
            series.sample_paths = [file.file_path for file in series.files[:5]]
            series.has_files = bool(series.files)
