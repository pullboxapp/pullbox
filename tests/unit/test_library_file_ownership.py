"""Referenced library ownership and containment tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pullbox.core.exceptions import ConfigurationError
from pullbox.core.library_file_ownership import (
    ReferencedFileValidationError,
    build_file_identity_signature,
    resolve_referenced_library_root,
    resolve_referenced_source_root,
    validate_file_identity_signature,
)
from pullbox.models.library import LibraryRoot

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


async def _add_root(
    session: AsyncSession,
    path: Path,
    *,
    name: str,
    enabled: bool = True,
    allow_referenced_registrations: bool = True,
) -> LibraryRoot:
    path.mkdir(parents=True, exist_ok=True)
    root = LibraryRoot(
        name=name,
        path=str(path),
        enabled=enabled,
        allow_referenced_registrations=allow_referenced_registrations,
    )
    session.add(root)
    await session.flush()
    return root


async def test_referenced_root_rejects_sibling_prefix(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    await _add_root(db_session, tmp_path / "comics", name="Comics")
    sibling_file = tmp_path / "comics-old" / "Issue 001.cbz"
    sibling_file.parent.mkdir()
    sibling_file.write_bytes(b"comic")

    with pytest.raises(ConfigurationError, match="allows referenced registrations"):
        await resolve_referenced_library_root(db_session, sibling_file, None)


@pytest.mark.parametrize("escape", ["sibling", "symlink", "disabled"])
async def test_referenced_source_directory_rejects_root_escapes(
    db_session: AsyncSession, tmp_path: Path, escape: str
) -> None:
    root_path = tmp_path / "comics"
    await _add_root(db_session, root_path, name="Comics", enabled=escape != "disabled")
    source = root_path
    if escape in {"sibling", "symlink"}:
        outside = tmp_path / "comics-old"
        outside.mkdir()
        source = outside
        if escape == "symlink":
            source = root_path / "escape"
            source.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ReferencedFileValidationError) as exc:
        await resolve_referenced_source_root(db_session, source, None)
    assert exc.value.reason == "source_outside_root"


async def test_referenced_source_directory_rejects_ambiguous_nested_roots(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    root = await _add_root(db_session, tmp_path / "comics", name="Comics")
    nested = await _add_root(db_session, tmp_path / "comics" / "nested", name="Nested")
    path = tmp_path / "comics" / "nested"

    with pytest.raises(ReferencedFileValidationError) as exc_info:
        await resolve_referenced_source_root(db_session, path, None)

    assert exc_info.value.reason == "source_root_ambiguous"
    assert exc_info.value.message == (
        "In-place import source matches multiple enabled reference-capable library roots."
    )
    explicit, _ = await resolve_referenced_source_root(db_session, path, root.id)
    assert explicit.id == root.id
    explicit_nested, _ = await resolve_referenced_source_root(db_session, path, nested.id)
    assert explicit_nested.id == nested.id


async def test_referenced_root_rejects_symlink_escape(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library_path = tmp_path / "comics"
    await _add_root(db_session, library_path, name="Comics")
    outside_path = tmp_path / "outside"
    outside_path.mkdir()
    outside_file = outside_path / "Issue 001.cbz"
    outside_file.write_bytes(b"comic")
    (library_path / "escape").symlink_to(outside_path, target_is_directory=True)

    with pytest.raises(ConfigurationError, match="allows referenced registrations"):
        await resolve_referenced_library_root(
            db_session,
            library_path / "escape" / outside_file.name,
            None,
        )


async def test_referenced_root_rejects_ambiguous_nested_roots(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    outer = await _add_root(db_session, tmp_path / "comics", name="All Comics")
    inner = await _add_root(db_session, tmp_path / "comics" / "dc", name="DC")
    source = tmp_path / "comics" / "dc" / "Batman" / "Issue 001.cbz"
    source.parent.mkdir()
    source.write_bytes(b"comic")

    with pytest.raises(ReferencedFileValidationError) as exc_info:
        await resolve_referenced_library_root(db_session, source, None)

    assert exc_info.value.reason == "source_root_ambiguous"
    assert exc_info.value.message == (
        "Referenced library file matches multiple enabled reference-capable library roots."
    )
    selected_root, resolved_source, signature = await resolve_referenced_library_root(
        db_session, source, inner.id
    )
    assert selected_root.id == inner.id
    assert selected_root.id != outer.id
    assert resolved_source == source.resolve()
    assert signature["resolved_path"] == str(source.resolve())


@pytest.mark.parametrize("source_kind", ["file", "directory"])
async def test_referenced_resolvers_reject_ambiguous_aliased_roots(
    db_session: AsyncSession,
    tmp_path: Path,
    source_kind: str,
) -> None:
    root_path = tmp_path / "comics"
    await _add_root(db_session, root_path, name="Comics")
    (root_path / "Batman").mkdir()
    alias_path = root_path / "legacy-alias"
    alias_path.symlink_to(root_path, target_is_directory=True)
    alias = LibraryRoot(
        name="Legacy alias",
        path=str(alias_path),
        enabled=True,
        allow_referenced_registrations=True,
    )
    db_session.add(alias)
    await db_session.flush()
    directory = alias_path / "Batman"
    source = directory
    if source_kind == "file":
        source = directory / "Issue 001.cbz"
        source.write_bytes(b"comic")

    with pytest.raises(ReferencedFileValidationError) as exc_info:
        if source_kind == "file":
            await resolve_referenced_library_root(db_session, source, None)
        else:
            await resolve_referenced_source_root(db_session, source, None)

    assert exc_info.value.reason == "source_root_ambiguous"
    assert "multiple enabled reference-capable library roots" in exc_info.value.message


@pytest.mark.parametrize(
    ("root_state", "message"),
    [
        ("missing", "Selected library root does not exist."),
        ("disabled", "Selected library root is disabled."),
        (
            "references_disabled",
            "Selected library root does not allow referenced registrations.",
        ),
    ],
)
@pytest.mark.parametrize("source_kind", ["file", "directory"])
async def test_explicit_referenced_root_requires_reference_capability(
    db_session: AsyncSession,
    tmp_path: Path,
    root_state: str,
    message: str,
    source_kind: str,
) -> None:
    root = await _add_root(
        db_session,
        tmp_path / "comics",
        name="Comics",
        enabled=root_state != "disabled",
        allow_referenced_registrations=root_state != "references_disabled",
    )
    source = tmp_path / "comics"
    if source_kind == "file":
        source = source / "Issue 001.cbz"
        source.write_bytes(b"comic")
    root_id = 999999 if root_state == "missing" else root.id

    with pytest.raises(ConfigurationError, match=message.replace(".", r"\.")) as exc_info:
        if source_kind == "file":
            await resolve_referenced_library_root(db_session, source, root_id)
        else:
            await resolve_referenced_source_root(db_session, source, root_id)

    assert str(exc_info.value) == message
    if source_kind == "directory":
        assert isinstance(exc_info.value, ReferencedFileValidationError)
        assert exc_info.value.reason == "source_outside_root"


@pytest.mark.parametrize("source_kind", ["file", "directory"])
async def test_implicit_referenced_root_ignores_non_reference_capable_root(
    db_session: AsyncSession,
    tmp_path: Path,
    source_kind: str,
) -> None:
    await _add_root(
        db_session,
        tmp_path / "comics",
        name="Managed only",
        allow_referenced_registrations=False,
    )
    source = tmp_path / "comics"
    if source_kind == "file":
        source = source / "Issue 001.cbz"
        source.write_bytes(b"comic")

    with pytest.raises(ConfigurationError, match="allows referenced registrations"):
        if source_kind == "file":
            await resolve_referenced_library_root(db_session, source, None)
        else:
            await resolve_referenced_source_root(db_session, source, None)


async def test_referenced_root_rejects_disabled_or_wrong_explicit_root(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    enabled = await _add_root(db_session, tmp_path / "enabled", name="Enabled")
    disabled = await _add_root(
        db_session,
        tmp_path / "disabled",
        name="Disabled",
        enabled=False,
    )
    source = tmp_path / "enabled" / "Issue 001.cbz"
    source.write_bytes(b"comic")

    with pytest.raises(ConfigurationError, match="is disabled"):
        await resolve_referenced_library_root(db_session, source, disabled.id)

    other = await _add_root(db_session, tmp_path / "other", name="Other")
    with pytest.raises(ConfigurationError, match="allows referenced registrations"):
        await resolve_referenced_library_root(db_session, source, other.id)

    selected_root, _, _ = await resolve_referenced_library_root(
        db_session,
        source,
        enabled.id,
    )
    assert selected_root.id == enabled.id


async def test_referenced_root_rejects_parent_components(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library_path = tmp_path / "comics"
    await _add_root(db_session, library_path, name="Comics")
    source = library_path / "Issue 001.cbz"
    source.write_bytes(b"comic")

    with pytest.raises(ConfigurationError, match="unsafe path components"):
        await resolve_referenced_library_root(
            db_session,
            library_path / "unused" / ".." / source.name,
            None,
        )


def test_file_identity_signature_accepts_unchanged_file(tmp_path: Path) -> None:
    source = tmp_path / "Issue 001.cbz"
    source.write_bytes(b"comic")
    expected = build_file_identity_signature(source)

    validate_file_identity_signature(expected, build_file_identity_signature(source))


def test_file_identity_signature_classifies_changed_file(tmp_path: Path) -> None:
    source = tmp_path / "Issue 001.cbz"
    source.write_bytes(b"comic")
    expected = build_file_identity_signature(source)
    source.write_bytes(b"replacement comic")

    with pytest.raises(ReferencedFileValidationError) as exc_info:
        validate_file_identity_signature(expected, build_file_identity_signature(source))

    assert exc_info.value.reason == "source_changed"


def test_file_identity_signature_rejects_missing_scan_evidence(tmp_path: Path) -> None:
    source = tmp_path / "Issue 001.cbz"
    source.write_bytes(b"comic")

    with pytest.raises(ReferencedFileValidationError) as exc_info:
        validate_file_identity_signature({}, build_file_identity_signature(source))

    assert exc_info.value.reason == "source_signature_missing"
