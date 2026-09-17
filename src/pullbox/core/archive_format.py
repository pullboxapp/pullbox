"""Bounded container identification shared by comic readers and safety checks."""

from pathlib import Path

ARCHIVE_SUFFIXES = frozenset({".cbz", ".zip", ".cbr", ".rar", ".cb7", ".7z", ".cbt", ".tar"})
_ALIASES = {"zip": "cbz", "rar": "cbr", "7z": "cb7", "tar": "cbt"}


def archive_format(path: Path) -> str:
    """Prefer a recognized header, retaining normal parser errors for corrupt files.

    Do not identify arbitrary executable or document suffixes as comic archives.
    Call from an I/O worker. Reading 512 bytes is independent of archive size.
    """
    declared = path.suffix.lower().lstrip(".")
    if path.suffix.lower() not in ARCHIVE_SUFFIXES:
        return declared
    try:
        with path.open("rb") as source:
            header = source.read(512)
    except OSError:
        return _ALIASES.get(declared, declared)
    if header.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "cbz"
    if header.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")):
        return "cbr"
    if header.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "cb7"
    if header.startswith(b"%PDF"):
        return "pdf"
    if header[257:262] == b"ustar":
        return "cbt"
    return _ALIASES.get(declared, declared)
