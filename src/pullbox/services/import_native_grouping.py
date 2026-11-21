"""Partition folder review rows using each file's saved native series evidence."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pullbox.services.import_metron_matching import import_identity_namespace, uses_metron_identity

if TYPE_CHECKING:
    from pullbox.core.collection_scanner import DiscoveredFile, DiscoveredSeries


def _with_series_sidecar(file: DiscoveredFile) -> dict[str, Any] | None:
    """Add folder claims without manufacturing any per-file issue identity."""
    evidence = file.metadata_diagnostics.get("metron_import")
    sidecar = file.metadata_diagnostics.get("series_sidecar")
    if not isinstance(sidecar, dict) or not (
        sidecar.get("problems")
        or any(ref["namespace"] in {"metron", "gcd"} for ref in sidecar.get("identities", []))
    ):
        return evidence if isinstance(evidence, dict) else None
    base = (
        evidence if isinstance(evidence, dict) else {"problems": ["archive_metadata_unavailable"]}
    )
    refs = {
        (ref["namespace"], ref["kind"], ref["id"])
        for ref in [*base.get("identities", []), *sidecar.get("identities", [])]
    }
    if file.comicvine_series_id is not None:
        refs.add(("comicvine", "series", str(file.comicvine_series_id)))
    problems = set(base.get("problems", [])) | set(sidecar.get("problems", []))
    if file.metadata_diagnostics.get("identity_conflicts"):
        problems.add("sidecar_identity_conflict")
    native = sorted(
        {
            namespace
            for namespace, kind, _ in refs
            if kind == "series" and namespace in {"metron", "gcd"}
        }
    )
    primary = base.get("primary_source")
    merged = {
        **base,
        "embedded_primary_source": base.get("embedded_primary_source", primary),
        "primary_source": primary if primary in native else next(iter(native), None),
        "identities": [
            {"namespace": namespace, "kind": kind, "id": value}
            for namespace, kind, value in sorted(refs)
        ],
        "series_sidecar": sidecar,
        "problems": sorted(problems),
    }
    file.metadata_diagnostics = {**file.metadata_diagnostics, "metron_import": merged}
    member_evidence = file.metadata_diagnostics.get("archive_member_evidence")
    if isinstance(member_evidence, dict):
        file.metadata_diagnostics["archive_member_evidence"] = {
            **member_evidence,
            "metron_import": merged,
        }
    return merged


def group_native_folder_files(discovered: DiscoveredSeries) -> list[DiscoveredSeries]:
    """Separate exact identity cohorts, without granting identity to their neighbors."""
    groups: dict[tuple[str | None, tuple[tuple[str, str], ...]] | None, list[DiscoveredFile]] = {}
    for file in discovered.files:
        evidence = _with_series_sidecar(file)
        key = None
        if isinstance(evidence, dict) and uses_metron_identity(evidence):
            namespace = import_identity_namespace(evidence)
            # Keep competing cross-provider identities together as evidence, not
            # with otherwise clean files that happen to name the same primary ID.
            refs = tuple(
                sorted(
                    {
                        (ref["namespace"], ref["id"])
                        for ref in evidence.get("identities", [])
                        if ref.get("kind") == "series"
                    }
                )
            )
            key = (namespace.value if namespace else None, refs)
        groups.setdefault(key, []).append(file)
    if len(groups) <= 1:
        return [discovered]

    result = []
    for key, files in groups.items():
        diagnostics = dict(discovered.diagnostics)
        diagnostics["native_folder_grouping"] = {
            "original_series_name": discovered.raw_series_name,
            "folder_comicvine_id": discovered.folder_cv_id,
            "identity_required": key is None,
        }
        result.append(
            replace(
                discovered,
                files=files,
                file_count=len(files),
                sample_paths=[file.file_path for file in files[:5]],
                # A mixed folder's name/aggregate CV hint cannot identify a native
                # subgroup. Its own embedded cross-identities are still checked.
                folder_cv_id=None if key is not None else discovered.folder_cv_id,
                comicinfo_cv_id=None if key is not None else discovered.comicinfo_cv_id,
                comicinfo_source=None if key is not None else discovered.comicinfo_source,
                diagnostics=diagnostics,
            )
        )
    return result
