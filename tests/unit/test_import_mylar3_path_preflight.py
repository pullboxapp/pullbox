"""Focused path-inventory contracts for Mylar import preflight."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from pullbox.models.import_job import ImportFileHandlingMode
from pullbox.models.library import LibraryRoot
from pullbox.schemas.import_mylar3_path_preflight import MylarPathMappingDraft
from pullbox.services import import_mylar3_path_preflight as path_preflight

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession


def _write_mylar_path_database(
    database: Path,
    *,
    comics: list[tuple[str, str]],
    issues: list[tuple[str, str]],
) -> None:
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE comics (ComicID TEXT, ComicLocation TEXT)")
    connection.execute("CREATE TABLE issues (ComicID TEXT, Location TEXT)")
    connection.executemany(
        "INSERT INTO comics (ComicID, ComicLocation) VALUES (?, ?)",
        comics,
    )
    connection.executemany(
        "INSERT INTO issues (ComicID, Location) VALUES (?, ?)",
        issues,
    )
    connection.commit()
    connection.close()


async def test_preflight_includes_associated_absolute_issue_locations(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    series_directory = library_root / "Series"
    series_directory.mkdir(parents=True)
    (series_directory / "Issue 001.cbz").write_bytes(b"comic")
    orphan_directory = library_root / "Orphan"
    orphan_directory.mkdir()
    (orphan_directory / "Issue 999.cbz").write_bytes(b"orphan")

    database = tmp_path / "mylar.db"
    _write_mylar_path_database(
        database,
        comics=[("CV-1", "/stored/Series")],
        issues=[
            ("CV-1", "/stored/Series/Issue 001.cbz"),
            ("CV-1", "Issue 002.cbz"),
            ("CV-999", "/stored/Orphan/Issue 999.cbz"),
        ],
    )
    db_session.add(
        LibraryRoot(
            name="Library",
            path=str(library_root),
            enabled=True,
            allow_referenced_registrations=True,
        )
    )
    await db_session.flush()

    preview = await path_preflight.Mylar3PathPreflightAnalyzer().analyze(
        db_session,
        database,
        auto_detect=False,
        mappings=[
            MylarPathMappingDraft(
                stored_prefix="/stored",
                pullbox_prefix=str(library_root),
            )
        ],
    )

    assert preview.resolution.locations == 2
    assert preview.resolution.mapped_existing == 2
    assert preview.resolution.invalid == 0
    assert preview.can_confirm is True
    assert [
        example.relative_path for mapping in preview.mappings for example in mapping.examples
    ] == ["Series", "Series/Issue 001.cbz"]


async def test_in_place_mapping_is_tried_before_external_identity_is_rejected(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    external_root = tmp_path / "external"
    (external_root / "Series").mkdir(parents=True)
    reference_root = tmp_path / "reference"
    (reference_root / "Series").mkdir(parents=True)
    database = tmp_path / "mylar.db"
    _write_mylar_path_database(
        database,
        comics=[("CV-1", str(external_root / "Series"))],
        issues=[],
    )
    db_session.add(
        LibraryRoot(
            name="Reference library",
            path=str(reference_root),
            enabled=True,
            allow_referenced_registrations=True,
        )
    )
    await db_session.flush()

    preview = await path_preflight.Mylar3PathPreflightAnalyzer().analyze(
        db_session,
        database,
        auto_detect=False,
        mappings=[
            MylarPathMappingDraft(
                stored_prefix=str(external_root),
                pullbox_prefix=str(reference_root),
            )
        ],
        file_handling_mode=ImportFileHandlingMode.IN_PLACE,
    )

    assert preview.resolution.identity_resolved == 0
    assert preview.resolution.mapped_existing == 1
    assert preview.resolution.outside_root == 0
    assert preview.mappings[0].provenance == "manual"
    assert preview.can_confirm is True


async def test_incomplete_automatic_mapping_cannot_be_confirmed(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    (library_root / "Existing Series").mkdir(parents=True)
    database = tmp_path / "mylar.db"
    _write_mylar_path_database(
        database,
        comics=[
            ("CV-1", "/stored/Existing Series"),
            ("CV-2", "/stored/Missing Series"),
        ],
        issues=[],
    )
    db_session.add(LibraryRoot(name="Library", path=str(library_root), enabled=True))
    await db_session.flush()

    preview = await path_preflight.Mylar3PathPreflightAnalyzer().analyze(
        db_session,
        database,
        auto_detect=True,
        mappings=[],
    )

    assert preview.resolution.mapped_existing == 1
    assert preview.resolution.mapped_missing == 1
    assert preview.mappings[0].provenance == "automatic"
    assert preview.mappings[0].status == "review"
    assert "automatic_mapping_incomplete" in preview.warnings
    assert preview.can_confirm is False


async def test_manual_mapping_can_be_confirmed_after_missing_paths_are_reviewed(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    (library_root / "Existing Series").mkdir(parents=True)
    database = tmp_path / "mylar.db"
    _write_mylar_path_database(
        database,
        comics=[
            ("CV-1", "/stored/Existing Series"),
            ("CV-2", "/stored/Missing Series"),
        ],
        issues=[],
    )
    db_session.add(LibraryRoot(name="Library", path=str(library_root), enabled=True))
    await db_session.flush()

    preview = await path_preflight.Mylar3PathPreflightAnalyzer().analyze(
        db_session,
        database,
        auto_detect=False,
        mappings=[
            MylarPathMappingDraft(
                stored_prefix="/stored",
                pullbox_prefix=str(library_root),
            )
        ],
    )

    assert preview.resolution.mapped_existing == 1
    assert preview.resolution.mapped_missing == 1
    assert preview.mappings[0].provenance == "manual"
    assert "automatic_mapping_incomplete" not in preview.warnings
    assert preview.can_confirm is True


async def test_repreviewed_automatic_mapping_retains_automatic_provenance(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    (library_root / "Existing Series").mkdir(parents=True)
    database = tmp_path / "mylar.db"
    _write_mylar_path_database(
        database,
        comics=[
            ("CV-1", "/stored/Existing Series"),
            ("CV-2", "/stored/Missing Series"),
        ],
        issues=[],
    )
    db_session.add(LibraryRoot(name="Library", path=str(library_root), enabled=True))
    await db_session.flush()
    analyzer = path_preflight.Mylar3PathPreflightAnalyzer()
    automatic = await analyzer.analyze(
        db_session,
        database,
        auto_detect=True,
        mappings=[],
    )

    repeated = await analyzer.analyze(
        db_session,
        database,
        auto_detect=True,
        mappings=[
            MylarPathMappingDraft(
                stored_prefix=stored_prefix,
                pullbox_prefix=pullbox_prefix,
            )
            for stored_prefix, pullbox_prefix in automatic.path_map.items()
        ],
    )

    assert repeated.mappings[0].provenance == "automatic"
    assert "automatic_mapping_incomplete" in repeated.warnings
    assert repeated.can_confirm is False


def test_location_inventory_applies_database_limit_to_combined_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "mylar.db"
    _write_mylar_path_database(
        database,
        comics=[("CV-1", "/stored/Series")],
        issues=[
            ("CV-1", "/stored/Series/Issue 001.cbz"),
            ("CV-1", "/stored/Series/Issue 002.cbz"),
        ],
    )
    statements: list[str] = []
    real_connect = sqlite3.connect

    def traced_connect(database_path: str, *, uri: bool = False) -> sqlite3.Connection:
        connection = real_connect(database_path, uri=uri)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(path_preflight, "_MAX_LOCATIONS", 2)
    monkeypatch.setattr(sqlite3, "connect", traced_connect)

    locations, partial = path_preflight.Mylar3PathPreflightAnalyzer._read_locations(database)

    inventory_queries = [
        statement
        for statement in statements
        if "COMICLOCATION" in statement.upper() and statement.lstrip().upper().startswith("SELECT")
    ]
    assert len(locations) == 2
    assert partial is True
    assert len(inventory_queries) == 1
    assert "LIMIT 3" in inventory_queries[0].upper()


async def test_identity_missing_paths_have_actionable_exceptions_and_safe_continuation(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root = tmp_path / "comics"
    (root / "Existing").mkdir(parents=True)
    database = tmp_path / "mylar.db"
    _write_mylar_path_database(
        database,
        comics=[
            ("1", str(root / "Existing")),
            ("2", str(root / "Missing")),
            ("3", str(tmp_path / "other-mount" / "Unmapped")),
        ],
        issues=[],
    )
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE comics ADD COLUMN ComicName TEXT")
        connection.execute("UPDATE comics SET ComicName = 'Missing series' WHERE ComicID = '2'")
    db_session.add(LibraryRoot(name="Comics", path=str(root), enabled=True))
    await db_session.flush()
    preview = await path_preflight.Mylar3PathPreflightAnalyzer().analyze(
        db_session,
        database,
        auto_detect=True,
        mappings=[],
    )
    data = preview.model_dump()
    assert data["resolution"].get("missing") == 1
    assert data["resolution"]["unmapped"] == 1
    assert data.get("can_continue_with_unresolved") is True
    assert data.get("requires_unresolved_acknowledgement") is True
    assert preview.can_confirm is False
    missing = next(
        item for item in data["exceptions"] if item["stored_path"] == str(root / "Missing")
    )
    assert missing["series_name"] == "Missing series"
    assert missing["series_id"] == "2"
    assert missing["attempted_path"] == str(root / "Missing")
    assert missing["outcome"] == "missing"
    assert missing["reason"] and missing["suggested_action"]


async def test_permission_denied_is_not_reported_as_unmapped(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "comics"
    (root / "Existing").mkdir(parents=True)
    denied = root / "Denied"
    database = tmp_path / "mylar.db"
    _write_mylar_path_database(
        database, comics=[("1", str(root / "Existing")), ("2", str(denied))], issues=[]
    )
    db_session.add(LibraryRoot(name="Comics", path=str(root), enabled=True))
    await db_session.flush()
    original = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        if path == denied:
            raise PermissionError("blocked")
        return original(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    preview = await path_preflight.Mylar3PathPreflightAnalyzer().analyze(
        db_session,
        database,
        auto_detect=True,
        mappings=[],
    )
    assert preview.resolution.unreadable == 1
    assert preview.resolution.unmapped == 0
    assert preview.model_dump().get("can_continue_with_unresolved") is False


async def test_entirely_unavailable_library_cannot_continue(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root = tmp_path / "comics"
    root.mkdir()
    database = tmp_path / "mylar.db"
    _write_mylar_path_database(database, comics=[("1", str(root / "Missing"))], issues=[])
    db_session.add(LibraryRoot(name="Comics", path=str(root), enabled=True))
    await db_session.flush()
    for automatic in (True, False):
        preview = await path_preflight.Mylar3PathPreflightAnalyzer().analyze(
            db_session,
            database,
            auto_detect=automatic,
            mappings=[],
        )
        assert preview.can_confirm is False
        assert preview.model_dump().get("can_continue_with_unresolved") is False


async def test_io_errors_and_sensitive_missing_paths_are_not_skippable(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "comics"
    (root / "Existing").mkdir(parents=True)
    broken = root / "Broken"
    database = tmp_path / "mylar.db"
    _write_mylar_path_database(
        database,
        comics=[("1", str(root / "Existing")), ("2", str(broken))],
        issues=[],
    )
    db_session.add(LibraryRoot(name="Comics", path=str(root), enabled=True))
    await db_session.flush()
    original_resolve = Path.resolve

    def resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path == broken:
            raise OSError("Input/output error")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    preview = await path_preflight.Mylar3PathPreflightAnalyzer().analyze(
        db_session,
        database,
        auto_detect=False,
        mappings=[],
    )
    assert not preview.can_continue_with_unresolved
    assert preview.resolution.invalid == 1
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE comics SET ComicLocation=? WHERE ComicID='2'",
            ("/etc/missing-comic",),
        )
    preview = await path_preflight.Mylar3PathPreflightAnalyzer().analyze(
        db_session,
        database,
        auto_detect=False,
        mappings=[],
    )
    assert not preview.can_continue_with_unresolved
    assert preview.resolution.invalid == 1
