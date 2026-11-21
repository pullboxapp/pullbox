"""Compiled sidecars identify series, not every issue sharing their folder."""

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from pullbox.core.metadata_identity import (
    ExternalIdentityRef,
    IdentityNamespace,
    MetadataEntityKind,
)
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.core.series_sidecar import render_series_sidecar
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries
from pullbox.models.library import LibraryFile
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.schemas.import_job import ConfirmImportRequest, ImportJobCreate
from pullbox.schemas.metadata_snapshot import MetadataSnapshot, MetadataValues
from tests.integration.test_metron_folder_import import _archive, _service, _setup


def _sidecar(folder: Path, namespace: IdentityNamespace, identifier: str = "21") -> bytes:
    payload = render_series_sidecar(
        MetadataSnapshot(
            entity_kind=MetadataEntityKind.SERIES,
            identities=(ExternalIdentityRef(namespace, MetadataEntityKind.SERIES, identifier),),
            values=MetadataValues(title="Harbor Lights", year_start=2024),
        ),
        (),
        None,
        updated_at=datetime(2026, 11, 20, tzinfo=UTC),
    )
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "series.json").write_bytes(payload)
    return payload


def _comic(
    folder: Path, namespace: IdentityNamespace, *, tagged: bool = True, number: str | None = "13a"
) -> Path:
    path = folder / ("tagged.cbz" if tagged else "untagged.cbz")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("page001.jpg", b"first page")
        archive.writestr("page002.jpg", b"second page")
        if tagged:
            source = {
                IdentityNamespace.METRON: "Metron",
                IdentityNamespace.GCD: "GCD",
                IdentityNamespace.COMICVINE: "Comic Vine",
            }[namespace]
            archive.writestr(
                "MetronInfo.xml",
                f'<MetronInfo><IDS><ID source="{source}" primary="true">101</ID></IDS>'
                "<Series><Name>Harbor Lights</Name></Series>"
                + (f"<Number>{number}</Number>" if number else "")
                + "</MetronInfo>",
            )
    return path


@pytest.mark.parametrize("namespace", [IdentityNamespace.METRON, IdentityNamespace.GCD])
@pytest.mark.parametrize(
    "issue_namespace",
    [IdentityNamespace.METRON, IdentityNamespace.GCD, IdentityNamespace.COMICVINE],
)
@pytest.mark.parametrize("number", ["13a", None])
async def test_compiled_series_imports_from_verified_issue_identity(
    db_session, tmp_path, namespace, issue_namespace, number
):
    root, series, issue = await _setup(db_session, tmp_path, number="13a", namespace=namespace)
    issue_link = (await db_session.scalars(select(IssueExternalIdentity))).one()
    issue_link.identity_namespace = issue_namespace
    if issue_namespace == IdentityNamespace.COMICVINE:
        issue.comicvine_id = 101
        # Existing ComicVine catalogs can predate provider-identity rows.
        await db_session.delete(issue_link)
    await db_session.commit()
    source = tmp_path / "source"
    folder = source / "Unsorted"
    sidecar = _sidecar(folder, namespace)
    comic = _comic(folder, issue_namespace, number=number)
    original = comic.read_bytes()
    service = _service()
    job = await service.create_job(
        db_session,
        ImportJobCreate(
            source_path=str(source), source_type="filesystem", target_library_root_id=root.id
        ),
    )
    await service.start_scan(db_session, job.id)
    row = (await db_session.scalars(select(ImportedFile))).one()
    parent = await db_session.get(ImportedSeries, row.import_series_id)
    assert parent.series_id == series.id
    assert row.status == ImportedFileStatus.MATCHED
    assert row.matched_issue_id == issue.id
    assert row.match_method == f"{namespace.value}_id"
    assert (
        row.diagnostics["source_metadata"]["metron_import"]["series_sidecar"]["source"]
        == "series.json"
    )
    await service.update_file_selection(db_session, job.id, row.id, include_in_import=True)
    await service.confirm_import(db_session, job.id, ConfirmImportRequest(series_ids=[parent.id]))
    await service.run_import(db_session, job.id)
    await db_session.refresh(row)
    assert row.status == ImportedFileStatus.IMPORTED
    assert (await db_session.scalars(select(LibraryFile))).one().issue_id == issue.id
    assert await service.rollback_import(db_session, job.id)
    assert not (await db_session.scalars(select(LibraryFile))).all()
    assert comic.read_bytes() == original
    assert (folder / "series.json").read_bytes() == sidecar
    service._metadata_service._provider.search_series.assert_not_awaited()
    service._metadata_service._provider.get_series.assert_not_awaited()
    service._metadata_service._provider.get_issues_for_series.assert_not_awaited()


async def test_mixed_grouping_keeps_legacy_sidecar_conflicts(db_session, tmp_path):
    root, _, _ = await _setup(db_session, tmp_path, number="13a")
    folder = tmp_path / "source" / "Unsorted"
    data = json.loads(_sidecar(folder, IdentityNamespace.METRON))
    data["comicid"] = 999
    (folder / "series.json").write_text(json.dumps(data))
    _archive(folder / "first.cbz", number="13a")
    _archive(folder / "second.cbz", number="13a", series_id="22", issue_id="102")
    service = _service()
    job = await service.create_job(
        db_session,
        ImportJobCreate(
            source_path=str(folder.parent), source_type="filesystem", target_library_root_id=root.id
        ),
    )
    await service.start_scan(db_session, job.id)
    rows = (await db_session.scalars(select(ImportedFile))).all()
    assert len(rows) == 2
    assert all(row.matched_issue_id is None and not row.include_in_import for row in rows)


@pytest.mark.parametrize(
    "defect",
    [
        "series_link",
        "issue_link",
        "version",
        "xml_series",
        "xml_number",
        "legacy_cv",
        "cross_provider",
    ],
)
async def test_compiled_sidecar_cannot_override_conflicting_or_unverified_evidence(
    db_session, tmp_path, defect
):
    namespace = IdentityNamespace.METRON
    root, _, _ = await _setup(db_session, tmp_path, number="13a")
    folder = tmp_path / "source" / "Unsorted"
    _sidecar(folder, namespace)
    comic = _comic(folder, namespace)
    if defect in {"series_link", "issue_link"}:
        model = SeriesExternalIdentity if defect == "series_link" else IssueExternalIdentity
        link = (await db_session.scalars(select(model))).one()
        link.verification_state = IdentityVerificationState.STALE
        await db_session.commit()
    if defect in {"version", "legacy_cv", "cross_provider"}:
        data = json.loads((folder / "series.json").read_bytes())
        if defect == "version":
            data["pullbox"]["schema_version"] = 2
        elif defect == "legacy_cv":
            data["comicid"] = 999
        else:
            data["pullbox"]["snapshot"]["identities"].append(
                {"namespace": "gcd", "entity_kind": "series", "external_id": "999"}
            )
        (folder / "series.json").write_text(json.dumps(data))
    if defect in {"xml_series", "xml_number"}:
        with zipfile.ZipFile(comic) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        members["MetronInfo.xml"] = members["MetronInfo.xml"].replace(
            b"<Series>" if defect == "xml_series" else b"<Number>13a</Number>",
            b'<Series id="22">' if defect == "xml_series" else b"<Number>13b</Number>",
        )
        with zipfile.ZipFile(comic, "w") as archive:
            for name, value in members.items():
                archive.writestr(name, value)
    original, sidecar = comic.read_bytes(), (folder / "series.json").read_bytes()
    service = _service()
    job = await service.create_job(
        db_session,
        ImportJobCreate(
            source_path=str(folder.parent), source_type="filesystem", target_library_root_id=root.id
        ),
    )
    await service.start_scan(db_session, job.id)
    file = (await db_session.scalars(select(ImportedFile))).one()
    assert file.status not in {ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED}
    assert file.matched_issue_id is None and not file.include_in_import
    assert comic.read_bytes() == original
    assert (folder / "series.json").read_bytes() == sidecar
    service._metadata_service._provider.search_series.assert_not_awaited()
    service._metadata_service._provider.get_series.assert_not_awaited()
    service._metadata_service._provider.get_issues_for_series.assert_not_awaited()
