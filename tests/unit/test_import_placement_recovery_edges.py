"""Fail-closed edge coverage for durable import placement recovery."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pullbox.core.exceptions import ConfigurationError
from pullbox.services import import_placement_recovery as recovery


def _action(action_id: int, **payload: object) -> SimpleNamespace:
    return SimpleNamespace(id=action_id, payload=payload)


@pytest.mark.asyncio
async def test_direct_move_completion_requires_exact_durable_paths(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source.cbz"
    destination = tmp_path / "destination.cbz"
    destination.write_bytes(b"comic")
    actions = [
        _action(1, placement_completed=False),
        _action(2, placement_completed=True, transfer_method="copy"),
        _action(3, placement_completed=True, transfer_method="move"),
        _action(
            4,
            placement_completed=True,
            transfer_method="move",
            original_source_path=tmp_path / "other.cbz",
            artifact_source_path=source,
        ),
        _action(
            5,
            placement_completed=True,
            transfer_method="move",
            original_source_path=source,
            artifact_source_path=tmp_path / "other.cbz",
        ),
        _action(
            6,
            placement_completed=True,
            transfer_method="move",
            original_source_path=source,
            artifact_source_path=source,
            destination_path=tmp_path / "missing.cbz",
        ),
        _action(
            7,
            placement_completed=True,
            transfer_method="move",
            original_source_path=source,
            artifact_source_path=source,
            destination_path=destination,
        ),
    ]
    monkeypatch.setattr(recovery, "_candidate_actions", AsyncMock(return_value=actions))

    assert await recovery.has_completed_direct_move_placement_record(
        AsyncMock(), job_id=1, imported_file_id=2, source_path=source
    )


@pytest.mark.asyncio
async def test_recovery_rejects_each_untrusted_journal_shape(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source.cbz"
    destination = tmp_path / "destination.cbz"
    destination.write_bytes(b"comic")
    temp = tmp_path / "partial.tmp"
    temp.write_bytes(b"partial")
    signature = {
        "content_digest": "digest",
        "content_digest_algorithm": "sha256",
        "size_bytes": 5,
    }
    actions = [
        _action(1, placement_completed=False),
        _action(2, placement_completed=True, issue_id=9, transfer_method="copy"),
        _action(3, placement_completed=True, issue_id=7, transfer_method="move"),
        _action(4, placement_completed=True, issue_id=7, transfer_method="copy"),
        _action(
            5,
            placement_completed=True,
            issue_id=7,
            transfer_method="copy",
            original_source_path=tmp_path / "other.cbz",
            artifact_source_path=source,
            destination_path=destination,
        ),
        _action(
            6,
            placement_completed=True,
            issue_id=7,
            transfer_method="copy",
            original_source_path=source,
            artifact_source_path=source,
            destination_path=source,
        ),
        _action(
            7,
            placement_completed=True,
            issue_id=7,
            transfer_method="copy",
            original_source_path=source,
            artifact_source_path=source,
            destination_path=destination,
            temp_paths=[temp],
        ),
        _action(
            8,
            placement_completed=True,
            issue_id=7,
            transfer_method="copy",
            original_source_path=source,
            artifact_source_path=source,
            destination_path=tmp_path / "missing.cbz",
        ),
        _action(
            9,
            placement_completed=True,
            issue_id=7,
            transfer_method="copy",
            original_source_path=source,
            artifact_source_path=source,
            destination_path=destination,
            destination_signature="invalid",
        ),
        _action(
            10,
            placement_completed=True,
            issue_id=7,
            transfer_method="copy",
            original_source_path=source,
            artifact_source_path=source,
            destination_path=destination,
            destination_signature={"content_digest": "digest", "content_digest_algorithm": "md5"},
        ),
        _action(
            11,
            placement_completed=True,
            issue_id=7,
            transfer_method="copy",
            original_source_path=source,
            artifact_source_path=source,
            destination_path=destination,
            destination_signature=signature,
        ),
    ]
    monkeypatch.setattr(recovery, "_candidate_actions", AsyncMock(return_value=actions))
    monkeypatch.setattr(
        recovery,
        "build_managed_placement_signature",
        lambda _path: {**signature, "content_digest": "different"},
    )

    session = AsyncMock()
    assert (
        await recovery.load_completed_import_placement_recovery(
            session,
            job_id=1,
            imported_file_id=2,
            issue_id=7,
            source_path=source,
            transfer_method="copy",
        )
        is None
    )


@pytest.mark.asyncio
async def test_recovery_tolerates_signature_errors_and_returns_exact_evidence(
    monkeypatch, tmp_path
) -> None:
    source = tmp_path / "source.cbz"
    destination = tmp_path / "destination.cbz"
    destination.write_bytes(b"comic")
    signature = {
        "content_digest": "digest",
        "content_digest_algorithm": "sha256",
        "size_bytes": 5,
    }
    candidate = _action(
        12,
        placement_completed=True,
        issue_id=7,
        transfer_method="copy",
        original_source_path=source,
        artifact_source_path=source,
        destination_path=destination,
        destination_signature=signature,
    )
    monkeypatch.setattr(recovery, "_candidate_actions", AsyncMock(return_value=[candidate]))
    monkeypatch.setattr(
        recovery,
        "build_managed_placement_signature",
        lambda _path: (_ for _ in ()).throw(ConfigurationError("unreadable")),
    )
    assert (
        await recovery.load_completed_import_placement_recovery(
            AsyncMock(),
            job_id=1,
            imported_file_id=2,
            issue_id=7,
            source_path=source,
            transfer_method="copy",
        )
        is None
    )

    monkeypatch.setattr(recovery, "build_managed_placement_signature", lambda _path: signature)
    session = AsyncMock()
    session.scalar.return_value = None
    result = await recovery.load_completed_import_placement_recovery(
        session,
        job_id=1,
        imported_file_id=2,
        issue_id=7,
        source_path=source,
        transfer_method="copy",
    )
    assert result == recovery.CompletedImportPlacementRecovery(
        action_id=12,
        destination_path=destination,
        destination_signature=signature,
    )

    session.scalar.return_value = 99
    assert (
        await recovery.load_completed_import_placement_recovery(
            session,
            job_id=1,
            imported_file_id=2,
            issue_id=7,
            source_path=source,
            transfer_method="copy",
        )
        is None
    )


@pytest.mark.asyncio
async def test_direct_move_recovery_preserves_a_reappeared_source(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source.cbz"
    destination = tmp_path / "destination.cbz"
    source.write_bytes(b"source")
    destination.write_bytes(b"destination")
    candidate = _action(
        13,
        placement_completed=True,
        issue_id=7,
        transfer_method="move",
        original_source_path=source,
        artifact_source_path=source,
        destination_path=destination,
        destination_signature={
            "content_digest": "digest",
            "content_digest_algorithm": "sha256",
        },
    )
    monkeypatch.setattr(recovery, "_candidate_actions", AsyncMock(return_value=[candidate]))

    assert (
        await recovery.load_completed_import_placement_recovery(
            AsyncMock(),
            job_id=1,
            imported_file_id=2,
            issue_id=7,
            source_path=source,
            transfer_method="move",
        )
        is None
    )


def test_path_comparison_fails_closed_when_resolution_errors(monkeypatch, tmp_path) -> None:
    path = tmp_path / "issue.cbz"
    monkeypatch.setattr(
        type(path), "resolve", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError())
    )
    assert not recovery._same_unresolved_path(path, path)
