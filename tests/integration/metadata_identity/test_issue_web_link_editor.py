"""Reviewed reference links are not a way to reassign the comic's provider identity."""

from zipfile import ZipFile

import pytest
from pydantic import ValidationError

from pullbox.services.archive_metadata_rendering import ArchiveMetadataRenderError
from pullbox.services.issue_metadata_editor import IssueMetadataEditError
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared
from tests.integration.metadata_identity.test_comicinfo_text_editor import documents
from tests.integration.metadata_identity.test_issue_metadata_editor import edit
from tests.integration.metadata_identity.test_issue_tag_editor import preview, source

A = "https://publisher.example/edition"
B = "https://author.example/interview"
C = "https://publisher.example/bonus%20art"
MI_ID = "https://metron.cloud/issue/42/"


async def test_edit_cannot_turn_separate_urls_into_legacy_import_evidence(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        web = f"https://comicvine.gamespot.com/archive/\nhttps://other.example/4000-989/ {A}"
        await source(factory, plan, path, "", "", ci_extra=f"<Web>{web}</Web>")
        before = path.read_bytes()
        changes = {"web_links": {"source": "custom", "value": [{"url": B}]}}
        assert (await preview(factory, issue, changes)).ready
        assert path.read_bytes() == before
        await edit(factory, issue, changes)
        assert documents(path)[0].findtext("Web") == web.replace(A, B)


async def test_edit_clear_and_recheck_web_links_preserves_provider_evidence(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        # A provider link belongs only to the document containing it.
        ci_id = "https://www.comics.org/issue/234/"
        await source(
            factory,
            plan,
            path,
            "",
            "",
            ci_extra=f"<Web>{ci_id} {A}</Web>",
            mi_extra=(
                f'<URLs><URL primary="false">{MI_ID}</URL>'
                f'<URL primary="1">{A}</URL><URL>{B}</URL></URLs>'
            ),
        )
        before = path.read_bytes()
        initial = await preview(factory, issue)
        fields = {r.key: r for r in initial.editor_fields}
        assert initial.ready and "issue.web_links" in fields
        row = fields["issue.web_links"]
        assert row.editable and row.input_type == "links"
        assert [(r.url, r.primary) for r in row.link_draft] == [(A, True), (B, False)]
        assert ci_id not in str(row.options) and MI_ID not in str(row.options)
        choices = {
            "web_links": {"source": "custom", "value": [{"url": C, "primary": True}, {"url": B}]}
        }
        assert (await preview(factory, issue, choices)).ready and path.read_bytes() == before
        await edit(factory, issue, choices)
        ci, mi = documents(path)
        assert ci.findtext("Web").split() == [ci_id, C, B]
        assert [(n.text, n.get("primary")) for n in mi.findall("URLs/URL")] == [
            (MI_ID, "false"),
            (C, "true"),
            (B, None),
        ]
        assert (
            mi.findtext("IDS/ID") == "42"
            and ci.findtext("Number") == mi.findtext("Number") == "50-X"
        )
        saved = next(
            r for r in (await preview(factory, issue)).editor_fields if r.key == "issue.web_links"
        )
        assert saved.overridden and not saved.differs
        await edit(factory, issue, {"web_links": {"source": "clear"}})
        ci, mi = documents(path)
        assert ci.findtext("Web") == ci_id and [
            (n.text, n.get("primary")) for n in mi.findall("URLs/URL")
        ] == [(MI_ID, "false")]
        with ZipFile(path) as z:
            assert z.read("page.jpg") == b"page bytes"
        await source(factory, plan, path, "", "", ci_extra=f"<Web>{A}</Web>")
        drift = await preview(factory, issue)
        assert not drift.ready and any(c.key == "xml.Web" for c in drift.conflicts)


async def test_primary_identity_stays_primary_and_invalid_choices_do_not_write(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        await source(
            factory,
            plan,
            path,
            "",
            "",
            ci_extra=f"<Web>{B}</Web>",
            mi_extra=f'<URLs><URL primary="1">{MI_ID}</URL><URL>{A}</URL></URLs>',
        )
        before = path.read_bytes()
        row = next(
            r for r in (await preview(factory, issue)).editor_fields if r.key == "issue.web_links"
        )
        assert row.primary_link_locked and row.editable
        with pytest.raises(IssueMetadataEditError, match="primary"):
            await preview(
                factory,
                issue,
                {"web_links": {"source": "custom", "value": [{"url": B, "primary": True}]}},
            )
        for bad in (
            MI_ID,
            "https://elsewhere.example/4000-99/",
            "https://u:p@example.com/link",
            "not a URL",
        ):
            with pytest.raises(ValidationError):
                await preview(
                    factory, issue, {"web_links": {"source": "custom", "value": [{"url": bad}]}}
                )
        assert path.read_bytes() == before
        await edit(factory, issue, {"web_links": {"source": "ComicInfo.xml"}})
        assert [(n.text, n.get("primary")) for n in documents(path)[1].findall("URLs/URL")] == [
            (MI_ID, "1"),
            (B, None),
        ]


async def test_existing_private_link_does_not_leak_into_review_or_block_other_fields(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        private = A + "?access_token=private-fixture"
        await source(
            factory,
            plan,
            path,
            "",
            "",
            ci_extra=f"<Web>{private}</Web>",
            mi_extra=f"<URLs><URL>{A}</URL></URLs>",
        )
        result = await preview(factory, issue)
        row = next(r for r in result.editor_fields if r.key == "issue.web_links")
        assert result.ready and not row.editable and "private-fixture" not in row.model_dump_json()
        with pytest.raises(IssueMetadataEditError):
            await preview(factory, issue, {"web_links": {"source": "clear"}})
        await edit(factory, issue, {"review": {"source": "custom", "value": "Worth reading"}})
        assert documents(path)[0].findtext("Web") == private


async def test_clear_cannot_remove_an_unverified_comicvine_issue_url(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        provider_url = "https://comicvine.gamespot.com/book/4000-7/"
        await source(factory, plan, path, "", "", ci_extra=f"<Web>{provider_url} {B}</Web>")
        before = path.read_bytes()
        with pytest.raises(ArchiveMetadataRenderError, match="unverified_identity"):
            await preview(factory, issue, {"web_links": {"source": "clear"}})
        assert path.read_bytes() == before


async def test_external_private_link_can_release_a_saved_override_without_rewriting(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        await source(factory, plan, path, "", "", ci_extra=f"<Web>{B}</Web>")
        await edit(factory, issue, {"web_links": {"source": "ComicInfo.xml"}})
        await source(
            factory, plan, path, "", "", ci_extra=f"<Web>{A}?access_token=private-fixture</Web>"
        )
        sig = path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns
        result = await preview(factory, issue)
        row = next(r for r in result.editor_fields if r.key == "issue.web_links")
        assert not result.ready and row.editable and row.reset_only
        with pytest.raises(IssueMetadataEditError):
            await preview(factory, issue, {"web_links": {"source": "library"}})
        await edit(factory, issue, {"web_links": {"source": "reset"}})
        assert sig == (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
