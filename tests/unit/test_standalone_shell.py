"""Shared shell asset versions must change when a served controller changes."""

import os
from pathlib import Path

import pytest

from pullbox.ui import standalone_shell


@pytest.mark.parametrize(
    "script", ["story-arc-preview.js", "story-arc-detail.js", "series-rescan.js"]
)
def test_story_arc_controller_change_invalidates_main_shell_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str
) -> None:
    assets = tuple(tmp_path / asset.name for asset in standalone_shell._MAIN_SHELL_ASSET_PATHS)
    for asset in assets:
        asset.write_text("original asset", encoding="utf-8")
    controller = tmp_path / script
    controller.write_text("original controller", encoding="utf-8")
    os.utime(controller, ns=(1_000_000_000, 1_000_000_000))
    monkeypatch.setattr(standalone_shell, "_MAIN_SHELL_ASSET_PATHS", assets)
    original = standalone_shell.main_shell_asset_version()
    assert standalone_shell.main_shell_asset_version() == original

    controller.write_text("updated controller", encoding="utf-8")
    os.utime(controller, ns=(2_000_000_000, 2_000_000_000))

    assert standalone_shell.main_shell_asset_version() != original
