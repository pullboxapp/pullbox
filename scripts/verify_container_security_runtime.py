"""Verify security-sensitive libraries embedded in the production runtime."""

from __future__ import annotations

import pyexpat

MINIMUM_EXPAT_VERSION = (2, 8, 1)


def verify_expat_version(version: tuple[int, int, int]) -> None:
    """Reject runtimes whose Python XML parser lacks reviewed Expat fixes."""
    if version < MINIMUM_EXPAT_VERSION:
        actual = ".".join(str(part) for part in version)
        required = ".".join(str(part) for part in MINIMUM_EXPAT_VERSION)
        raise SystemExit(f"Expat {required} or newer is required; found {actual}")


def main() -> None:
    """Run all container runtime security assertions."""
    verify_expat_version(pyexpat.version_info)
    print(f"Container Expat runtime verified: {pyexpat.EXPAT_VERSION}")


if __name__ == "__main__":
    main()
