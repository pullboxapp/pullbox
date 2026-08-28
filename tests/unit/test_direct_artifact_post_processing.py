"""Adapter tests for handing direct artifacts to existing post-processing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import pullbox.services.direct_artifact_post_processing as post_processing
from pullbox.models.issue import IssueStatus
from pullbox.services.direct_artifact_post_processing import (
    run_direct_artifact_pack_post_processing,
    run_direct_artifact_post_processing,
)


@pytest.mark.asyncio
async def test_direct_handoff_reuses_pipeline_without_client_mapping_or_cleanup(
    tmp_path: Path,
) -> None:
    source = tmp_path / "quarantine" / "artifact-2.cbz"
    source.parent.mkdir()
    source.write_bytes(b"fixture")
    session = AsyncMock()
    library_file = SimpleNamespace(id=77, file_path="/library/Series/Issue 1.cbz")
    query_result = SimpleNamespace(scalar_one_or_none=lambda: library_file)
    session.execute.return_value = query_result
    observed: dict[str, Any] = {}

    async def fake_post_processor(
        session: Any,
        download: Any,
        *,
        resolve_local_path: Any,
        cleanup_source: bool,
        allow_resource_safety_exception: bool,
    ) -> None:
        observed["session"] = session
        observed["download"] = download
        observed["cleanup_source"] = cleanup_source
        observed["allow_resource_safety_exception"] = allow_resource_safety_exception
        observed["resolved_path"] = await resolve_local_path(session, download)
        download.final_path = library_file.file_path

    result = await run_direct_artifact_post_processing(
        session,
        acquisition_id=12,
        download_history_id=56,
        issue_id=34,
        source_path=source,
        replace_existing_file=True,
        allow_resource_safety_exception=True,
        post_processor=fake_post_processor,
    )

    assert observed["session"] is session
    assert observed["cleanup_source"] is False
    assert observed["allow_resource_safety_exception"] is True
    assert observed["resolved_path"] == str(source)
    assert observed["download"].id == 56
    assert observed["download"].download_client.value == "direct"
    assert observed["download"].replace_existing_file is True
    assert result.library_file_id == 77
    assert result.final_path == Path(library_file.file_path)


@pytest.mark.asyncio
async def test_direct_handoff_fails_when_pipeline_does_not_register_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "artifact.cbz"
    source.write_bytes(b"fixture")
    session = AsyncMock()
    query_result = SimpleNamespace(scalar_one_or_none=lambda: None)
    session.execute.return_value = query_result

    async def no_op_post_processor(*_args: Any, **_kwargs: Any) -> None:
        return None

    with pytest.raises(RuntimeError, match="did not register"):
        await run_direct_artifact_post_processing(
            session,
            acquisition_id=1,
            download_history_id=3,
            issue_id=2,
            source_path=source,
            replace_existing_file=False,
            post_processor=no_op_post_processor,
        )


@pytest.mark.asyncio
async def test_direct_handoff_materializes_library_symlink_before_quarantine_cleanup(
    tmp_path: Path,
) -> None:
    source = tmp_path / "quarantine" / "artifact-2.cbz"
    source.parent.mkdir()
    source.write_bytes(b"direct artifact")
    library_path = tmp_path / "library" / "Issue 1.cbz"
    library_path.parent.mkdir()
    session = AsyncMock()
    library_file = SimpleNamespace(id=77, file_path=str(library_path))
    session.execute.return_value = SimpleNamespace(
        scalar_one_or_none=lambda: library_file,
    )

    async def symlink_post_processor(
        _session: Any,
        download: Any,
        **_kwargs: Any,
    ) -> None:
        library_path.symlink_to(source)
        download.final_path = str(library_path)

    result = await run_direct_artifact_post_processing(
        session,
        acquisition_id=12,
        download_history_id=56,
        issue_id=34,
        source_path=source,
        replace_existing_file=False,
        post_processor=symlink_post_processor,
    )

    assert result.final_path == library_path
    assert library_path.is_symlink() is False
    assert library_path.read_bytes() == b"direct artifact"
    assert source.exists()


@pytest.mark.asyncio
async def test_direct_pack_always_imports_the_explicitly_selected_skipped_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_issue = SimpleNamespace(
        id=7,
        issue_number=5.0,
        status=IssueStatus.SKIPPED,
        series_id=3,
        series=SimpleNamespace(title="Alien", alternate_names=[]),
    )
    wanted_issue = SimpleNamespace(
        id=8,
        issue_number=6.0,
        status=IssueStatus.WANTED,
        series_id=3,
        series=target_issue.series,
    )
    initiating_result = SimpleNamespace(
        unique=lambda: SimpleNamespace(scalar_one_or_none=lambda: target_issue)
    )
    issues_result = SimpleNamespace(
        unique=lambda: SimpleNamespace(scalars=lambda: [target_issue, wanted_issue])
    )
    session = AsyncMock()
    session.execute.side_effect = [initiating_result, issues_result]
    extracted_path = tmp_path / "issue-5.cbz"
    extracted_path.write_bytes(b"issue")
    prepared_issue_ids: list[int] = []

    monkeypatch.setattr(
        post_processing.asyncio,
        "to_thread",
        AsyncMock(return_value={5.0: extracted_path}),
    )

    async def fake_prepare(_session: Any, *, issue_id: int, **_kwargs: Any) -> SimpleNamespace:
        prepared_issue_ids.append(issue_id)
        return SimpleNamespace(issue_id=issue_id, source_path=extracted_path)

    async def fake_execute(
        _session: Any,
        prepared: SimpleNamespace,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            issue_id=prepared.issue_id,
            library_file=SimpleNamespace(id=prepared.issue_id, file_path=str(extracted_path)),
        )

    monkeypatch.setattr(post_processing, "prepare_manual_issue_import", fake_prepare)
    monkeypatch.setattr(post_processing, "execute_manual_issue_import", fake_execute)
    validator = AsyncMock()
    monkeypatch.setattr(post_processing, "validate_direct_artifact", validator)

    result = await run_direct_artifact_pack_post_processing(
        session,
        acquisition_id=1,
        download_history_id=2,
        issue_id=target_issue.id,
        source_path=tmp_path / "pack.cbz",
        expected_issue_numbers=frozenset({"5"}),
        replace_existing_file=False,
    )

    assert prepared_issue_ids == [target_issue.id]
    assert result.imported_issue_ids == (target_issue.id,)
    validator.assert_awaited_once_with(session, extracted_path)


@pytest.mark.asyncio
async def test_direct_pack_only_replaces_the_explicitly_selected_existing_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_issue = SimpleNamespace(
        id=7,
        issue_number=5.0,
        status=IssueStatus.SKIPPED,
        series_id=3,
        library_file=object(),
        series=SimpleNamespace(title="Alien", alternate_names=[]),
    )
    supplemental_issue = SimpleNamespace(
        id=8,
        issue_number=6.0,
        status=IssueStatus.WANTED,
        series_id=3,
        library_file=object(),
        series=target_issue.series,
    )
    session = AsyncMock()
    session.execute.side_effect = [
        SimpleNamespace(unique=lambda: SimpleNamespace(scalar_one_or_none=lambda: target_issue)),
        SimpleNamespace(
            unique=lambda: SimpleNamespace(scalars=lambda: [target_issue, supplemental_issue])
        ),
    ]
    target_path = tmp_path / "issue-5.cbz"
    supplemental_path = tmp_path / "issue-6.cbz"
    target_path.write_bytes(b"target")
    supplemental_path.write_bytes(b"supplemental")
    prepared_issue_ids: list[int] = []

    monkeypatch.setattr(
        post_processing.asyncio,
        "to_thread",
        AsyncMock(return_value={5.0: target_path, 6.0: supplemental_path}),
    )

    async def fake_prepare(_session: Any, *, issue_id: int, **_kwargs: Any) -> SimpleNamespace:
        prepared_issue_ids.append(issue_id)
        return SimpleNamespace(issue_id=issue_id, source_path=target_path)

    async def fake_execute(
        _session: Any,
        prepared: SimpleNamespace,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            issue_id=prepared.issue_id,
            library_file=SimpleNamespace(id=prepared.issue_id, file_path=str(target_path)),
        )

    monkeypatch.setattr(post_processing, "prepare_manual_issue_import", fake_prepare)
    monkeypatch.setattr(post_processing, "execute_manual_issue_import", fake_execute)
    monkeypatch.setattr(post_processing, "validate_direct_artifact", AsyncMock())

    await run_direct_artifact_pack_post_processing(
        session,
        acquisition_id=1,
        download_history_id=2,
        issue_id=target_issue.id,
        source_path=tmp_path / "pack.cbz",
        expected_issue_numbers=frozenset({"5", "6"}),
        replace_existing_file=True,
    )

    assert prepared_issue_ids == [target_issue.id]


@pytest.mark.asyncio
async def test_direct_pack_materializes_library_symlinks_before_workspace_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_issue = SimpleNamespace(
        id=7,
        issue_number=5.0,
        status=IssueStatus.WANTED,
        series_id=3,
        library_file=None,
        series=SimpleNamespace(title="Alien", alternate_names=[]),
    )
    session = AsyncMock()
    session.execute.side_effect = [
        SimpleNamespace(unique=lambda: SimpleNamespace(scalar_one_or_none=lambda: target_issue)),
        SimpleNamespace(unique=lambda: SimpleNamespace(scalars=lambda: [target_issue])),
    ]
    source_path = tmp_path / "issue-5.cbz"
    source_path.write_bytes(b"issue")
    library_path = tmp_path / "library" / "issue-5.cbz"
    library_path.parent.mkdir()

    monkeypatch.setattr(
        post_processing,
        "extract_same_series_issue_files",
        lambda *_args, **_kwargs: {5.0: source_path},
    )

    async def run_inline(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(post_processing.asyncio, "to_thread", run_inline)

    async def fake_prepare(_session: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(issue_id=target_issue.id, source_path=source_path)

    async def fake_execute(
        _session: Any,
        prepared: SimpleNamespace,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        library_path.symlink_to(prepared.source_path)
        return SimpleNamespace(
            issue_id=prepared.issue_id,
            library_file=SimpleNamespace(id=1, file_path=str(library_path)),
        )

    monkeypatch.setattr(post_processing, "prepare_manual_issue_import", fake_prepare)
    monkeypatch.setattr(post_processing, "execute_manual_issue_import", fake_execute)
    monkeypatch.setattr(post_processing, "validate_direct_artifact", AsyncMock())

    await run_direct_artifact_pack_post_processing(
        session,
        acquisition_id=1,
        download_history_id=2,
        issue_id=target_issue.id,
        source_path=tmp_path / "pack.cbz",
        expected_issue_numbers=frozenset({"5"}),
        replace_existing_file=False,
    )

    assert library_path.is_symlink() is False
    assert library_path.read_bytes() == b"issue"
