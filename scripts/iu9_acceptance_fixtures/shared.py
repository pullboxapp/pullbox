"""Shared deterministic archives, seed validation, and manifest evidence."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import struct
import zipfile
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import cast
from xml.etree.ElementTree import Element, SubElement, tostring

RAR3_SIGNATURE = b"Rar!\x1a\x07\x00"
RAR5_SIGNATURE = b"Rar!\x1a\x07\x01\x00"
CBR_SEED_DESCRIPTOR = "cbr-seeds.json"
CBR_SEED_FILENAMES = {
    "rar3": "iu9-rar3.cbr",
    "rar5": "iu9-rar5.cbr",
}
CBR_FIXTURE_FILENAMES = {
    "rar3": "Genuine CBR Seeds 001 (2026).cbr",
    "rar5": "Genuine CBR Seeds 002 (2026).cbr",
}
_FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)
_HEX_DIGITS = frozenset("0123456789abcdef")


class CbrSeedValidationError(ValueError):
    """Raised when an externally supplied genuine-CBR seed is not trustworthy."""


@dataclass(frozen=True, slots=True)
class CbrSeedEvidence:
    """Validated provenance and destination evidence for one CBR seed."""

    seed_id: str
    archive_format: str
    source_filename: str
    destination: Path
    sha256: str
    source_url: str
    license: str
    expected_members: tuple[str, ...]

    def to_manifest(self, fixture_root: Path) -> dict[str, object]:
        """Return path-safe JSON evidence relative to the fixture root."""
        values = asdict(self)
        values["destination"] = self.destination.relative_to(fixture_root).as_posix()
        values["expected_members"] = list(self.expected_members)
        return values


def prepare_fixture_root(root: Path) -> Path:
    """Create a fresh fixture root without deleting existing content."""
    root = root.expanduser().absolute()
    if root.exists():
        if not root.is_dir():
            raise FileExistsError(f"Fixture output is not a directory: {root}")
        if any(root.iterdir()):
            raise FileExistsError(f"Fixture output must be empty: {root}")
    else:
        root.mkdir(parents=True)
    root.chmod(0o755)
    return root


def write_bytes(path: Path, payload: bytes, *, mode: int = 0o644) -> Path:
    """Write deterministic fixture bytes with stable permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)
    return path


def write_text(path: Path, text: str, *, mode: int = 0o644) -> Path:
    """Write deterministic UTF-8 fixture text with stable permissions."""
    return write_bytes(path, text.encode("utf-8"), mode=mode)


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest for a regular file."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deterministic_png(*, seed: int, identity: str) -> bytes:
    """Build a valid deterministic 2x2 RGB PNG from a seed and identity."""
    color = hashlib.sha256(f"{seed}:{identity}".encode()).digest()[:3]
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        crc = struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        return struct.pack(">I", len(payload)) + body + crc

    image_row = b"\x00" + (color * 2)
    pixels = image_row * 2
    return b"".join(
        (
            signature,
            chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)),
            chunk(b"IDAT", zlib.compress(pixels, level=9)),
            chunk(b"IEND", b""),
        )
    )


def deterministic_jpeg() -> bytes:
    """Return a tiny valid 1x1 JPEG for cover sidecars."""
    return bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb004300"
        "01010101010101010101010101010101010101010101010101"
        "01010101010101010101010101010101010101010101010101"
        "010101010101ffc0000b080001000101011100ffc40014000100"
        "000000000000000000000000000000ffc4001410010000000000"
        "0000000000000000000000ffda0008010100003f00d2cfffd9"
    )


def comic_info_xml(
    *,
    series: str,
    number: str,
    title: str | None = None,
    year: int | None = None,
    publisher: str | None = None,
    comicvine_series_id: int | None = None,
    comicvine_issue_id: int | None = None,
) -> bytes:
    """Build deterministic ComicInfo.xml without deferred Metron metadata."""
    root = Element("ComicInfo")
    SubElement(root, "Series").text = series
    SubElement(root, "Number").text = number
    if title is not None:
        SubElement(root, "Title").text = title
    if year is not None:
        SubElement(root, "Year").text = str(year)
    if publisher is not None:
        SubElement(root, "Publisher").text = publisher
    identity_tags: list[str] = []
    if comicvine_series_id is not None:
        identity_tags.append(f"[cv_vol_id:{comicvine_series_id}]")
    if comicvine_issue_id is not None:
        identity_tags.append(f"[cv_issue_id:{comicvine_issue_id}]")
    if identity_tags:
        SubElement(root, "Notes").text = " ".join(identity_tags)
    xml = cast("bytes", tostring(root, encoding="utf-8", xml_declaration=True))
    return xml + b"\n"


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def create_deterministic_zip(path: Path, members: dict[str, bytes]) -> Path:
    """Create a deterministic ZIP-family archive from safe member names."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, payload in members.items():
            member = PurePosixPath(name)
            if member.is_absolute() or ".." in member.parts:
                raise ValueError(f"Unsafe deterministic ZIP member: {name}")
            archive.writestr(_zip_info(name), payload)
    path.chmod(0o644)
    return path


def create_deterministic_cbz(
    path: Path,
    *,
    seed: int,
    case_id: str,
    series: str,
    number: str,
    title: str | None = None,
    year: int | None = None,
    publisher: str | None = None,
    comicvine_series_id: int | None = None,
    comicvine_issue_id: int | None = None,
    page_count: int = 3,
    include_comicinfo: bool = True,
) -> Path:
    """Create a valid deterministic CBZ with PNG pages and ComicInfo.xml."""
    if page_count < 1:
        raise ValueError("A valid CBZ fixture requires at least one page")
    members: dict[str, bytes] = {}
    if include_comicinfo:
        members["ComicInfo.xml"] = comic_info_xml(
            series=series,
            number=number,
            title=title,
            year=year,
            publisher=publisher,
            comicvine_series_id=comicvine_series_id,
            comicvine_issue_id=comicvine_issue_id,
        )
    members.update(
        {
            f"pages/{page:03d}.png": deterministic_png(
                seed=seed,
                identity=f"{case_id}:page:{page}",
            )
            for page in range(1, page_count + 1)
        }
    )
    return create_deterministic_zip(path, members)


def load_json(path: Path) -> dict[str, object]:
    """Read a JSON object used by the fixture contract."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return cast("dict[str, object]", value)


def _descriptor_rows(descriptor: dict[str, object]) -> dict[str, dict[str, object]]:
    if descriptor.get("schema_version") != 1:
        raise CbrSeedValidationError("Unsupported CBR seed descriptor schema")
    raw_rows = descriptor.get("seeds")
    if not isinstance(raw_rows, list):
        raise CbrSeedValidationError("CBR seed descriptor must contain a seed list")
    rows: dict[str, dict[str, object]] = {}
    for raw_row in raw_rows:
        if not isinstance(raw_row, dict) or not isinstance(raw_row.get("id"), str):
            raise CbrSeedValidationError("CBR seed descriptor contains an invalid row")
        row = cast("dict[str, object]", raw_row)
        seed_id = cast("str", row["id"])
        if seed_id in rows:
            raise CbrSeedValidationError(f"Duplicate CBR seed id: {seed_id}")
        rows[seed_id] = row
    if set(rows) != set(CBR_SEED_FILENAMES):
        raise CbrSeedValidationError("CBR seed descriptor must define rar3 and rar5")
    return rows


def _safe_expected_members(value: object, *, seed_id: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise CbrSeedValidationError(f"{seed_id} must declare expected archive members")
    members: list[str] = []
    for raw_member in value:
        if not isinstance(raw_member, str):
            raise CbrSeedValidationError(f"{seed_id} has an invalid expected member")
        member = PurePosixPath(raw_member)
        if member.is_absolute() or ".." in member.parts:
            raise CbrSeedValidationError(f"{seed_id} has an unsafe expected member")
        members.append(raw_member)
    return tuple(members)


def _expected_digest(
    row: dict[str, object],
    *,
    seed_id: str,
    expected_sha256: dict[str, str] | None,
) -> str | None:
    caller_digest = None if expected_sha256 is None else expected_sha256.get(seed_id)
    descriptor_digest = row.get("sha256")
    if descriptor_digest is not None and not isinstance(descriptor_digest, str):
        raise CbrSeedValidationError(f"{seed_id} SHA-256 must be text")
    if caller_digest is not None and descriptor_digest not in {None, caller_digest}:
        raise CbrSeedValidationError(f"{seed_id} SHA-256 pins disagree")
    digest = caller_digest or descriptor_digest
    if digest is not None and (
        len(digest) != 64 or any(character not in _HEX_DIGITS for character in digest)
    ):
        raise CbrSeedValidationError(f"{seed_id} SHA-256 pin is invalid")
    return digest


def consume_cbr_seed_set(
    seed_dir: Path,
    destination: Path,
    *,
    expected_sha256: dict[str, str] | None = None,
    destination_filenames: dict[str, str] | None = None,
) -> tuple[CbrSeedEvidence, ...]:
    """Validate and copy fixed RAR3/RAR5 CBR seeds without downloading them."""
    descriptor_path = seed_dir / CBR_SEED_DESCRIPTOR
    if not descriptor_path.is_file() or descriptor_path.is_symlink():
        raise CbrSeedValidationError(f"Missing regular {CBR_SEED_DESCRIPTOR}")
    rows = _descriptor_rows(load_json(descriptor_path))
    validated: list[tuple[str, Path, str, str, str, tuple[str, ...]]] = []
    for seed_id in ("rar3", "rar5"):
        row = rows[seed_id]
        expected_filename = CBR_SEED_FILENAMES[seed_id]
        if row.get("filename") != expected_filename:
            raise CbrSeedValidationError(f"{seed_id} must use fixed filename {expected_filename}")
        if row.get("archive_format") != seed_id:
            raise CbrSeedValidationError(f"{seed_id} archive family does not match its id")
        source = seed_dir / expected_filename
        if source.is_symlink() or not source.is_file():
            raise CbrSeedValidationError(f"{seed_id} seed must be a regular file")
        with source.open("rb") as seed_file:
            payload_prefix = seed_file.read(len(RAR5_SIGNATURE))
        expected_signature = RAR3_SIGNATURE if seed_id == "rar3" else RAR5_SIGNATURE
        if not payload_prefix.startswith(expected_signature):
            raise CbrSeedValidationError(f"{seed_id.upper()} signature is invalid")
        actual_digest = sha256_file(source)
        pinned_digest = _expected_digest(
            row,
            seed_id=seed_id,
            expected_sha256=expected_sha256,
        )
        if pinned_digest is not None and actual_digest != pinned_digest:
            raise CbrSeedValidationError(f"{seed_id} SHA-256 does not match its pin")
        source_url = row.get("source_url")
        license_name = row.get("license")
        if not isinstance(source_url, str) or not source_url.startswith("https://"):
            raise CbrSeedValidationError(f"{seed_id} must declare an HTTPS source URL")
        if not isinstance(license_name, str) or not license_name.strip():
            raise CbrSeedValidationError(f"{seed_id} must declare a license")
        expected_members = _safe_expected_members(row.get("expected_members"), seed_id=seed_id)
        target_name = (
            CBR_FIXTURE_FILENAMES[seed_id]
            if destination_filenames is None
            else destination_filenames.get(seed_id)
        )
        if not isinstance(target_name, str):
            raise CbrSeedValidationError(f"Missing destination filename for {seed_id}")
        target_path = PurePosixPath(target_name)
        if (
            target_path.is_absolute()
            or len(target_path.parts) != 1
            or target_path.suffix.casefold() != ".cbr"
        ):
            raise CbrSeedValidationError(f"Unsafe destination filename for {seed_id}")
        validated.append(
            (seed_id, source, actual_digest, source_url, license_name, expected_members)
        )

    destination.mkdir(parents=True, exist_ok=True)
    evidence: list[CbrSeedEvidence] = []
    for seed_id, source, digest, source_url, license_name, expected_members in validated:
        target_name = (
            CBR_FIXTURE_FILENAMES[seed_id]
            if destination_filenames is None
            else destination_filenames[seed_id]
        )
        target = destination / target_name
        shutil.copyfile(source, target)
        target.chmod(0o644)
        evidence.append(
            CbrSeedEvidence(
                seed_id=seed_id,
                archive_format=seed_id,
                source_filename=source.name,
                destination=target,
                sha256=digest,
                source_url=source_url,
                license=license_name,
                expected_members=expected_members,
            )
        )
    return tuple(evidence)


def _archive_evidence(path: Path) -> tuple[str | None, list[str]]:
    try:
        with zipfile.ZipFile(path) as archive:
            return "zip", archive.namelist()
    except (OSError, zipfile.BadZipFile):
        with path.open("rb") as archive_file:
            prefix = archive_file.read(len(RAR5_SIGNATURE))
        if prefix.startswith(RAR5_SIGNATURE):
            return "rar5", []
        if prefix.startswith(RAR3_SIGNATURE):
            return "rar3", []
        if path.suffix.casefold() in {".cbz", ".cbr", ".zip", ".rar"}:
            return "unreadable", []
        return None, []


def snapshot_tree(root: Path) -> list[dict[str, object]]:
    """Capture deterministic, relative evidence for generated files and links."""
    paths = sorted(
        (path for path in root.rglob("*") if path.is_file() or path.is_symlink()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    inode_paths: dict[tuple[int, int], list[str]] = {}
    for path in paths:
        metadata = path.lstat()
        if stat.S_ISREG(metadata.st_mode):
            inode_paths.setdefault((metadata.st_dev, metadata.st_ino), []).append(
                path.relative_to(root).as_posix()
            )
    hardlink_labels = {
        path_name: f"hardlink-{index:03d}"
        for index, names in enumerate(
            sorted((names for names in inode_paths.values() if len(names) > 1), key=min),
            start=1,
        )
        for path_name in names
    }

    rows: list[dict[str, object]] = []
    for path in paths:
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            rows.append(
                {
                    "path": relative,
                    "type": "symlink",
                    "link_target": os.readlink(path),
                }
            )
            continue
        archive_format, members = _archive_evidence(path)
        row: dict[str, object] = {
            "path": relative,
            "type": "file",
            "size": metadata.st_size,
            "mode": stat.S_IMODE(metadata.st_mode),
            "sha256": sha256_file(path),
        }
        if relative in hardlink_labels:
            row["hardlink_group"] = hardlink_labels[relative]
        if archive_format is not None:
            row["archive_format"] = archive_format
            row["archive_members"] = members
        rows.append(row)
    return rows


def write_manifest(root: Path, manifest: dict[str, object]) -> Path:
    """Write canonical JSON without timestamps or absolute host paths."""
    path = root / "manifest.json"
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return write_text(path, payload)
