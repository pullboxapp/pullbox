"""Official dump-shaped arc membership through the shared source registry."""

import hashlib
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import replace

import pytest

from pullbox.core.metadata_identity import MetadataSource as Source
from pullbox.providers.metadata.gcd_local_database import open_readonly, rows, validate_candidate
from pullbox.schemas.metadata_sources import SourceStatus, StoryArcDiscoveryQuery
from pullbox.services.metadata_discovery import MetadataSourceRegistry
from tests.unit.test_gcd_local_credits import EXPECTED, credited_dump
from tests.unit.test_metadata_discovery import runtime


def arc_dump(path):
    credited_dump(path)
    with sqlite3.connect(path) as db:
        # The official dump links stories to arcs, not issue IDs or reading positions.
        db.executescript("""
            CREATE TABLE gcd_story_arc (
                id INTEGER PRIMARY KEY, name TEXT, sort_name TEXT, disambiguation TEXT,
                description TEXT, notes TEXT, language_id INTEGER, deleted INTEGER);
            CREATE INDEX arc_name ON gcd_story_arc(name);
            CREATE TABLE gcd_story_story_arc (
                id INTEGER PRIMARY KEY, story_id INTEGER, storyarc_id INTEGER,
                UNIQUE(story_id,storyarc_id));
            CREATE INDEX member_arc ON gcd_story_story_arc(storyarc_id);
            INSERT INTO gcd_story_arc VALUES
                (4,'Civil War','Civil War','Marvel','<b>Native description</b>','',1,0),
                (5,'Civil War','Civil War','Other publisher','','',1,0),
                (6,'Deleted arc','Deleted arc','','','',1,1);
            INSERT INTO gcd_story VALUES (206,15,19,0), (207,14,19,0);
            INSERT INTO gcd_story_story_arc VALUES
                (1,201,4), (2,202,4), (3,203,4), (4,205,4), (5,206,4), (6,207,4);
        """)
    return path


async def registry(path, **updates):
    return MetadataSourceRegistry(
        [
            replace(
                runtime(Source.GCD_LOCAL, revision=1, **updates),
                gcd_snapshot=await validate_candidate(str(path)),
            )
        ],
    )


async def test_gcd_arcs_are_native_distinct_and_readonly_with_no_curated_order_claim(tmp_path):
    path = arc_dump(tmp_path / "gcd.db")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    source = await registry(path)
    found = await source.discover_arcs(
        StoryArcDiscoveryQuery(query="Civil War", sources=[Source.GCD_LOCAL])
    )
    assert found.sources[0].status is SourceStatus.OK
    assert [row.title for row in found.results] == [
        "Civil War [Marvel]",
        "Civil War [Other publisher]",
    ]
    arc = await source.story_arc(Source.GCD_LOCAL, "0004")
    assert arc.status is SourceStatus.OK
    assert arc.data.issue_external_ids == ["15", "10", "11"]
    assert arc.data.declared_issue_count == 3 and arc.data.membership_complete
    assert "publication" in " ".join(arc.data.warnings).lower()
    assert arc.data.identity_namespace.value == "gcd" and not arc.data.cross_identities
    assert arc.data.image_url is None
    assert "&lt;b&gt;" in arc.data.description
    members = await source.story_arc_issues(Source.GCD_LOCAL, "4")
    assert members.status is SourceStatus.OK
    assert [row.external_id for row in members.data.results] == arc.data.issue_external_ids
    assert members.data.total == 3 and not members.data.order_is_reading_order
    assert (
        next(row for row in members.data.results if row.external_id == "10").model_dump(
            mode="json"
        )["credits"]
        == EXPECTED
    )
    assert (await source.story_arc(Source.GCD_LOCAL, "6")).status is SourceStatus.NOT_FOUND
    assert (await source.story_arc_issues(Source.GCD_LOCAL, "999")).status is SourceStatus.NOT_FOUND
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert [item.name for item in tmp_path.iterdir()] == ["gcd.db"]


async def test_gcd_arc_pages_are_stable_bounded_and_search_terms_are_bound(tmp_path):
    path = arc_dump(tmp_path / "gcd.db")
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO gcd_issue VALUES (?,2999,?,'',?,0,NULL,'','2001-01-01','',32)",
            [(1000 + n, str(100 + n), 100 + n) for n in range(201)],
        )
        db.executemany(
            "INSERT INTO gcd_story VALUES (?,?,19,0)", [(1000 + n, 1000 + n) for n in range(201)]
        )
        db.executemany(
            "INSERT INTO gcd_story_story_arc VALUES (?,?,4)",
            [(1000 + n, 1000 + n) for n in range(201)],
        )
        db.executemany(
            "INSERT INTO gcd_story_arc VALUES (?,?,?,'','','',1,0)",
            [(1000 + n, f"Event {n:03}", f"Event {n:03}") for n in range(101)],
        )
    source = await registry(path)
    pages = [(await source.story_arc_issues(Source.GCD_LOCAL, "4", page=n)).data for n in (1, 2, 3)]
    assert [len(page.results) for page in pages] == [100, 100, 4]
    assert [page.next_page for page in pages] == [2, 3, None]
    ids = [row.external_id for page in pages for row in page.results]
    assert len(set(ids)) == 204
    assert (await source.story_arc(Source.GCD_LOCAL, "4")).data.issue_external_ids == ids
    found = await source.discover_arcs(
        StoryArcDiscoveryQuery(query="Event", sources=[Source.GCD_LOCAL])
    )
    assert len(found.results) == 100 and found.sources[0].next_page == 2
    next_page = await source.discover_arcs(
        StoryArcDiscoveryQuery(
            query="Event", sources=[Source.GCD_LOCAL], pages={Source.GCD_LOCAL: 2}
        )
    )
    assert len(next_page.results) == 1 and next_page.sources[0].next_page is None
    assert not (
        await source.discover_arcs(
            StoryArcDiscoveryQuery(query="' OR 1=1 --", sources=[Source.GCD_LOCAL])
        )
    ).results


@pytest.mark.parametrize("problem", ["variant", "missing_story", "missing_issue", "missing_parent"])
async def test_gcd_arc_never_substitutes_or_hides_an_ambiguous_member(tmp_path, problem):
    path = arc_dump(tmp_path / "gcd.db")
    with sqlite3.connect(path) as db:
        sql = {
            "variant": "INSERT INTO gcd_story_story_arc VALUES (7,204,4)",
            "missing_story": "INSERT INTO gcd_story_story_arc VALUES (7,999,4)",
            "missing_issue": "UPDATE gcd_story SET issue_id=999 WHERE id=205",
            "missing_parent": "UPDATE gcd_issue SET series_id=999 WHERE id=11",
        }[problem]
        db.execute(sql)
    source = await registry(path)
    for result in (
        await source.story_arc(Source.GCD_LOCAL, "4"),
        await source.story_arc_issues(Source.GCD_LOCAL, "4"),
    ):
        assert result.status is SourceStatus.INCOMPATIBLE_RESPONSE and result.data is None


@pytest.mark.parametrize("problem", ["absent", "view", "column", "changed", "disabled", "overflow"])
async def test_gcd_arc_absence_or_invalid_data_does_not_become_an_empty_catalog(tmp_path, problem):
    path = arc_dump(tmp_path / "gcd.db")
    with sqlite3.connect(path) as db:
        if problem == "absent":
            db.execute("DROP TABLE gcd_story_arc")
        elif problem == "view":
            db.executescript(
                "ALTER TABLE gcd_story_story_arc RENAME TO hidden; "
                "CREATE VIEW gcd_story_story_arc AS SELECT * FROM hidden;"
            )
        elif problem == "column":
            db.execute("ALTER TABLE gcd_story_story_arc RENAME COLUMN storyarc_id TO obsolete")
        elif problem == "overflow":
            db.executemany(
                "INSERT INTO gcd_issue VALUES (?,2999,?,'',?,0,NULL,'','','',32)",
                [(1000 + n, str(100 + n), 100 + n) for n in range(5001)],
            )
            db.executemany(
                "INSERT INTO gcd_story VALUES (?,?,19,0)",
                [(1000 + n, 1000 + n) for n in range(5001)],
            )
            db.executemany(
                "INSERT INTO gcd_story_story_arc VALUES (?,?,4)",
                [(1000 + n, 1000 + n) for n in range(5001)],
            )
    source = await registry(path, enabled=problem != "disabled")
    if problem == "changed":
        with sqlite3.connect(path) as db:
            db.execute("UPDATE gcd_story_arc SET name='Replaced' WHERE id=4")
    expected = {
        "absent": SourceStatus.UNSUPPORTED,
        "view": SourceStatus.INVALID_CONFIG,
        "column": SourceStatus.INVALID_CONFIG,
        "changed": SourceStatus.INVALID_CONFIG,
        "disabled": SourceStatus.DISABLED,
        "overflow": SourceStatus.INCOMPATIBLE_RESPONSE,
    }[problem]
    assert (await source.story_arc(Source.GCD_LOCAL, "4")).status is expected
    if problem == "absent":
        assert (await source.series(Source.GCD_LOCAL, "2999")).status is SourceStatus.OK


async def test_arc_membership_uses_existing_arc_index_and_fixed_query_count(tmp_path, monkeypatch):
    from sqlalchemy.dialects.sqlite import dialect

    from pullbox.providers.metadata import gcd_local as adapter_module
    from pullbox.providers.metadata.gcd_local_arcs import members_query

    path = arc_dump(tmp_path / "gcd.db")
    with sqlite3.connect(path) as db:
        db.execute("CREATE INDEX deleted_story ON gcd_story(deleted)")
        db.execute("CREATE INDEX deleted_issue ON gcd_issue(deleted)")
        compiled = members_query("4").compile(dialect=dialect(paramstyle="named"))
        plan = [
            row[3] for row in db.execute("EXPLAIN QUERY PLAN " + str(compiled), compiled.params)
        ]
        assert any("member_arc" in detail for detail in plan)
        assert not any("SCAN gcd_story" in detail or "SCAN gcd_issue" in detail for detail in plan)
        assert not any("deleted_story" in detail or "deleted_issue" in detail for detail in plan)
        db.executemany(
            "INSERT INTO gcd_issue VALUES (?,2999,?,'',?,0,NULL,'','','',32)",
            [(1000 + n, str(100 + n), 100 + n) for n in range(100)],
        )
        db.executemany(
            "INSERT INTO gcd_story VALUES (?,?,19,0)", [(1000 + n, 1000 + n) for n in range(100)]
        )
        db.executemany(
            "INSERT INTO gcd_story_story_arc VALUES (?,?,4)",
            [(1000 + n, 1000 + n) for n in range(100)],
        )
    statements = []

    @contextmanager
    def traced(*args):
        with open_readonly(*args) as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(adapter_module, "open_readonly", traced)
    source = await registry(path)
    assert (await source.story_arc_issues(Source.GCD_LOCAL, "4")).status is SourceStatus.OK
    first_count = sum(statement.startswith("SELECT") for statement in statements)
    statements.clear()
    assert (await source.story_arc_issues(Source.GCD_LOCAL, "4", page=2)).status is SourceStatus.OK
    assert sum(statement.startswith("SELECT") for statement in statements) == first_count
    assert (
        first_count <= 22
    )  # Includes optional schema inspection, not SQLite's PRAGMA trace comments.


async def test_arc_descriptive_text_is_bounded_before_materialization(tmp_path):
    from pullbox.providers.metadata.gcd_local_arcs import arc_query

    path = arc_dump(tmp_path / "gcd.db")
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE gcd_story_arc SET description=?,notes=? WHERE id=4",
            ("x" * 1000000, "y" * 1000000),
        )
    with open_readonly(path, threading.Event(), time.monotonic() + 1) as db:
        found = rows(db, arc_query())
    assert max(len(row["description"]) for row in found) == 20000
    assert max(len(row["notes"]) for row in found) == 20000
