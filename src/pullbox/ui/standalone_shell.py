"""Helpers for versioned standalone setup/login shells."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pullbox

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
_STANDALONE_SHELL_ASSET_PATHS = (
    _STATIC_DIR / "css" / "tailwind.css",
    _STATIC_DIR / "js" / "pullbox.js",
)
_MAIN_SHELL_ASSET_PATHS = (
    _STATIC_DIR / "css" / "tailwind.css",
    _STATIC_DIR / "js" / "htmx.min.js",
    _STATIC_DIR / "js" / "idiomorph-ext.min.js",
    _STATIC_DIR / "js" / "alpine.min.js",
    _STATIC_DIR / "js" / "pullbox.js",
    _STATIC_DIR / "js" / "story-arc-preview.js",
    _STATIC_DIR / "js" / "story-arc-detail.js",
)


def standalone_shell_version(*template_paths: str) -> str:
    """Return a compact version fingerprint for standalone page shells."""
    digest = hashlib.sha256()
    digest.update(pullbox.__version__.encode("utf-8"))

    for template_path in template_paths:
        digest.update(template_path.encode("utf-8"))
        full_path = _TEMPLATE_DIR / template_path
        try:
            digest.update(str(full_path.stat().st_mtime_ns).encode("utf-8"))
        except FileNotFoundError:
            digest.update(b"missing")

    for asset_path in _STANDALONE_SHELL_ASSET_PATHS:
        digest.update(str(asset_path).encode("utf-8"))
        try:
            digest.update(str(asset_path.stat().st_mtime_ns).encode("utf-8"))
        except FileNotFoundError:
            digest.update(b"missing")

    return digest.hexdigest()[:16]


def main_shell_asset_version() -> str:
    """Return a compact fingerprint for the shared authenticated app shell."""
    digest = hashlib.sha256()
    digest.update(pullbox.__version__.encode("utf-8"))

    for asset_path in _MAIN_SHELL_ASSET_PATHS:
        digest.update(str(asset_path).encode("utf-8"))
        try:
            digest.update(str(asset_path.stat().st_mtime_ns).encode("utf-8"))
        except FileNotFoundError:
            digest.update(b"missing")

    return digest.hexdigest()[:16]
