"""Structured credits use the real dump's name-detail foreign keys and roles."""

import hashlib
import sqlite3

import pytest

from pullbox.providers.metadata.gcd_local import GcdLocalSource
from pullbox.providers.metadata.gcd_local_database import validate_candidate
from pullbox.schemas.metadata_sources import SourceStatus
from pullbox.services.metadata_discovery import MetadataSourceError
from pullbox.utilities.comicinfo_creators import creator_roles_to_comicinfo_fields
from tests.api.test_gcd_local import gcd_dump


def credited_dump(path):
    gcd_dump(path)
    with sqlite3.connect(path) as db:
        # Columns and FKs observed in the official 2026-09-29 SQLite dump.
        db.executescript("""
            CREATE TABLE gcd_story (
                id INTEGER PRIMARY KEY, issue_id INTEGER, type_id INTEGER, deleted INTEGER);
            CREATE INDEX story_issue ON gcd_story(issue_id);
            CREATE TABLE gcd_creator_name_detail (
                id INTEGER PRIMARY KEY, creator_id INTEGER, name TEXT, deleted INTEGER);
            CREATE TABLE gcd_credit_type (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE gcd_story_credit (
                id INTEGER PRIMARY KEY, story_id INTEGER, creator_id INTEGER,
                credit_type_id INTEGER, credit_name TEXT, uncertain INTEGER, deleted INTEGER);
            CREATE INDEX story_credit_story ON gcd_story_credit(story_id);
            CREATE TABLE gcd_issue_credit (
                id INTEGER PRIMARY KEY, issue_id INTEGER, creator_id INTEGER,
                credit_type_id INTEGER, credit_name TEXT, uncertain INTEGER, deleted INTEGER);
            CREATE INDEX issue_credit_issue ON gcd_issue_credit(issue_id);
            INSERT INTO gcd_credit_type VALUES
                (1,'script'), (2,'pencils'), (3,'inks'), (4,'colors'), (5,'letters'),
                (6,'editing'), (7,'pencils and inks'), (9,'painting');
            INSERT INTO gcd_creator_name_detail VALUES
                (101,9001,'Alan Moore',0), (102,9002,'Dave Gibbons',0),
                (103,9003,'John Higgins',0), (104,9004,'Len Wein',0),
                (105,9005,'Richard Bruning',0), (106,9006,'Deleted creator',1);
            INSERT INTO gcd_story VALUES
                (201,10,6,0), (202,10,19,0), (203,10,19,1),
                (204,13,6,0), (205,11,19,0);
            INSERT INTO gcd_story_credit VALUES
                (1,201,102,2,'',0,0), (2,201,102,3,'',0,0),
                (3,201,103,4,'',0,0), (4,202,101,1,'',0,0),
                (5,202,102,7,'',0,0), (6,202,103,4,'',0,0),
                (7,202,102,5,'',0,0), (8,203,106,1,'',0,0),
                (9,202,106,1,'',0,1), (10,204,104,1,'',0,0),
                (11,205,104,5,'',0,0);
            INSERT INTO gcd_issue_credit VALUES
                (12,10,104,6,'editor',0,0), (13,10,105,6,'designer',0,0);
        """)
    return path


EXPECTED = [
    {"name": "Alan Moore", "role": "writer"},
    {"name": "Dave Gibbons", "role": "cover, inker, letterer, penciller"},
    {"name": "John Higgins", "role": "colorist, cover"},
    {"name": "Len Wein", "role": "editor"},
    {"name": "Richard Bruning", "role": "designer"},
]


@pytest.mark.parametrize("operation", ["issue", "issues"])
async def test_structured_story_and_issue_credits_survive_read_without_writes(tmp_path, operation):
    path = credited_dump(tmp_path / "gcd.db")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    adapter = GcdLocalSource(await validate_candidate(str(path)))
    result = await getattr(adapter, operation)("10" if operation == "issue" else "2999")
    issue = result.data if operation == "issue" else result.data.results[0]
    assert issue.model_dump(mode="json")["credits"] == EXPECTED
    assert issue.external_id == "10" and issue.series_external_id == "2999"
    assert (await adapter.issue("13")).data is None  # No variant-to-base credit borrowing.
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert sorted(p.name for p in tmp_path.iterdir()) == ["gcd.db"]


async def test_absent_optional_tables_or_records_are_unknown_not_authoritative_clears(tmp_path):
    adapter = GcdLocalSource(await validate_candidate(str(gcd_dump(tmp_path / "basic.db"))))
    assert (await adapter.issue("10")).data.credits is None
    path = credited_dump(tmp_path / "rich.db")
    adapter = GcdLocalSource(await validate_candidate(str(path)))
    assert (await adapter.issue("12")).data.credits is None
    assert (await adapter.issue("11")).data.credits[0].name == "Len Wein"


@pytest.mark.parametrize("problem", ["uncertain", "missing_name", "deleted_name", "missing_role"])
async def test_incomplete_credit_set_is_not_published_as_a_partial_authority(tmp_path, problem):
    path = credited_dump(tmp_path / "gcd.db")
    with sqlite3.connect(path) as db:
        sql = {
            "uncertain": "UPDATE gcd_story_credit SET uncertain=1 WHERE id=4",
            "missing_name": "DELETE FROM gcd_creator_name_detail WHERE id=101",
            "deleted_name": "UPDATE gcd_creator_name_detail SET deleted=1 WHERE id=101",
            "missing_role": "DELETE FROM gcd_credit_type WHERE id=1",
        }[problem]
        db.execute(sql)
    adapter = GcdLocalSource(await validate_candidate(str(path)))
    assert (await adapter.issue("10")).data.credits is None
    assert (await adapter.issue("11")).data.credits is not None


@pytest.mark.parametrize(
    "problem", ["view", "virtual", "column", "name", "role", "role_tail", "role_blob", "overflow"]
)
async def test_invalid_or_oversized_credit_profile_fails_safely(tmp_path, problem):
    path = credited_dump(tmp_path / "gcd.db")
    with sqlite3.connect(path) as db:
        if problem == "view":
            db.executescript(
                "ALTER TABLE gcd_story RENAME TO hidden_story; "
                "CREATE VIEW gcd_story AS SELECT * FROM hidden_story;"
            )
        elif problem == "virtual":
            db.executescript(
                "DROP TABLE gcd_story; CREATE VIRTUAL TABLE gcd_story USING fts5(issue_id);"
            )
        elif problem == "column":
            db.execute("ALTER TABLE gcd_story_credit RENAME COLUMN creator_id TO obsolete_id")
        elif problem == "name":
            db.execute("UPDATE gcd_creator_name_detail SET name=? WHERE id=101", ("x" * 256,))
        elif problem == "role":
            db.execute("UPDATE gcd_issue_credit SET credit_name=? WHERE id=12", ("x" * 101,))
        elif problem == "role_tail":
            db.execute(
                "UPDATE gcd_issue_credit SET credit_name=? WHERE id=12",
                ("writer" + " " * 110 + "designer",),
            )
        elif problem == "role_blob":
            db.execute(
                "UPDATE gcd_issue_credit SET credit_name=? WHERE id=12",
                (sqlite3.Binary(b"writer"),),
            )
        else:
            db.executemany(
                "INSERT INTO gcd_creator_name_detail VALUES (?,?,?,0)",
                [(1000 + n, 9000 + n, f"Author {n}") for n in range(129)],
            )
            db.executemany(
                "INSERT INTO gcd_story_credit VALUES (?,202,?,1,'',0,0)",
                [(1000 + n, 1000 + n) for n in range(129)],
            )
    adapter = GcdLocalSource(await validate_candidate(str(path)))
    with pytest.raises(MetadataSourceError) as failed:
        await adapter.issue("10")
    assert failed.value.status in {SourceStatus.INVALID_CONFIG, SourceStatus.INCOMPATIBLE_RESPONSE}
    assert str(path) not in str(failed.value)


def test_cover_and_interior_roles_keep_both_xml_fields_without_promoting_cover_only_art():
    assert creator_roles_to_comicinfo_fields([(row["name"], row["role"]) for row in EXPECTED]) == {
        "Writer": "Alan Moore",
        "Penciller": "Dave Gibbons",
        "Inker": "Dave Gibbons",
        "Colorist": "John Higgins",
        "Letterer": "Dave Gibbons",
        "CoverArtist": "Dave Gibbons, John Higgins",
        "Editor": "Len Wein",
    }
    assert creator_roles_to_comicinfo_fields([("Cover Only", "cover")]) == {
        "CoverArtist": "Cover Only"
    }


async def test_credit_queries_are_indexed_and_batched_for_a_full_issue_page(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from sqlalchemy.dialects.sqlite import dialect

    from pullbox.providers.metadata import gcd_local as adapter_module
    from pullbox.providers.metadata.gcd_local_credits import STORY_CREDIT, credit_query
    from pullbox.providers.metadata.gcd_local_database import open_readonly

    path = credited_dump(tmp_path / "gcd.db")
    with sqlite3.connect(path) as db:
        db.execute("CREATE INDEX deleted_story ON gcd_story(deleted)")
        db.execute("CREATE INDEX type_story ON gcd_story(type_id)")
        query = credit_query(STORY_CREDIT, list(range(10, 110))).compile(
            dialect=dialect(paramstyle="named"), compile_kwargs={"render_postcompile": True}
        )
        plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + str(query), query.params)]
        assert any("story_issue" in detail for detail in plan)
        assert any("story_credit_story" in detail for detail in plan)
        assert not any("deleted_story" in detail for detail in plan)
        assert not any("type_story" in detail for detail in plan)
        db.executemany(
            "INSERT INTO gcd_issue VALUES (?,2999,?,'',?,0,NULL,'','','',32)",
            [(1000 + n, str(1000 + n), 1000 + n) for n in range(100)],
        )
        db.executemany(
            "INSERT INTO gcd_story VALUES (?,?,19,0)",
            [(1000 + n, 1000 + n) for n in range(100)],
        )
        db.executemany(
            "INSERT INTO gcd_story_credit VALUES (?,?,101,1,'',0,0)",
            [(1000 + n, 1000 + n) for n in range(100)],
        )
    statements = []

    @contextmanager
    def traced(*args):
        with open_readonly(*args) as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(adapter_module, "open_readonly", traced)
    adapter = GcdLocalSource(await validate_candidate(str(path)))
    result = (await adapter.issues("2999")).data
    assert len(result.results) == 100
    assert sum(item.credits is not None for item in result.results) == 99
    reads = [sql for sql in statements if sql.startswith("SELECT")]
    assert len(reads) == 14, "Credit queries must not grow one per issue"


async def test_duplicate_story_credits_do_not_overflow_unique_credit_limit(tmp_path):
    path = credited_dump(tmp_path / "gcd.db")
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO gcd_story_credit VALUES (?,202,101,1,'',0,0)",
            [(1000 + n,) for n in range(129)],
        )
    adapter = GcdLocalSource(await validate_candidate(str(path)))
    assert (await adapter.issue("10")).data.model_dump(mode="json")["credits"] == EXPECTED


@pytest.mark.parametrize("story_type", [2, 12, 16, 26, 28, 29])
async def test_letters_ads_and_house_columns_are_not_comic_writer_credits(tmp_path, story_type):
    path = credited_dump(tmp_path / "gcd.db")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO gcd_story VALUES (999,10,?,0)", (story_type,))
        db.execute("INSERT INTO gcd_story_credit VALUES (999,999,104,1,'',0,0)")
    adapter = GcdLocalSource(await validate_candidate(str(path)))
    assert (await adapter.issue("10")).data.model_dump(mode="json")["credits"] == EXPECTED


@pytest.mark.parametrize("story_type", [5, 6, 7, 13, 19, 21])
async def test_all_gcd_core_story_categories_retain_descriptive_credits(tmp_path, story_type):
    path = credited_dump(tmp_path / "gcd.db")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE gcd_story SET type_id=? WHERE id=205", (story_type,))
    adapter = GcdLocalSource(await validate_candidate(str(path)))
    assert (await adapter.issue("11")).data.model_dump(mode="json")["credits"] == [
        {"name": "Len Wein", "role": "letterer"}
    ]


@pytest.mark.parametrize(
    "role, expected",
    [
        ("script, pencils, and inks", "cover, writer"),
        ("script, pencils, inks, and colors", "cover, writer"),
        ("script, pencils, inks, and letters", "cover, letterer, writer"),
        ("script, pencils, inks, colors, and letters", "cover, letterer, writer"),
        ("pencils, inks, and letters", "cover, letterer"),
    ],
)
async def test_composite_cover_credit_does_not_become_interior_art(tmp_path, role, expected):
    path = credited_dump(tmp_path / "gcd.db")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO gcd_credit_type VALUES (99,?)", (role,))
        db.execute("INSERT INTO gcd_creator_name_detail VALUES (999,9999,'Composite Cover',0)")
        db.execute("INSERT INTO gcd_story_credit VALUES (999,201,999,99,'',0,0)")
    adapter = GcdLocalSource(await validate_candidate(str(path)))
    credits = (await adapter.issue("10")).data.credits
    composite = next(item for item in credits if item.name == "Composite Cover")
    assert composite.role == expected


def test_credit_read_does_not_materialize_unbounded_optional_text(tmp_path):
    import threading
    import time

    from pullbox.providers.metadata.gcd_local_credits import STORY_CREDIT, credit_query
    from pullbox.providers.metadata.gcd_local_database import open_readonly, rows

    path = credited_dump(tmp_path / "gcd.db")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE gcd_creator_name_detail SET name=? WHERE id=101", ("x" * 1000000,))
    with open_readonly(path, threading.Event(), time.monotonic() + 1) as db:
        found = rows(db, credit_query(STORY_CREDIT, [10]))
        assert max(len(row["name"]) for row in found) == 256
