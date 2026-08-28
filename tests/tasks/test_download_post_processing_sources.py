"""Download post-processing source helper characterization tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_post_processing_source_module_exposes_path_helpers() -> None:
    """Source discovery and path mapping helpers should live beside the task module."""
    from pullbox.tasks import download_post_processing_sources

    assert download_post_processing_sources._POST_PROCESSING_SOURCE_RETRY_DELAYS == (
        0.0,
        0.5,
        1.0,
        2.0,
        4.0,
    )
    assert callable(download_post_processing_sources._find_comic_file)
    assert callable(download_post_processing_sources._probe_post_processing_source)
    assert callable(download_post_processing_sources._resolve_local_download_root)
    assert callable(download_post_processing_sources._resolve_local_path)


@pytest.mark.asyncio
async def test_resolve_local_download_root_uses_enabled_client_directory() -> None:
    """The configured local directory should define the cleanup boundary."""
    from pullbox.models.download import DownloadClientType
    from pullbox.tasks.download_post_processing_sources import _resolve_local_download_root

    result = MagicMock()
    result.scalars.return_value.first.return_value = SimpleNamespace(download_dir="/downloads/")
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    download = SimpleNamespace(download_client=DownloadClientType.SABNZBD)

    root = await _resolve_local_download_root(session, download)

    assert root == Path("/downloads")


@pytest.mark.asyncio
async def test_resolve_local_download_root_requires_configured_directory() -> None:
    """Cleanup should fail closed when no local download root is configured."""
    from pullbox.models.download import DownloadClientType
    from pullbox.tasks.download_post_processing_sources import _resolve_local_download_root

    result = MagicMock()
    result.scalars.return_value.first.return_value = SimpleNamespace(download_dir=None)
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    download = SimpleNamespace(download_client=DownloadClientType.SABNZBD)

    root = await _resolve_local_download_root(session, download)

    assert root is None


@pytest.mark.asyncio
async def test_airdcpp_local_path_uses_exact_client_and_strict_mapper(tmp_path: Path) -> None:
    from pullbox.models.download import DownloadClientType
    from pullbox.tasks.download_post_processing_sources import _resolve_local_path

    local_root = tmp_path / "airdcpp"
    local_root.mkdir()
    result = MagicMock()
    result.scalar_one_or_none.return_value = SimpleNamespace(
        remote_path="/Downloads",
        download_dir=str(local_root),
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    download = SimpleNamespace(
        download_client=DownloadClientType.AIRDCPP,
        download_client_config_id=22,
        downloaded_path="/Downloads/Example.cbz",
    )

    mapped = await _resolve_local_path(session, download)

    assert mapped == str((local_root / "Example.cbz").resolve(strict=False))
    statement = session.execute.await_args.args[0]
    assert 22 in statement.compile().params.values()


@pytest.mark.asyncio
async def test_resolve_local_path_normalizes_windows_remote_path() -> None:
    """Windows client separators must not leak into container paths."""
    from pullbox.models.download import DownloadClientType
    from pullbox.tasks.download_post_processing_sources import _resolve_local_path

    result = MagicMock()
    result.scalars.return_value.first.return_value = SimpleNamespace(
        remote_path=r"E:\Temp\Pullbox_Downloads",
        download_dir="/downloads",
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    download = SimpleNamespace(
        download_client=DownloadClientType.SABNZBD,
        downloaded_path=r"E:\Temp\Pullbox_Downloads\Release\file.cbr",
    )

    resolved = await _resolve_local_path(session, download)

    assert resolved == "/downloads/Release/file.cbr"
    assert "\\" not in resolved


@pytest.mark.asyncio
async def test_resolve_local_path_rejects_unmapped_windows_path() -> None:
    """An unmapped Windows path must fail before container filesystem probing."""
    from pullbox.models.download import DownloadClientType
    from pullbox.tasks.download_post_processing_sources import _resolve_local_path

    result = MagicMock()
    result.scalars.return_value.first.return_value = SimpleNamespace(
        remote_path=r"D:\Downloads",
        download_dir="/downloads",
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    download = SimpleNamespace(
        download_client=DownloadClientType.SABNZBD,
        downloaded_path=r"E:\Temp\Release\file.cbr",
    )

    with pytest.raises(FileNotFoundError, match="does not match the configured Remote Path"):
        await _resolve_local_path(session, download)


@pytest.mark.asyncio
async def test_resolve_local_path_matches_windows_paths_case_insensitively() -> None:
    """Windows drive and path casing must not affect a valid mapping."""
    from pullbox.models.download import DownloadClientType
    from pullbox.tasks.download_post_processing_sources import _resolve_local_path

    result = MagicMock()
    result.scalars.return_value.first.return_value = SimpleNamespace(
        remote_path=r"E:\Temp\Pullbox_Downloads",
        download_dir="/downloads",
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    download = SimpleNamespace(
        download_client=DownloadClientType.SABNZBD,
        downloaded_path=r"e:\temp\pullbox_downloads\Release\file.cbr",
    )

    resolved = await _resolve_local_path(session, download)

    assert resolved == "/downloads/Release/file.cbr"


@pytest.mark.asyncio
async def test_resolve_local_path_requires_windows_component_boundary() -> None:
    """A similarly prefixed sibling folder must not map into the download root."""
    from pullbox.models.download import DownloadClientType
    from pullbox.tasks.download_post_processing_sources import _resolve_local_path

    result = MagicMock()
    result.scalars.return_value.first.return_value = SimpleNamespace(
        remote_path=r"E:\Temp\Pullbox_Downloads",
        download_dir="/downloads",
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    download = SimpleNamespace(
        download_client=DownloadClientType.SABNZBD,
        downloaded_path=r"E:\Temp\Pullbox_Downloads-old\Release\file.cbr",
    )

    with pytest.raises(FileNotFoundError, match="does not match the configured Remote Path"):
        await _resolve_local_path(session, download)


@pytest.mark.asyncio
async def test_resolve_local_path_maps_windows_unc_path() -> None:
    """UNC roots should map with the same component-aware Windows semantics."""
    from pullbox.models.download import DownloadClientType
    from pullbox.tasks.download_post_processing_sources import _resolve_local_path

    result = MagicMock()
    result.scalars.return_value.first.return_value = SimpleNamespace(
        remote_path=r"\\server\share\downloads",
        download_dir="/downloads",
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    download = SimpleNamespace(
        download_client=DownloadClientType.SABNZBD,
        downloaded_path=r"\\SERVER\SHARE\Downloads\Release\file.cbr",
    )

    resolved = await _resolve_local_path(session, download)

    assert resolved == "/downloads/Release/file.cbr"


@pytest.mark.asyncio
async def test_resolve_local_path_rejects_windows_parent_traversal() -> None:
    """A client-reported parent traversal must not escape the local mapping root."""
    from pullbox.models.download import DownloadClientType
    from pullbox.tasks.download_post_processing_sources import _resolve_local_path

    result = MagicMock()
    result.scalars.return_value.first.return_value = SimpleNamespace(
        remote_path=r"E:\Temp\Pullbox_Downloads",
        download_dir="/downloads",
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    download = SimpleNamespace(
        download_client=DownloadClientType.SABNZBD,
        downloaded_path=r"E:\Temp\Pullbox_Downloads\..\outside\file.cbr",
    )

    with pytest.raises(FileNotFoundError, match="unsafe parent traversal"):
        await _resolve_local_path(session, download)


@pytest.mark.asyncio
async def test_resolve_local_path_preserves_posix_literal_backslash() -> None:
    """POSIX paths may legally contain a literal backslash in a filename."""
    from pullbox.models.download import DownloadClientType
    from pullbox.tasks.download_post_processing_sources import _resolve_local_path

    result = MagicMock()
    result.scalars.return_value.first.return_value = SimpleNamespace(
        remote_path="/remote",
        download_dir="/downloads/Batman\\Superman",
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    download = SimpleNamespace(
        download_client=DownloadClientType.SABNZBD,
        downloaded_path="/remote/file.cbz",
    )

    resolved = await _resolve_local_path(session, download)

    assert resolved == "/downloads/Batman\\Superman/file.cbz"


@pytest.mark.asyncio
async def test_resolve_local_download_root_preserves_posix_literal_backslash() -> None:
    """The configured local cleanup root must not reinterpret POSIX filenames."""
    from pullbox.models.download import DownloadClientType
    from pullbox.tasks.download_post_processing_sources import _resolve_local_download_root

    result = MagicMock()
    result.scalars.return_value.first.return_value = SimpleNamespace(
        download_dir="/downloads/Batman\\Superman"
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    download = SimpleNamespace(download_client=DownloadClientType.SABNZBD)

    root = await _resolve_local_download_root(session, download)

    assert root == Path("/downloads/Batman\\Superman").resolve(strict=False)


@pytest.mark.asyncio
async def test_probe_rejects_filesystem_root_before_recursive_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed paths must not invoke recursive discovery against the root."""
    from pullbox.tasks import download_post_processing_sources as sources

    finder = MagicMock(return_value=None)
    monkeypatch.setattr(sources, "_POST_PROCESSING_SOURCE_RETRY_DELAYS", (0.0,))

    with pytest.raises(RuntimeError, match="filesystem root"):
        await sources._probe_post_processing_source(
            Path("/downloads\\Release\\file.cbr"),
            {".cbr"},
            find_comic_file=finder,
        )

    finder.assert_not_called()


def test_post_processing_integrity_exception_distinguishes_missing_source() -> None:
    """Transient missing files should stay typed separately from bad releases."""
    from pullbox.tasks import download_post_processing_sources

    exc = download_post_processing_sources._build_post_processing_integrity_exception(
        Path("/downloads/Missing.cbz"),
        ["File not found: /downloads/Missing.cbz"],
    )

    assert isinstance(exc, FileNotFoundError)
