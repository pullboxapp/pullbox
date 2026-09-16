from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from pullbox.core.exceptions import NotFoundError
from pullbox.core.library_policy import LibraryIngestPolicy
from pullbox.models.issue import IssueStatus
from pullbox.services import issue_import_service
from pullbox.services.issue_import_service import (
    ManualIssueImportError,
    PreparedManualIssueImport,
    execute_manual_issue_import,
    prepare_manual_issue_import,
)

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any


def _policy() -> LibraryIngestPolicy:
    return LibraryIngestPolicy(
        rename_on_import=True,
        series_folder_template="{Series Title} ({Year})",
        comic_file_template="{Series Title} #{Issue Number}",
        annual_file_template="{Series Title} Annual #{Issue Number}",
        non_standard_file_template="{Series Title} {Issue Type} #{Issue Number}",
        single_non_standard_file_template="{Series Title} {Issue Type}",
        replace_illegal_characters=True,
        colon_replacement=" -",
        post_processing_method="move",
        torrent_import_strategy="copy",
        normalize_imported_archives_to_cbz=True,
        skip_existing_files=False,
        update_embedded_comicinfo_from_match=True,
    )


def _prepared(
    source_path: Path,
    *,
    library_file: object | None = None,
    preferred_library_root_id: int | None = 84,
) -> PreparedManualIssueImport:
    issue = SimpleNamespace(
        series=SimpleNamespace(
            library_root_id=42,
            preferred_library_root_id=preferred_library_root_id,
        ),
        library_file=library_file,
    )
    return PreparedManualIssueImport(
        issue=issue,  # type: ignore[arg-type]
        issue_id=123,
        source_path=source_path,
        ingest_policy=_policy(),
    )


class _ScalarResult:
    def __init__(self, issue: object | None) -> None:
        self.issue = issue

    def unique(self) -> _ScalarResult:
        return self

    def scalar_one_or_none(self) -> object | None:
        return self.issue


class _FakeSession:
    def __init__(self, issue: object | None) -> None:
        self.issue = issue

    async def execute(self, _statement: object) -> _ScalarResult:
        return _ScalarResult(self.issue)


def _issue(
    *,
    status: IssueStatus = IssueStatus.WANTED,
    library_file: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(id=123, status=status, library_file=library_file)


async def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    issue: object | None = None,
    file_path: str | None = None,
    move_to_library: bool | None = True,
    allowed_extensions: set[str] | None = None,
) -> PreparedManualIssueImport:
    source = tmp_path / "manual.cbz"
    source.write_bytes(b"cbz")

    async def fake_allowed_extensions(_session: object) -> set[str]:
        return allowed_extensions or {".cbz"}

    async def fake_policy(_session: object) -> LibraryIngestPolicy:
        return _policy()

    monkeypatch.setattr(issue_import_service, "get_allowed_extensions", fake_allowed_extensions)
    monkeypatch.setattr(issue_import_service, "load_library_ingest_policy", fake_policy)

    return await prepare_manual_issue_import(
        _FakeSession(issue if issue is not None else _issue()),  # type: ignore[arg-type]
        issue_id=123,
        file_path=file_path if file_path is not None else str(source),
        move_to_library=move_to_library,
    )


@pytest.mark.asyncio
async def test_prepare_manual_issue_import_rejects_missing_issue(
    tmp_path: Path,
) -> None:
    source = tmp_path / "manual.cbz"
    source.write_bytes(b"cbz")

    with pytest.raises(NotFoundError):
        await prepare_manual_issue_import(
            _FakeSession(None),  # type: ignore[arg-type]
            issue_id=123,
            file_path=str(source),
            move_to_library=True,
        )


@pytest.mark.asyncio
async def test_prepare_manual_issue_import_allows_already_owned_issue_for_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared = await _prepare(
        monkeypatch,
        tmp_path,
        issue=_issue(status=IssueStatus.OWNED, library_file=object()),
    )

    assert prepared.issue_id == 123


@pytest.mark.asyncio
async def test_prepare_manual_issue_import_rejects_deprecated_non_library_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(ManualIssueImportError) as exc_info:
        await _prepare(monkeypatch, tmp_path, move_to_library=False)

    assert exc_info.value.status_code == 422
    assert "always creates a library artifact" in exc_info.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_path", "expected_detail"),
    [
        ("bad\x00path.cbz", "Invalid file path: contains null bytes"),
        ("relative.cbz", "File path must be absolute"),
    ],
)
async def test_prepare_manual_issue_import_rejects_unsafe_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    file_path: str,
    expected_detail: str,
) -> None:
    with pytest.raises(ManualIssueImportError) as exc_info:
        await _prepare(monkeypatch, tmp_path, file_path=file_path)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == expected_detail


@pytest.mark.asyncio
async def test_prepare_manual_issue_import_rejects_missing_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(ManualIssueImportError) as exc_info:
        await _prepare(monkeypatch, tmp_path, file_path=str(tmp_path / "missing.cbz"))

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "File not found on disk"


@pytest.mark.asyncio
async def test_prepare_manual_issue_import_rejects_directory_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(ManualIssueImportError) as exc_info:
        await _prepare(monkeypatch, tmp_path, file_path=str(tmp_path))

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Path is a directory, not a file"


@pytest.mark.asyncio
async def test_prepare_manual_issue_import_rejects_unsupported_extension(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"pdf")

    with pytest.raises(ManualIssueImportError) as exc_info:
        await _prepare(
            monkeypatch,
            tmp_path,
            file_path=str(source),
            allowed_extensions={".cbz"},
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Unsupported format '.pdf'. Supported: .cbz"


@pytest.mark.asyncio
async def test_prepare_manual_issue_import_returns_validated_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared = await _prepare(monkeypatch, tmp_path)

    assert prepared.issue_id == 123
    assert prepared.source_path == (tmp_path / "manual.cbz").resolve()
    assert prepared.ingest_policy == _policy()


@pytest.mark.asyncio
async def test_execute_manual_issue_import_wires_converter_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.cbr"
    converted = tmp_path / "source.cbz"
    destination = tmp_path / "converted.cbz"
    source.write_bytes(b"cbr")
    progress_calls: list[tuple[str, int, int, str]] = []
    convert_calls: list[dict[str, Any]] = []

    async def fake_convert_file(
        source_path: Path,
        target_format: str,
        target_path: Path | None = None,
        *,
        progress_callback: Any = None,
        allow_resource_safety_exception: bool = False,
    ) -> Path:
        convert_calls.append(
            {
                "source_path": source_path,
                "target_format": target_format,
                "target_path": target_path,
                "progress_callback": progress_callback,
                "allow_resource_safety_exception": allow_resource_safety_exception,
            }
        )
        assert progress_callback is not None
        progress_callback("convert", 1, 2, "Converting archive")
        return converted

    async def fake_register_library_file(
        _session: object,
        **kwargs: Any,
    ) -> object:
        converter = kwargs["converter"]
        assert converter is not None
        assert kwargs["library_root_id"] == 84
        assert kwargs["replace_existing_library_file"] is True
        assert kwargs["allow_resource_safety_exception"] is True
        assert kwargs["transfer_progress_callback"] is transfer_progress
        assert kwargs["comicinfo_progress_callback"] is comicinfo_progress
        assert kwargs["comicinfo_materializer"] is not None
        await converter(
            kwargs["source_path"],
            "cbz",
            destination,
            allow_resource_safety_exception=kwargs["allow_resource_safety_exception"],
        )
        return SimpleNamespace(id=99)

    def preparation_progress(stage: str, current: int, total: int, message: str) -> None:
        progress_calls.append((stage, current, total, message))

    def transfer_progress(_current: int, _total: int) -> None:
        raise AssertionError("transfer callback should only be forwarded")

    def comicinfo_progress(_stage: str, _current: int, _total: int, _message: str) -> None:
        raise AssertionError("comicinfo callback should only be forwarded")

    monkeypatch.setattr(issue_import_service, "convert_file", fake_convert_file)
    monkeypatch.setattr(issue_import_service, "register_library_file", fake_register_library_file)
    monkeypatch.setattr(
        issue_import_service,
        "resolve_configured_utility_trash_dir",
        AsyncMock(return_value=None),
    )

    result = await execute_manual_issue_import(
        object(),  # type: ignore[arg-type]
        _prepared(source, library_file=object()),
        allow_resource_safety_exception=True,
        preparation_progress_callback=preparation_progress,
        transfer_progress_callback=transfer_progress,
        comicinfo_progress_callback=comicinfo_progress,
    )

    assert result.issue_id == 123
    assert result.library_file.id == 99
    assert result.ingest_policy == _policy()
    assert progress_calls == [("convert", 1, 2, "Converting archive")]
    assert convert_calls == [
        {
            "source_path": source,
            "target_format": "cbz",
            "target_path": destination,
            "progress_callback": preparation_progress,
            "allow_resource_safety_exception": True,
        }
    ]


@pytest.mark.asyncio
async def test_execute_manual_issue_import_wires_threaded_comicinfo_materializer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.cbz"
    target = tmp_path / "target.cbz"
    source.write_bytes(b"cbz")
    materialize_calls: list[dict[str, Any]] = []
    progress_calls: list[tuple[str, int, int, str]] = []

    def fake_materialize_cbz_with_comicinfo(
        source_path: Path,
        target_path: Path,
        payload: dict[str, Any],
        *,
        transfer_method: str,
        progress_callback: Any = None,
    ) -> bool:
        materialize_calls.append(
            {
                "source_path": source_path,
                "target_path": target_path,
                "payload": payload,
                "transfer_method": transfer_method,
                "progress_callback": progress_callback,
            }
        )
        assert progress_callback is comicinfo_progress
        progress_callback("transferring", 5, 10, "bytes")
        progress_callback("rewriting", 1, 1, "entries")
        return True

    async def fake_register_library_file(
        _session: object,
        **kwargs: Any,
    ) -> object:
        materializer = kwargs["comicinfo_materializer"]
        assert materializer is not None
        changed = await materializer(
            kwargs["source_path"],
            target,
            {"Series": "Aliens Epic Collection"},
            transfer_method="move",
            progress_callback=kwargs["comicinfo_progress_callback"],
        )
        assert changed is True
        return SimpleNamespace(id=100)

    def comicinfo_progress(stage: str, current: int, total: int, unit: str) -> None:
        progress_calls.append((stage, current, total, unit))

    monkeypatch.setattr(
        issue_import_service,
        "materialize_cbz_with_comicinfo",
        fake_materialize_cbz_with_comicinfo,
    )
    monkeypatch.setattr(issue_import_service, "register_library_file", fake_register_library_file)

    result = await execute_manual_issue_import(
        object(),  # type: ignore[arg-type]
        _prepared(source),
        comicinfo_progress_callback=comicinfo_progress,
    )

    assert result.issue_id == 123
    assert result.library_file.id == 100
    assert progress_calls == [
        ("transferring", 5, 10, "bytes"),
        ("rewriting", 1, 1, "entries"),
    ]
    assert materialize_calls == [
        {
            "source_path": source,
            "target_path": target,
            "payload": {"Series": "Aliens Epic Collection"},
            "transfer_method": "move",
            "progress_callback": comicinfo_progress,
        }
    ]


@pytest.mark.asyncio
async def test_execute_manual_issue_import_omits_converter_without_preparation_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"cbz")

    async def fake_register_library_file(
        _session: object,
        **kwargs: Any,
    ) -> object:
        assert kwargs["converter"] is None
        assert kwargs["source_path"] == source
        assert kwargs["move_to_library"] is True
        assert kwargs["library_root_id"] is None
        return SimpleNamespace(id=101)

    monkeypatch.setattr(issue_import_service, "register_library_file", fake_register_library_file)

    result = await execute_manual_issue_import(
        object(),  # type: ignore[arg-type]
        _prepared(source, preferred_library_root_id=None),
    )

    assert result.issue_id == 123
    assert result.library_file.id == 101
