"""Review color and manga flags explicitly; these are not a volume or a reader preference."""

import pytest

from pullbox.core.metadata_identity import IdentityNamespace, MetadataSource
from pullbox.schemas.metadata_sources import ProviderIssueRead
from pullbox.services.issue_metadata_editor import IssueMetadataEditError
from pullbox.services.metadata_issue_refresh import refresh_issue_from_sources
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared
from tests.integration.metadata_identity.test_comicinfo_text_editor import documents
from tests.integration.metadata_identity.test_issue_metadata_editor import edit
from tests.integration.metadata_identity.test_issue_refresh import IssueAdapter, reader
from tests.integration.metadata_identity.test_issue_tag_editor import preview, source
from tests.integration.metadata_identity.test_series_adoption import (
    configured_sources,  # noqa: F401
)


async def test_optional_comicinfo_flags_keep_separate_states(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        await source(
            factory,
            plan,
            path,
            "",
            "",
            ci_extra="<Manga>YesAndRightToLeft</Manga>",
            mi_extra="<MangaVolume>8</MangaVolume>",
        )
        original = path.read_bytes()
        pending = await preview(factory, issue)
        rows = {r.key: r for r in pending.editor_fields}
        assert {"issue.black_and_white", "issue.manga"} <= rows.keys()
        assert set(rows["issue.black_and_white"].allowed_values) == {"Yes", "No", "Unknown"}
        assert set(rows["issue.manga"].allowed_values) == {
            "Yes",
            "No",
            "Unknown",
            "YesAndRightToLeft",
        }
        assert not rows["issue.black_and_white"].differs and rows["issue.manga"].differs
        assert {o.source for o in rows["issue.manga"].options} == {"library", "ComicInfo.xml"}
        changes = {
            "black_and_white": {"source": "custom", "value": "No"},
            "manga": {"source": "ComicInfo.xml"},
        }
        assert (await preview(factory, issue, changes)).ready and path.read_bytes() == original
        await edit(factory, issue, changes)
        ci, mi = documents(path)
        assert (ci.findtext("BlackAndWhite"), ci.findtext("Manga")) == ("No", "YesAndRightToLeft")
        assert (
            mi.findtext("MangaVolume") == "8"
            and mi.find("Manga") is mi.find("BlackAndWhite") is None
        )
        before_refresh = path.read_bytes(), path.stat().st_mtime_ns
        adapter = IssueAdapter(
            ProviderIssueRead(
                source=MetadataSource.METRON_API,
                identity_namespace=IdentityNamespace.METRON,
                external_id="42",
                series_external_id="12",
                issue_number_text="50-X",
            )
        )
        async with factory() as session:
            await refresh_issue_from_sources(
                session, issue, gcd_api_enabled=False, registry=reader(adapter)
            )
            await session.commit()
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before_refresh
        saved = await preview(factory, issue)
        assert saved.ready and all(
            f.overridden and not f.differs
            for f in saved.editor_fields
            if f.key in {"issue.black_and_white", "issue.manga"}
        )
        await edit(factory, issue, {"black_and_white": {"source": "custom", "value": "Unknown"}})
        assert documents(path)[0].findtext("BlackAndWhite") == "Unknown"
        await edit(factory, issue, {"black_and_white": {"source": "clear"}})
        assert documents(path)[0].find("BlackAndWhite") is None
        assert documents(path)[0].findtext("Manga") == "YesAndRightToLeft"
        sig = path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns
        assert await edit(factory, issue, {"manga": {"source": "reset"}}) == "override_released"
        assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == sig


async def test_invalid_legacy_flag_is_not_adopted_or_silently_replaced(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        await source(factory, plan, path, "", "", ci_extra="<Manga>RTL</Manga><BlackAndWhite/>")
        report = await preview(factory, issue)
        rows = {r.key: r for r in report.editor_fields}
        assert report.ready and {"issue.manga", "issue.black_and_white"} <= rows.keys()
        assert any(o.value == "RTL" and not o.selectable for o in rows["issue.manga"].options)
        assert any(
            o.value == "Unknown" and o.selectable for o in rows["issue.black_and_white"].options
        )
        for field, action in (
            ("manga", {"source": "ComicInfo.xml"}),
            ("manga", {"source": "custom", "value": "RTL"}),
            ("black_and_white", {"source": "custom", "value": "YesAndRightToLeft"}),
            ("manga", {"source": "MetronInfo.xml"}),
        ):
            with pytest.raises(IssueMetadataEditError):
                await preview(factory, issue, {field: action})
        await edit(factory, issue, {"black_and_white": {"source": "ComicInfo.xml"}})
        ci, _ = documents(path)
        assert ci.findtext("Manga") == "RTL" and ci.findtext("BlackAndWhite") == "Unknown"
        await edit(factory, issue, {"manga": {"source": "custom", "value": "Yes"}})
        assert documents(path)[0].findtext("Manga") == "Yes"
        await source(factory, plan, path, "", "", ci_extra="<BlackAndWhite>Unknown</BlackAndWhite>")
        changed = await preview(factory, issue)
        assert not changed.ready and any(c.key == "xml.Manga" for c in changed.conflicts)
        await edit(factory, issue, {"manga": {"source": "library"}})
        assert (
            documents(path)[0].findtext("Manga") == "Yes" and (await preview(factory, issue)).ready
        )


async def test_legacy_identifier_in_flag_keeps_protection(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        await source(factory, plan, path, "", "", ci_extra="<Manga>[CVDB123]</Manga>")
        report = await preview(factory, issue)
        row = next(r for r in report.editor_fields if r.key == "issue.manga")
        assert report.ready and not row.editable and "identifier" in row.reason
        with pytest.raises(IssueMetadataEditError, match="identifier"):
            await preview(factory, issue, {"manga": {"source": "clear"}})
        await edit(factory, issue, {"black_and_white": {"source": "custom", "value": "Yes"}})
        assert documents(path)[0].findtext("Manga") == "[CVDB123]"
