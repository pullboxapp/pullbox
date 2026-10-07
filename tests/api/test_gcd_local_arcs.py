"""GCD Local uses the existing authenticated Story Arc review and command path."""

import hashlib
import sqlite3

from sqlalchemy import func, select

from pullbox.models import Issue, Series, StoryArc, StoryArcExternalIdentity
from pullbox.models.library import LibraryRoot
from pullbox.models.story_arc import IssueStoryArc
from tests.api.test_gcd_local import activate
from tests.api.test_metadata_sources_api import csrf
from tests.api.test_source_arc_catalog_api import BASE
from tests.api.test_source_arc_catalog_api import sec_db as _sec_db
from tests.ui.test_source_story_arc_ui import post, seed
from tests.unit.test_gcd_local_arcs import arc_dump

pytest_plugins = ["conftest_security"]
sec_db = _sec_db
PREVIEW = "/story-arcs/catalog/gcd_local/4"


async def test_gcd_local_arc_search_review_add_repeat_and_refresh(
    authenticated_client, sec_db, tmp_path, monkeypatch
):
    client = authenticated_client
    path = arc_dump(tmp_path / "gcd.db")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert (await activate(client, path)).status_code == 200
    root_path = tmp_path / "comics"
    root_path.mkdir()
    async with sec_db.begin() as session:
        root = LibraryRoot(name="Managed", path=str(root_path), allow_managed_writes=True)
        session.add(root)
        await session.flush()
        root_id = root.id
    searches = []
    monkeypatch.setattr(
        "pullbox.tasks.story_arc_search_task.schedule_story_arc_search", searches.append
    )
    result = await client.get("/story-arcs/add?q=Civil%20War&source=gcd_local")
    assert result.status_code == 200 and f'href="{PREVIEW}"' in result.text
    assert "Civil War [Marvel]" in result.text and "GCD local" in result.text
    reviewed = await client.get(PREVIEW)
    data = seed(reviewed.text)
    assert data["ready"] and data["sourceLabel"] == "GCD local"
    assert len(data["members"]) == 3
    assert "publication" in reviewed.text.lower()
    form = [
        ("source_revision", str(data["sourceRevision"])),
        ("fingerprint", data["fingerprint"]),
        ("file_defaults_fingerprint", data["fileDefaultsFingerprint"]),
        ("library_root_id", str(root_id)),
        ("issue_provider_ids", "11"),
        ("issue_provider_ids", "10"),
        ("issue_provider_ids", "15"),
        ("reading_orders", "1"),
        ("reading_orders", "2"),
        ("reading_orders", "3"),
        ("skipped_issue_provider_ids", "15"),
    ]
    added = await post(client, PREVIEW, form)
    assert added.status_code == 303 and "catalog-added" in added.headers["location"]
    arc_id = int(added.headers["location"].split("/")[-1].split("?")[0])
    again = await client.get(PREVIEW, follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"] == f"/story-arcs/{arc_id}"
    async with sec_db() as session:
        assert await session.scalar(select(func.count()).select_from(StoryArc)) == 1
        assert await session.scalar(select(func.count()).select_from(Series)) == 2
        assert await session.scalar(select(func.count()).select_from(Issue)) == 3
        arc = await session.get(StoryArc, arc_id)
        assert arc.comicvine_id is None
        identity = await session.scalar(select(StoryArcExternalIdentity))
        assert identity.source == "gcd" and identity.external_id == "4"
        members = list(
            await session.scalars(select(IssueStoryArc).order_by(IssueStoryArc.sequence_number))
        )
        assert [member.source_issue_id for member in members] == ["11", "10", "15"]
        assert members[-1].resolution_state.value == "skipped"
        assert all(member.resolution_method == "exact_gcd_id" for member in members[:-1])
        assert all(issue.comicvine_id is None for issue in await session.scalars(select(Issue)))
    refreshed = await client.get(f"/story-arcs/{arc_id}/catalog-refresh")
    assert refreshed.status_code == 200 and "Compare GCD local" in refreshed.text
    assert not searches and not list(root_path.iterdir())
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


async def test_changed_gcd_dump_cannot_commit_a_previously_reviewed_arc(
    authenticated_client, sec_db, tmp_path
):
    client = authenticated_client
    path = arc_dump(tmp_path / "gcd.db")
    assert (await activate(client, path)).status_code == 200
    selection = {"source": "gcd_local", "external_id": "4", "source_revision": 1}
    result = await client.post(BASE + "/preview", json=selection, headers=csrf(client))
    assert result.status_code == 200 and len(result.json()["issues"]) == 3
    snapshot = result.json()
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM gcd_story_story_arc WHERE story_id=205")
    decision = {
        **selection,
        "fingerprint": snapshot["fingerprint"],
        "file_defaults_fingerprint": snapshot["file_defaults_fingerprint"],
        "library_root_id": 1,
        "ordered_issue_ids": ["15", "10", "11"],
    }
    rejected = await client.post(BASE, json=decision, headers=csrf(client))
    assert rejected.status_code in {400, 409}, rejected.text
    async with sec_db() as session:
        for model in (StoryArc, Series, Issue):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


async def test_cached_gcd_arc_search_cannot_hide_a_changed_dump(authenticated_client, tmp_path):
    client = authenticated_client
    path = arc_dump(tmp_path / "gcd.db")
    assert (await activate(client, path)).status_code == 200
    url = "/story-arcs/add?q=Civil%20War&source=gcd_local"
    assert f'href="{PREVIEW}"' in (await client.get(url)).text
    with sqlite3.connect(path) as db:
        db.execute("UPDATE gcd_story_arc SET name='Replaced' WHERE id=4")
    response = await client.get(url)
    assert f'href="{PREVIEW}"' not in response.text
    assert "Story Arc search failed" in response.text
