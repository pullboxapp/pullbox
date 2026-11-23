"""Restore one reviewed issue from its retained artifact, not another transfer."""

from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from sqlalchemy import func, select

from pullbox.models import Issue, LibraryFile
from pullbox.models.direct_acquisition import DirectAcquisitionAttempt
from pullbox.models.pending_match import PendingMatch
from pullbox.schemas.issue_metadata_editor import IssueMetadataEditorReview
from pullbox.services.direct_metadata_recovery import (
    prepare_direct_metadata_recovery,
    run_direct_metadata_recovery,
)
from pullbox.services.issue_file_metadata import prepare_file_metadata
from tests.integration.metadata_identity.test_direct_metadata_recovery import (
    _review_case,
)
from tests.integration.metadata_identity.test_direct_metadata_recovery import (
    no_global_sync as no_global_sync,
)
from tests.integration.metadata_identity.test_direct_metadata_recovery import (
    paired_download_setting as paired_download_setting,
)
from tests.integration.metadata_identity.test_direct_metadata_recovery import (
    paired_restore_enabled as paired_restore_enabled,
)
from tests.unit.test_pdf_metadata_writing import native_pdf, pdf_source

pytestmark = pytest.mark.usefixtures(
    "no_global_sync", "paired_download_setting", "paired_restore_enabled"
)


@pytest.mark.parametrize(
    "scenario,suffix",
    [
        ("success", ".cbz"),
        ("success", ".cb7"),
        ("success", ".cbr"),
        ("occupied", ".cbz"),
        ("changed-source", ".cbz"),
        ("stale-edits", ".cbz"),
    ],
)
async def test_restore_reviewed_issue_fields(identity_probe_db, tmp_path, scenario, suffix):
    _, factory, _ = identity_probe_db
    pending, attempt, issue, source = await _review_case(
        factory, tmp_path, remove_copy=True, suffix=suffix
    )
    original = source.read_bytes()
    choices = {"series.issue_count": "library"}
    editor = IssueMetadataEditorReview.model_validate(
        {
            "edits": {
                "title": {"source": "custom", "value": "Recovered title"},
                "tags": {"source": "custom", "value": "Harbor\nNight"},
            }
        }
    )
    async with factory() as session:
        base = await prepare_direct_metadata_recovery(session, pending)
        assert base.preview.restores_copy and base.preview.editor_available
        before = await prepare_direct_metadata_recovery(session, pending, editor=editor)
        assert not before.preview.ready
        approved = await prepare_direct_metadata_recovery(
            session, pending, choices=choices, editor=editor
        )
        assert approved.preview.ready and approved.preview.editor_changed
        assert any(c.after == "Recovered title" for c in approved.preview.changes)
        assert await session.scalar(select(LibraryFile)) is None
        marker = (await session.get(DirectAcquisitionAttempt, attempt)).plan_snapshot[
            "paired_metadata_copy"
        ]
        path = Path(marker["copy_signature"]["resolved_path"]).with_suffix(".cbz")
    assert source.read_bytes() == original and not path.exists()
    operation = uuid4()

    async def run():
        return await run_direct_metadata_recovery(
            factory,
            pending,
            approved.preview.review_key,
            operation,
            choices=choices,
            editor=editor,
            limit=10**6,
            check_control=AsyncMock(),
            progress=AsyncMock(),
        )

    if scenario != "success":
        if scenario == "occupied":
            path.write_bytes(b"other copy")
        elif scenario == "stale-edits":
            editor = IssueMetadataEditorReview()
        else:
            with ZipFile(source, "a") as archive:
                archive.writestr("extra.jpg", b"changed")
        with pytest.raises(ValueError, match=r"changed|already exists|occupied"):
            await run()
        async with factory() as session:
            assert (await session.get(PendingMatch, pending)).status == "pending"
            assert await session.scalar(select(LibraryFile)) is None
            assert (await session.get(Issue, issue)).title != "Recovered title"
        if scenario == "occupied":
            assert path.read_bytes() == b"other copy"
            assert list(path.parent.glob("*.cbz")) == [path]
        else:
            assert not path.exists()
        return
    assert await run() == "restored"
    async with factory() as session:
        file = await session.scalar(select(LibraryFile))
        assert Path(file.file_path) == path
        assert (await session.get(PendingMatch, pending)).status == "approved"
        assert (await session.get(Issue, issue)).title == "Recovered title"
        assert await session.scalar(select(func.count()).select_from(DirectAcquisitionAttempt)) == 1
        plan = await prepare_file_metadata(session, issue, editor=IssueMetadataEditorReview())
        assert plan.preview.unchanged
        assert {f.key for f in plan.preview.editor_fields if f.overridden} >= {
            "issue.title",
            "issue.tags",
        }
    with ZipFile(path) as z:
        assert ET.fromstring(z.read("ComicInfo.xml")).findtext("Tags") == "Harbor, Night"
        assert [n.text for n in ET.fromstring(z.read("MetronInfo.xml")).findall("Tags/Tag")] == [
            "Harbor",
            "Night",
        ]
        assert z.read("page.jpg") == b"page bytes"
    published = path.stat()
    assert await run() == "already_completed" and path.stat().st_mtime_ns == published.st_mtime_ns
    assert source.read_bytes() == original


@native_pdf
async def test_restore_pdf_with_reviewed_issue_fields(identity_probe_db, tmp_path, monkeypatch):
    from pullbox.models.direct_acquisition import DirectAcquisitionState, DirectArtifactAttempt
    from pullbox.models.issue import IssueStatus
    from pullbox.services import direct_paired_metadata
    from pullbox.services.archive_metadata_binding import ArchiveMetadataBindingError
    from tests.integration.metadata_identity.test_direct_paired_metadata import (
        direct_artifact,
        execute,
    )

    _, factory, _ = identity_probe_db
    attempt, artifact, issue_id, initial = await direct_artifact(factory, tmp_path)
    source = pdf_source(initial.with_suffix(".pdf"))
    initial.unlink()
    original = source.read_bytes()
    async with factory.begin() as session:
        (await session.get(DirectArtifactAttempt, artifact)).quarantine_path = str(source)
    # A temporary provider binding problem leaves a genuine retained PDF and recovery entry.
    with monkeypatch.context() as patch:
        patch.setattr(
            direct_paired_metadata,
            "prepare_file_metadata",
            AsyncMock(side_effect=ArchiveMetadataBindingError("identity_requires_review")),
        )
        assert (
            await execute(factory, tmp_path, attempt, artifact)
        ).state is DirectAcquisitionState.INTERVENTION
    async with factory.begin() as session:
        pending_id = await session.scalar(select(PendingMatch.id))
        file = await session.scalar(select(LibraryFile))
        Path(file.file_path).unlink()
        await session.delete(file)
        (await session.get(Issue, issue_id)).status = IssueStatus.WANTED
    editor = IssueMetadataEditorReview.model_validate(
        {"edits": {"title": {"source": "custom", "value": "Reviewed PDF"}}}
    )
    async with factory() as session:
        prepared = await prepare_direct_metadata_recovery(session, pending_id, editor=editor)
        assert (
            prepared.preview.ready
            and prepared.preview.restores_copy
            and prepared.preview.converts_to_cbz
        )
    assert (
        await run_direct_metadata_recovery(
            factory,
            pending_id,
            prepared.preview.review_key,
            uuid4(),
            choices={},
            editor=editor,
            limit=10**7,
            check_control=AsyncMock(),
            progress=AsyncMock(),
        )
        == "restored"
    )
    async with factory() as session:
        assert (await session.get(Issue, issue_id)).title == "Reviewed PDF"
        output = Path(await session.scalar(select(LibraryFile.file_path)))
    with ZipFile(output) as comic:
        assert ET.fromstring(comic.read("ComicInfo.xml")).findtext("Title") == "Reviewed PDF"
        assert "MetronInfo.xml" in comic.namelist()
        assert len([name for name in comic.namelist() if name.endswith(".jpg")]) == 3
    assert source.read_bytes() == original
