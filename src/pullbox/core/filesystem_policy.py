"""Shared import path-text and sensitive-directory policy."""

import unicodedata
from pathlib import Path

_SUPPORTED_PATH_FORMAT_CHARACTERS = frozenset(
    "\u00ad\u061c\u200b\u200c\u200d\u200e\u200f\u2060\ufeff"
)
_MAX_PATH_TEXT_LENGTH = 4096
_BLOCKED_DIRS = ("/etc", "/proc", "/sys", "/dev", "/run", "/boot", "/root", "/var/log", "/var/run")
BLOCKED_DIRECTORY_PREFIXES = frozenset(
    prefix for directory in _BLOCKED_DIRS for prefix in (directory, str(Path(directory).resolve()))
)


def is_invalid_path_text(value: str) -> bool:
    """Accept literal multilingual paths without weakening containment checks.

    Existing filenames may contain nonbreaking spaces, soft hyphens, joiners,
    zero-width spaces, or Arabic/left-to-right/right-to-left marks. Preserve
    those characters exactly; stripping them could select a different file.
    Other non-printable characters, including controls, surrogates, line/paragraph
    separators, and bidi embeddings/overrides/isolates, remain rejected.
    Callers must still resolve paths and check root containment separately.
    """
    if not value or len(value) > _MAX_PATH_TEXT_LENGTH:
        return True
    if value.isprintable():
        return False
    return any(
        not character.isprintable()
        and character not in _SUPPORTED_PATH_FORMAT_CHARACTERS
        and unicodedata.category(character) != "Zs"
        for character in value
    )


def is_sensitive_path(path: Path) -> bool:
    """Check an already-resolved path against the browser's system-directory policy."""
    for prefix in BLOCKED_DIRECTORY_PREFIXES:
        blocked = Path(prefix)
        if path == blocked or blocked in path.parents:
            return True
        # resolve() retains directory casing on case-insensitive filesystems.
        # Probe identity only for case aliases, not every sampled comic path.
        ancestor = Path(*path.parts[: len(blocked.parts)])
        if str(ancestor).casefold() == str(blocked).casefold():
            try:
                if ancestor.samefile(blocked):
                    return True
            except OSError:
                return True
    return False


def resolve_preview_source(source: str | Path) -> Path:
    """Reject unsafe preview sources without redirecting the scan to another root.

    External import folders are allowed; only in-place adoption requires an
    enabled library root. Validation is repeated by analyzers before use.
    """
    raw = str(source)
    if is_invalid_path_text(raw) or ".." in Path(raw).parts:
        raise ValueError("Import preview source contains unsafe path components")
    try:
        # Resolution only probes the path. Sensitive aliases are rejected below
        # before either analyzer can enumerate a directory or open a database.
        # codeql[py/path-injection]
        resolved = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("Import preview source is unavailable") from exc
    if is_sensitive_path(resolved):
        raise ValueError("Import preview cannot inspect sensitive system directories")
    return resolved
