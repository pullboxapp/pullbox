from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_SOURCE = PROJECT_ROOT / "native" / "mega_bridge" / "main.cpp"
BRIDGE_CMAKE = PROJECT_ROOT / "native" / "mega_bridge" / "CMakeLists.txt"
DOCKERFILE = PROJECT_ROOT / "docker" / "Dockerfile"
DEV_DOCKERFILE = PROJECT_ROOT / "docker" / "Dockerfile.dev"


def test_mega_sdk_source_and_digest_are_pinned_in_production_image() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "e11a1a4648bdee70dac67ecb2200c1507e4de53c" in dockerfile
    assert "fd4d37848ed3c3cf799695e3aa47cb69c2f5938e6efe530a718d3f1fbf7c46e8" in dockerfile
    assert "sha256sum -c -" in dockerfile
    assert "pullbox-mega-bridge" in dockerfile
    assert "copy_binary /usr/bin/pullbox-mega-bridge" in dockerfile


def test_mega_builder_installs_archive_decompression_support() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    mega_builder_dependencies = dockerfile.split(" AS mega-builder", maxsplit=1)[1].split(
        " AS builder", maxsplit=1
    )[0]

    assert "gzip" in mega_builder_dependencies


def test_production_mega_builder_uses_verified_archive_without_git() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    mega_builder = dockerfile.split("\nFROM ", maxsplit=1)[0]
    packages = mega_builder.split("apt-get install", maxsplit=1)[1].split("&&", maxsplit=1)[0]

    assert "git" not in packages.split()
    assert 'curl -fsSL "${MEGA_SDK_SOURCE_URL}" -o /tmp/mega-sdk.tar.gz' in mega_builder
    assert 'echo "${MEGA_SDK_SOURCE_SHA256}  /tmp/mega-sdk.tar.gz" | sha256sum -c -' in mega_builder
    assert "tar -xzf /tmp/mega-sdk.tar.gz" in mega_builder


def test_development_image_includes_the_same_pinned_mega_bridge() -> None:
    dockerfile = DEV_DOCKERFILE.read_text(encoding="utf-8")

    assert "e11a1a4648bdee70dac67ecb2200c1507e4de53c" in dockerfile
    assert "fd4d37848ed3c3cf799695e3aa47cb69c2f5938e6efe530a718d3f1fbf7c46e8" in dockerfile
    assert "sha256sum -c -" in dockerfile
    assert "pullbox-mega-bridge" in dockerfile
    assert "COPY --from=mega-builder /mega-runtime/ /" in dockerfile


def test_mega_bridge_uses_stdin_protocol_and_official_sdk_controls() -> None:
    source = BRIDGE_SOURCE.read_text(encoding="utf-8")

    assert "PULLBOX_MEGA_BRIDGE 1" in source
    assert "std::cin" in source
    assert "MegaCancelToken::createInstance" in source
    assert "startDownload" in source
    assert "getPublicNode" in source
    assert "loginToFolder" in source
    assert "fastLogin" in source
    assert "argv[1]" not in source
    assert "getenv" not in source
    assert "public_link" not in source
    assert "session=" not in source


def test_mega_bridge_uses_account_session_for_files_without_leaking_into_folders() -> None:
    source = BRIDGE_SOURCE.read_text(encoding="utf-8")

    assert 'const auto accountCachePath = cachePath / "account";' in source
    assert 'const auto publicCachePath = cachePath / "public";' in source
    assert "validateAccountSession(accountApi, request.accountSession)" in source
    assert "mega::MegaApi accountApi(nullptr" in source
    assert "mega::MegaApi publicApi(nullptr" in source
    assert "appKey" not in source
    assert "api.fetchNodes(&nodesListener)" in source
    assert source.index("api.fastLogin") < source.index("api.fetchNodes")
    assert "resolveFileNode(accountApi, request.link)" in source
    assert "resolveFolderNode(publicApi, request.link)" in source
    assert "resolveFolderNode(accountApi, request.link)" not in source


def test_mega_bridge_build_disables_unneeded_sdk_features() -> None:
    cmake = BRIDGE_CMAKE.read_text(encoding="utf-8")

    assert "ENABLE_SYNC OFF" in cmake
    assert "ENABLE_MEDIA_FILE_METADATA OFF" in cmake
    assert "USE_FREEIMAGE OFF" in cmake
    assert "USE_FFMPEG OFF" in cmake
    assert "USE_LIBUV OFF" in cmake
    assert "USE_PDFIUM OFF" in cmake
    assert "ENABLE_SDKLIB_TESTS OFF" in cmake
    assert "ENABLE_SDKLIB_EXAMPLES OFF" in cmake
