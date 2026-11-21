"""An identity-only Mylar recheck grants one saved path, never a source directory."""

import zipfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.models.import_job import ImportedFileStatus, ImportFileHandlingMode, ImportSourceType
from pullbox.services.import_review_source_actions import process_source_action, queue_source_action
from tests.unit.test_import_review_source_actions import make_source


async def saved_source(session, tmp_path):
    job, series, file = await make_source(session, root=str(tmp_path))
    job.source_type = ImportSourceType.MYLAR3
    job.source_path = str(tmp_path / "mylar.db")
    job.file_handling_mode = ImportFileHandlingMode.MANAGED_COPY
    job.mylar3_path_map_confirmed = True
    job.mylar3_path_map = {}
    path = Path(file.file_path)
    archive(path)
    file.source_signature = build_file_identity_signature(path)
    return job, series, file, path


def archive(path):
    with zipfile.ZipFile(path, "w") as output:
        output.writestr("01.jpg", b"first")
        output.writestr("02.jpg", b"second")


@pytest.mark.parametrize("replace", [False, True])
async def test_exact_mylar_recheck_accepts_only_the_saved_path(
    db_session, async_engine, tmp_path, replace
):
    job, series, file, path = await saved_source(db_session, tmp_path)
    if replace:
        replacement = tmp_path / "replacement.cbz"
        archive(replacement)
        replacement.replace(path)
        assert file.source_signature["inode"] != path.stat().st_ino
    before = path.read_bytes()
    await queue_source_action(db_session, job.id, file.id)
    await db_session.commit()

    result = await process_source_action(
        async_sessionmaker(async_engine, expire_on_commit=False), job.id, file.id
    )

    assert result == series.id
    await db_session.refresh(file)
    assert file.status is ImportedFileStatus.SAFETY_APPROVED
    assert file.source_signature == build_file_identity_signature(path)
    assert path.read_bytes() == before
    assert not file.diagnostics.get("safety_exception")


@pytest.mark.parametrize(
    "change",
    [
        "symlink",
        "different_path",
        "missing_proof",
        "unconfirmed",
        "in_place",
        "mapped",
        "sensitive",
        "redirect_during_stat",
    ],
)
async def test_exact_mylar_recheck_cannot_expand_saved_access(
    db_session, async_engine, tmp_path, monkeypatch, change
):
    from pullbox.services import import_review_recheck as recheck

    job, _, file, path = await saved_source(db_session, tmp_path)
    other = tmp_path / "other.cbz"
    archive(other)
    before = other.read_bytes()
    if change == "symlink":
        path.unlink()
        path.symlink_to(other)
    elif change == "different_path":
        file.file_path = str(other)
    elif change == "missing_proof":
        file.source_signature = {"schema_version": 1, "resolved_path": str(path.resolve())}
    elif change == "unconfirmed":
        job.mylar3_path_map_confirmed = False
    elif change == "in_place":
        job.file_handling_mode = ImportFileHandlingMode.IN_PLACE
    elif change == "mapped":
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        job.mylar3_path_map = {"/old": str(allowed)}
    elif change == "sensitive":
        monkeypatch.setattr("pullbox.core.filesystem_policy.is_sensitive_path", lambda _: True)
    else:
        other_signature = build_file_identity_signature(other)
        monkeypatch.setattr(recheck, "build_file_identity_signature", lambda _: other_signature)

    def forbidden(*args, **kwargs):
        pytest.fail("An unauthorized source must not be opened")

    monkeypatch.setattr(recheck, "run_safety_checks", forbidden)
    monkeypatch.setattr(recheck.SourceMetadataExtractor, "read_sidecars", forbidden)
    await queue_source_action(db_session, job.id, file.id)
    await db_session.commit()
    assert (
        await process_source_action(
            async_sessionmaker(async_engine, expire_on_commit=False), job.id, file.id
        )
        is None
    )
    await db_session.refresh(file)
    assert file.status is ImportedFileStatus.SAFETY_BLOCKED
    assert other.read_bytes() == before


async def test_revoked_mylar_confirmation_invalidates_a_queued_recheck(
    db_session, async_engine, tmp_path, monkeypatch
):
    from pullbox.services import import_review_source_actions as actions

    job, _, file, _ = await saved_source(db_session, tmp_path)
    await queue_source_action(db_session, job.id, file.id)
    job.mylar3_path_map_confirmed = False
    await db_session.commit()

    def forbidden(*args, **kwargs):
        pytest.fail("Revoked confirmation must invalidate the queued scope before inspection")

    monkeypatch.setattr(actions, "inspect_review_source", forbidden)
    assert (
        await process_source_action(
            async_sessionmaker(async_engine, expire_on_commit=False), job.id, file.id
        )
        is None
    )
    await db_session.refresh(file)
    assert file.diagnostics["review_source_action"]["state"] == "failed"


async def test_exact_mylar_recheck_does_not_approve_dangerous_replacement(
    db_session, async_engine, tmp_path
):
    job, _, file, path = await saved_source(db_session, tmp_path)
    with zipfile.ZipFile(path, "w") as output:
        output.writestr("../escape.jpg", b"unsafe")
    before = path.read_bytes()
    await queue_source_action(db_session, job.id, file.id)
    await db_session.commit()
    assert (
        await process_source_action(
            async_sessionmaker(async_engine, expire_on_commit=False), job.id, file.id
        )
        is None
    )
    await db_session.refresh(file)
    assert file.status is ImportedFileStatus.SAFETY_BLOCKED
    assert file.diagnostics["safety_block"]["category"] == "dangerous_path_or_payload"
    assert path.read_bytes() == before
