"""Partition folder review rows using each file's saved native series evidence."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pullbox.services.import_metron_matching import import_identity_namespace, uses_metron_identity

if TYPE_CHECKING:
    from pullbox.core.collection_scanner import DiscoveredFile, DiscoveredSeries


def _with_series_sidecar(
    file: DiscoveredFile, *, mylar_extra: bool = False
) -> dict[str, Any] | None:
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
    raw_conflicts = file.metadata_diagnostics.get("identity_conflicts")
    conflicts = (
        raw_conflicts
        if isinstance(raw_conflicts, list)
        else [raw_conflicts]
        if raw_conflicts
        else []
    )
    # The folder's Mylar owner is not an extra file's identity. Actual sidecar
    # disagreements still block review; retain every conflict in diagnostics.
    if any(
        not (
            mylar_extra
            and isinstance(conflict, dict)
            and set(conflict) == {"field", "mylar3", "sidecar"}
            and conflict["field"] == "comicvine_series_id"
        )
        for conflict in conflicts
    ):
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


def group_native_folder_files(
    discovered: DiscoveredSeries, *, mylar_extras: bool = False
) -> list[DiscoveredSeries]:
    """Separate exact identity cohorts, without granting identity to their neighbors."""
    groups: dict[tuple[str | None, tuple[tuple[str, str], ...]] | None, list[DiscoveredFile]] = {}
    for file in discovered.files:
        evidence = _with_series_sidecar(file, mylar_extra=mylar_extras)
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


def group_mylar_native_extras(discovered: DiscoveredSeries) -> list[DiscoveredSeries]:
    """Partition unrecorded native archives without moving Mylar-owned references."""
    extras, recorded = [], []
    for file in discovered.files:
        eligible = (
            isinstance(file.metadata_diagnostics.get("mylar3_unrecorded_file"), dict)
            and not file.metadata_diagnostics.get("mylar3_issue")
            and not file.metadata_diagnostics.get("mylar3_path_reconciliation")
            and not file.metadata_diagnostics.get("mylar3_cross_folder_reconciliation")
        )
        if not eligible:
            recorded.append(file)
            continue
        original_file = file
        inherited_owner = file.metadata_signals.get("comicvine_series_id") == "mylar3"
        file = replace(
            file,
            comicvine_series_id=None if inherited_owner else file.comicvine_series_id,
            metadata_diagnostics=dict(file.metadata_diagnostics),
        )
        evidence = _with_series_sidecar(file, mylar_extra=True)
        if isinstance(evidence, dict) and uses_metron_identity(evidence):
            # A folder's inferred Mylar owner is not evidence for an unrecorded
            # extra. Explicit sidecar claims still must agree with its XML.
            sidecar = file.metadata_diagnostics.get("sidecar_identity")
            if isinstance(sidecar, dict):
                refs = list(evidence.get("identities", []))
                for field, kind in (
                    ("comicvine_series_id", "series"),
                    ("comicvine_issue_id", "issue"),
                ):
                    value = sidecar.get(field)
                    if isinstance(value, int) and value > 0:
                        refs.append({"namespace": "comicvine", "kind": kind, "id": str(value)})
                evidence = {**evidence, "identities": refs, "mylar_sidecar_identity": sidecar}
                file = replace(
                    file,
                    metadata_diagnostics={**file.metadata_diagnostics, "metron_import": evidence},
                )
            extras.append(file)
        else:
            recorded.append(original_file)
    if not extras:
        return [discovered]

    # The original source series remains, including an empty wanted-only series.
    original = replace(
        discovered,
        files=recorded,
        file_count=len(recorded),
        sample_paths=[file.file_path for file in recorded[:5]],
        has_files=discovered.has_files and bool(recorded),
    )
    native = replace(
        discovered,
        files=extras,
        file_count=len(extras),
        sample_paths=[file.file_path for file in extras[:5]],
        mylar3_cv_id=None,
        folder_cv_id=None,
        comicinfo_cv_id=None,
        comicinfo_source=None,
        diagnostics={
            **discovered.diagnostics,
            "mylar3_native_extra": {
                "source_series_name": discovered.raw_series_name,
                "source_cv_id": discovered.mylar3_cv_id,
            },
        },
    )
    return [original, *group_native_folder_files(native, mylar_extras=True)]
