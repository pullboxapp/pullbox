"""Import previews must respect the browser's sensitive-directory boundary."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from pullbox.core import filesystem_policy
from pullbox.models.import_job import ImportSourceType
from pullbox.schemas.import_layout import LayoutPreviewRequest
from pullbox.schemas.import_story_arc_preflight import StoryArcPreflightRequest
from pullbox.services.import_layout_analysis import ImportLayoutAnalyzer, LayoutAnalysisBudget
from pullbox.services.import_story_arc_preflight import (
    StoryArcPreflightAnalyzer,
    StoryArcPreflightBudget,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX sensitive-directory policy")


@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("kind", ["layout", "arcs"])
async def test_preview_rejects_sensitive_root_before_scanning(
    tmp_path: Path, alias: bool, kind: str
) -> None:
    source = Path("/etc")
    if alias:
        source = tmp_path / "apparently-safe"
        source.symlink_to("/etc", target_is_directory=True)
    with pytest.raises(ValueError, match="sensitive"):
        if kind == "layout":
            await ImportLayoutAnalyzer().analyze(
                source, budget=LayoutAnalysisBudget(deadline_seconds=0)
            )
        else:
            await StoryArcPreflightAnalyzer().analyze(
                source,
                source_type=ImportSourceType.FILESYSTEM,
                budget=StoryArcPreflightBudget(deadline_seconds=0),
            )


@pytest.mark.parametrize("schema", [LayoutPreviewRequest, StoryArcPreflightRequest])
def test_preview_schema_rejects_sensitive_root(
    schema: type[LayoutPreviewRequest] | type[StoryArcPreflightRequest],
) -> None:
    with pytest.raises(ValueError, match="sensitive"):
        schema(source_path="/etc", source_type=ImportSourceType.FILESYSTEM)


@pytest.mark.parametrize("kind", ["layout", "arcs"])
async def test_preview_prunes_sensitive_descendants(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = Path("/var").resolve()
    remaining: list[str] = []

    def fake_walk(*args: object, **kwargs: object) -> Iterator[tuple[str, list[str], list[str]]]:
        directories = ["log"]
        yield str(root), directories, []
        remaining.extend(directories)

    monkeypatch.setattr(os, "walk", fake_walk)
    if kind == "layout":
        result = await ImportLayoutAnalyzer().analyze(root)
    else:
        result = await StoryArcPreflightAnalyzer().analyze(
            root, source_type=ImportSourceType.FILESYSTEM
        )
    assert remaining == []
    assert "sensitive_directory_skipped" in result.warnings


async def test_mylar_preview_checks_effective_database_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "mylar.db").symlink_to("/etc/hosts")
    read = AsyncMock()
    monkeypatch.setattr(StoryArcPreflightAnalyzer, "_analyze_mylar", read)
    with pytest.raises(ValueError, match="sensitive"):
        await StoryArcPreflightAnalyzer().analyze(tmp_path, source_type=ImportSourceType.MYLAR3)
    read.assert_not_called()


def test_mylar_schema_checks_effective_database_symlink(tmp_path: Path) -> None:
    (tmp_path / "mylar.db").symlink_to("/etc/hosts")
    with pytest.raises(ValueError, match="sensitive"):
        StoryArcPreflightRequest(source_path=str(tmp_path), source_type=ImportSourceType.MYLAR3)


def test_sensitive_directory_case_alias_is_blocked() -> None:
    alias = Path("/private/ETC")
    if not alias.exists() or not alias.samefile("/etc"):
        pytest.skip("Requires a case-insensitive filesystem")
    with pytest.raises(ValueError, match="sensitive"):
        filesystem_policy.resolve_preview_source(alias)


async def test_layout_preview_continues_after_pruning_sensitive_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    safe = tmp_path / "Batman"
    safe.mkdir()
    (safe / "Issue 001.cbz").write_bytes(b"comic")
    monkeypatch.setattr(filesystem_policy, "BLOCKED_DIRECTORY_PREFIXES", frozenset({str(blocked)}))
    result = await ImportLayoutAnalyzer().analyze(tmp_path)
    assert result.files_considered == 1
    assert result.partial is True
    assert "sensitive_directory_skipped" in result.warnings
