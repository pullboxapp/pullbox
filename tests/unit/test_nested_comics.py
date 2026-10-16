"""Nested comic normalization never modifies the source or guesses an identity."""

import io
import json
import zipfile
from pathlib import Path

import pytest

from pullbox.core.file_safety import run_safety_checks
from pullbox.core.nested_comics import inspect_nested_comic, normalize_nested_comic
from pullbox.services.import_content_inspection import inspect_import_content


def wrapper(
    tmp_path: Path,
    *,
    outer: bytes | None = None,
    inner: bytes | None = None,
    extra: tuple[str, bytes] | None = None,
) -> Path:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("001.jpg", b"page one")
        archive.writestr("002.jpg", b"page two")
        if inner is not None:
            archive.writestr("ComicInfo.xml", inner)
        if extra is not None:
            archive.writestr(*extra)
    path = tmp_path / "wrapped.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("comic.cbz", payload.getvalue())
        if outer is not None:
            archive.writestr("ComicInfo.xml", outer)
    return path


XML = b"<ComicInfo><Series>Batman</Series><Number>01</Number><Custom>keep</Custom></ComicInfo>"


def test_identifies_single_comic_and_preserves_source_and_metadata(tmp_path: Path) -> None:
    path = wrapper(tmp_path, outer=XML)
    original = path.read_bytes()
    report = inspect_nested_comic(path)
    assert report is not None
    assert report["eligible"] is True
    assert report["page_count"] == 2
    assert report["metadata_sources"] == ["outer"]
    target = tmp_path / "normalized.cbz"
    normalize_nested_comic(path, target)
    with zipfile.ZipFile(target) as archive:
        assert archive.namelist() == ["001.jpg", "002.jpg", "ComicInfo.xml"]
        assert archive.read("ComicInfo.xml") == XML
        assert archive.testzip() is None
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "inner",
    [
        b"<ComicInfo><Series>Other</Series><Number>1</Number></ComicInfo>",
        b"<ComicInfo><Series>Batman</Series><Number>2</Number></ComicInfo>",
    ],
)
def test_identity_disagreement_needs_review(tmp_path: Path, inner: bytes) -> None:
    path = wrapper(tmp_path, outer=XML, inner=inner)
    report = inspect_nested_comic(path)
    assert report is not None and report["eligible"] is False
    assert report["reason"] == "metadata_conflict"
    with pytest.raises(ValueError, match="metadata_conflict"):
        normalize_nested_comic(path, tmp_path / "output.cbz")


def test_agreeing_metadata_fills_missing_fields_without_losing_custom_xml(tmp_path: Path) -> None:
    path = wrapper(
        tmp_path,
        outer=XML,
        inner=b"<ComicInfo><Series>Batman</Series><Number>1</Number><Writer>A</Writer></ComicInfo>",
    )
    target = tmp_path / "output.cbz"
    normalize_nested_comic(path, target)
    with zipfile.ZipFile(target) as archive:
        data = archive.read("ComicInfo.xml")
    assert b"<Custom>keep</Custom>" in data and b"<Writer>A</Writer>" in data


@pytest.mark.parametrize(
    "extra", [("other.cbz", b"PK"), ("../evil.jpg", b"bad"), ("run.exe", b"bad")]
)
def test_ambiguous_or_unsafe_inner_is_never_repaired(
    tmp_path: Path, extra: tuple[str, bytes]
) -> None:
    path = wrapper(tmp_path, extra=extra)
    report = inspect_nested_comic(path)
    assert report is not None and report["eligible"] is False
    with pytest.raises(ValueError):
        normalize_nested_comic(path, tmp_path / "output.cbz")
    assert not (tmp_path / "output.cbz").exists()


def test_combined_budget_does_not_reset_at_inner_boundary(tmp_path: Path) -> None:
    path = wrapper(tmp_path, outer=XML)
    with zipfile.ZipFile(path) as archive:
        outer_total = sum(info.file_size for info in archive.infolist())
    report = inspect_nested_comic(path, max_bytes=outer_total)
    assert report is not None and report["eligible"] is False
    assert report["reason"] == "resource_limit"


def test_output_never_overwrites_existing_file(tmp_path: Path) -> None:
    path = wrapper(tmp_path, outer=XML)
    target = tmp_path / "output.cbz"
    target.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        normalize_nested_comic(path, target)
    assert target.read_bytes() == b"existing"


@pytest.mark.parametrize("name", ["another.cbz", "cover.jpg"])
def test_multiple_comics_and_mixed_wrappers_require_manual_review(tmp_path, name):
    path = wrapper(tmp_path)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(name, b"content")
    assert inspect_nested_comic(path)["eligible"] is False


def test_duplicate_identity_fields_and_cross_field_provider_conflicts_are_blocked(tmp_path):
    path = wrapper(
        tmp_path,
        outer=b"<ComicInfo><Web>https://comicvine.gamespot.com/a/4000-123/</Web></ComicInfo>",
        inner=b"<ComicInfo><Notes>[cv_issue_id:456]</Notes></ComicInfo>",
    )
    assert inspect_nested_comic(path)["reason"] == "metadata_conflict"
    path = wrapper(
        tmp_path,
        inner=b"<ComicInfo><Series>Batman</Series><Series>Other</Series></ComicInfo>",
    )
    assert inspect_nested_comic(path)["eligible"] is False


async def test_approved_metadata_replaces_outer_inventory_cache(tmp_path):
    from datetime import UTC, datetime

    from pullbox.core.library_file_ownership import build_file_identity_signature
    from pullbox.models.import_job import ImportedFile, ImportedSeries
    from pullbox.services.import_safety_bulk_review import _approve_nested_file
    from pullbox.services.import_source_metadata import (
        load_deferred_source_metadata_for_import_file,
    )

    path = wrapper(tmp_path, inner=XML)
    file = ImportedFile(
        file_path=str(path),
        file_name=path.name,
        source_signature=build_file_identity_signature(path),
        diagnostics={
            "archive_member_evidence": {"member_index_scanned": True, "comicinfo_entry_count": 0},
            "source_metadata": {"nested_comic": inspect_nested_comic(path)},
        },
    )
    _approve_nested_file(file, datetime.now(UTC))
    metadata = await load_deferred_source_metadata_for_import_file(
        ImportedSeries(raw_series_name="unknown"), file
    )
    assert metadata.series_name == "Batman"
    assert metadata.issue_number == 1
    from pullbox.services.import_file_matching import _persist_deferred_source_evidence

    _persist_deferred_source_evidence(file, metadata)
    assert file.diagnostics["source_metadata"]["nested_comic"]["eligible"] is True


async def test_cancellation_cleans_owned_scratch_and_keeps_original(tmp_path, monkeypatch):
    from pullbox.core.exceptions import JobCancelledError
    from pullbox.core.library_file_ownership import build_file_identity_signature
    from pullbox.models.import_job import ImportedFile, ImportFileHandlingMode, ImportJob
    from pullbox.services.import_file_preparation import prepare_import_file

    path = wrapper(tmp_path, outer=XML)
    original = path.read_bytes()
    signature = build_file_identity_signature(path)
    file = ImportedFile(
        file_path=str(path),
        file_name=path.name,
        source_signature=signature,
        diagnostics={
            "source_metadata": {"nested_comic": inspect_nested_comic(path)},
            "nested_repair": {"approved": True, "source_signature": signature},
        },
    )
    job = ImportJob(
        move_to_library=True,
        effective_transfer_method="copy",
        file_handling_mode=ImportFileHandlingMode.MANAGED_COPY,
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(
        "pullbox.services.import_file_preparation.tempfile.mkdtemp", lambda **kwargs: str(scratch)
    )

    async def cancel():
        raise JobCancelledError("test cancellation")

    with pytest.raises(JobCancelledError):
        await prepare_import_file(job, file, cancellation_check=cancel)
    assert not scratch.exists()
    assert path.read_bytes() == original


async def test_approval_does_not_survive_source_change(tmp_path):
    from pullbox.core.library_file_ownership import build_file_identity_signature
    from pullbox.models.import_job import ImportedFile, ImportFileHandlingMode, ImportJob
    from pullbox.services.import_file_preparation import prepare_import_file

    path = wrapper(tmp_path, outer=XML)
    signature = build_file_identity_signature(path)
    file = ImportedFile(
        file_path=str(path),
        file_name=path.name,
        source_signature=signature,
        diagnostics={
            "source_metadata": {"nested_comic": inspect_nested_comic(path)},
            "nested_repair": {"approved": True, "source_signature": signature},
        },
    )
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("added.txt", b"changed")
    from pullbox.core.library_file_ownership import ReferencedFileValidationError

    with pytest.raises(ReferencedFileValidationError, match="changed"):
        await prepare_import_file(
            ImportJob(
                move_to_library=True,
                effective_transfer_method="copy",
                file_handling_mode=ImportFileHandlingMode.MANAGED_COPY,
            ),
            file,
        )


@pytest.mark.parametrize(
    ("changed_evidence", "reason"),
    [
        ("signature", "source_changed"),
        ("report", "source_changed"),
        ("missing", "source_signature_missing"),
        ("unsupported", "source_signature_unsupported"),
    ],
)
async def test_worker_source_change_preserves_review_reason(
    tmp_path: Path, changed_evidence: str, reason: str
) -> None:
    from pullbox.core.library_file_ownership import (
        ReferencedFileValidationError,
        build_file_identity_signature,
    )
    from pullbox.utilities.executors.archive_subprocess import repair_nested_comic_interruptible

    source = wrapper(tmp_path, outer=XML)
    original = source.read_bytes()
    signature = build_file_identity_signature(source)
    report = inspect_nested_comic(source)
    assert report is not None
    if changed_evidence == "signature":
        signature["size"] = len(original) + 1
    elif changed_evidence == "missing":
        signature.clear()
    elif changed_evidence == "unsupported":
        signature["schema_version"] = 2
    else:
        report["page_count"] = 99
    target = tmp_path / "normalized.cbz"
    with pytest.raises(ReferencedFileValidationError) as error:
        await repair_nested_comic_interruptible(
            source,
            target,
            max_bytes=2_000_000,
            expected_signature=signature,
            expected_report=report,
        )
    assert error.value.reason == reason
    assert source.read_bytes() == original
    assert not target.exists()


def test_worker_preserves_post_normalization_source_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pullbox.core.library_file_ownership import (
        ReferencedFileValidationError,
        build_file_identity_signature,
    )
    from pullbox.utilities.executors import archive_subprocess

    source = wrapper(tmp_path, outer=XML)
    target = tmp_path / "normalized.cbz"
    payload = {
        "source": str(source),
        "target": str(target),
        "max_bytes": 2_000_000,
        "expected_signature": build_file_identity_signature(source),
        "expected_report": inspect_nested_comic(source),
        "progress_path": str(tmp_path / "progress.json"),
    }

    def change_source(*args, **kwargs):
        normalize_nested_comic(*args, **kwargs)
        with source.open("ab") as stream:
            stream.write(b"changed during repair")

    monkeypatch.setattr("pullbox.core.nested_comics.normalize_nested_comic", change_source)
    assert archive_subprocess._worker_main(["--worker", "nested_repair", json.dumps(payload)]) == 1
    stderr = capsys.readouterr().err.encode()
    with pytest.raises(ReferencedFileValidationError) as error:
        archive_subprocess._raise_worker_error("nested_repair", payload, b"", stderr, returncode=1)
    assert error.value.reason == "source_changed"


def test_scan_reports_nested_comic_instead_of_empty_archive(tmp_path: Path) -> None:
    path = wrapper(tmp_path, outer=XML)
    inspection = run_safety_checks(path, block_dangerous=True, max_archive_size=2000 * 1024 * 1024)
    report = inspect_import_content(path, inspection)
    assert report["file_safety"]["category"] == "nested_comic_archive"
    assert report["file_safety"]["overrideable"] is False
    assert report["nested_comic"]["eligible"] is True


def test_nested_repair_has_conversion_progress_even_when_cbz_conversion_is_off():
    from types import SimpleNamespace

    from pullbox.services.import_active_file_progress import (
        ActiveFileProgressSettings,
        active_file_stage_plan,
    )

    plan = active_file_stage_plan(
        ActiveFileProgressSettings(True, False, False),
        SimpleNamespace(file_path="wrapper.cbz", diagnostics={"nested_repair": {"approved": True}}),
    )
    assert [name for name, _ in plan] == ["extracting", "packing", "transferring", "finalizing"]


@pytest.mark.asyncio
async def test_preparation_requires_approval_and_never_rewrites_in_place(tmp_path: Path) -> None:
    from pullbox.core.exceptions import ValidationError
    from pullbox.core.library_file_ownership import build_file_identity_signature
    from pullbox.models.import_job import ImportedFile, ImportFileHandlingMode, ImportJob
    from pullbox.services.import_file_preparation import cleanup_prepared_file, prepare_import_file

    path = wrapper(tmp_path, outer=XML)
    before = path.read_bytes()
    signature = build_file_identity_signature(path)
    file = ImportedFile(
        file_path=str(path),
        file_name=path.name,
        source_signature=signature,
        diagnostics={"source_metadata": {"nested_comic": inspect_nested_comic(path)}},
    )
    job = ImportJob(
        move_to_library=True,
        effective_transfer_method="copy",
        file_handling_mode=ImportFileHandlingMode.MANAGED_COPY,
    )
    with pytest.raises(ValidationError, match="approval"):
        await prepare_import_file(job, file)
    file.diagnostics = {
        **file.diagnostics,
        "nested_repair": {"approved": True, "source_signature": signature},
    }
    prepared = await prepare_import_file(job, file)
    assert prepared.converted is True
    assert prepared.original_source == path
    assert prepared.registration_source != path
    assert inspect_nested_comic(prepared.registration_source) is None
    cleanup_prepared_file(prepared)
    job.file_handling_mode = ImportFileHandlingMode.IN_PLACE
    job.move_to_library = False
    with pytest.raises(ValidationError, match="Copy"):
        await prepare_import_file(job, file)
    assert path.read_bytes() == before
