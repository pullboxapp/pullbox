"""Explicit pack approvals preserve owned files and skipped sibling issues."""

import io
from datetime import UTC, datetime
from zipfile import ZipFile

import pytest
from sqlalchemy import select

from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models import Issue, LibraryFile, LibraryRoot, Series
from pullbox.models.direct_acquisition import DirectAcquisitionAttempt, DirectAcquisitionState
from pullbox.models.issue import IssueStatus
from pullbox.models.library import FileFormat
from pullbox.models.metadata_identity import IssueExternalIdentity
from tests.integration.metadata_identity.test_direct_pack_writer_boundary import (
    writer_flags,  # noqa: F401
)
from tests.integration.metadata_identity.test_direct_paired_metadata import direct_artifact, execute


@pytest.mark.parametrize("writer_flags", [(False, True)], indirect=True)
@pytest.mark.usefixtures("writer_flags")
@pytest.mark.parametrize(("count", "target"), [(8, 3), (8, 7), (4, 1), (4, 2), (4, 3), (4, 4)])
async def test_house_of_m_approval_imports_only_target_and_preserves_siblings(
    identity_probe_db, tmp_path, count, target
):
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, issue_id, source = await direct_artifact(factory, tmp_path)
    title = "House of M" if count == 8 else "House of M: Masters of Evil"
    numbers = [str(n) for n in range(1, count + 1)]
    owned = {}
    issue_ids = {}
    async with factory.begin() as session:
        target_issue = await session.get(Issue, issue_id)
        target_issue.issue_number = target
        target_issue.issue_number_text = str(target)
        target_issue.status = IssueStatus.SKIPPED
        series = await session.get(Series, target_issue.series_id)
        series.title = series.sort_title = title
        series.issue_count = count
        root_id = await session.scalar(select(LibraryRoot.id))
        for number in range(1, count + 1):
            if number == target:
                issue_ids[number] = issue_id
                continue
            is_owned = count == 8 and number not in (3, 7)
            sibling = Issue(
                series_id=series.id,
                issue_number=number,
                status=IssueStatus.OWNED if is_owned else IssueStatus.SKIPPED,
            )
            session.add(sibling)
            await session.flush()
            issue_ids[number] = sibling.id
            session.add(
                IssueExternalIdentity(
                    issue_id=sibling.id,
                    identity_namespace=IdentityNamespace.METRON,
                    external_id=str(1000 + number),
                    verification_state=IdentityVerificationState.VERIFIED,
                    evidence_kind="provider_result",
                )
            )
            if is_owned:
                path = tmp_path / "comics" / f"owned-{number}.cbz"
                path.write_bytes(f"Existing owned issue {number}".encode())
                owned[number] = (
                    path,
                    path.read_bytes(),
                    path.stat().st_ino,
                    path.stat().st_mtime_ns,
                )
                session.add(
                    LibraryFile(
                        issue_id=sibling.id,
                        library_root_id=root_id,
                        file_path=str(path),
                        file_name=path.name,
                        file_size=path.stat().st_size,
                        file_format=FileFormat.CBZ,
                        file_modified_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
                    )
                )
        attempt = await session.get(DirectAcquisitionAttempt, attempt_id)
        attempt.requested_coverage = {"issue_numbers": [str(target)], "issue_type": "issue"}
        attempt.plan_snapshot = {
            **attempt.plan_snapshot,
            "coverage": {"selected_content_issue_numbers": numbers},
        }
    with ZipFile(source, "w") as pack:
        for number in range(1, count + 1):
            payload = io.BytesIO()
            with ZipFile(payload, "w") as comic:
                comic.writestr("page.jpg", f"Page from issue {number}".encode())
                comic.writestr(
                    "ComicInfo.xml",
                    f"<ComicInfo><Series>{title}</Series><Number>{number}</Number></ComicInfo>",
                )
            pack.writestr(f"{title.replace(':', ' -')} #{number}.cbz", payload.getvalue())
    result = await execute(factory, tmp_path, attempt_id, artifact_id)
    assert result.state is DirectAcquisitionState.COMPLETED
    async with factory() as session:
        files = (await session.scalars(select(LibraryFile))).all()
        assert len(files) == len(owned) + 1
        imported = next(file for file in files if file.issue_id == issue_id)
        with ZipFile(imported.file_path) as comic:
            assert comic.read("page.jpg") == f"Page from issue {target}".encode()
            assert b"<Number>" + str(target).encode() + b"</Number>" in comic.read("MetronInfo.xml")
        for number, sibling_id in issue_ids.items():
            status = (await session.get(Issue, sibling_id)).status
            assert status is (
                IssueStatus.OWNED if number == target or number in owned else IssueStatus.SKIPPED
            )
        assert {file.issue_id for file in files} == {issue_ids[n] for n in {*owned, target}}
    for path, data, inode, mtime in owned.values():
        assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == (
            data,
            inode,
            mtime,
        )
