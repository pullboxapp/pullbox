#!/usr/bin/env bash

set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "Usage: $0 <actionlint|gitleaks|grype> [install-dir]" >&2
  exit 1
fi

tool="$1"
install_dir="${2:-.cache/tools}"
mkdir -p "${install_dir}"

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"

if command -v sha256sum >/dev/null 2>&1; then
  checksum_cmd="sha256sum"
else
  checksum_cmd="shasum -a 256"
fi

case "${tool}" in
  actionlint)
    version="1.7.12"
    binary="actionlint"
    checksum_file="actionlint_${version}_checksums.txt"
    base_url="https://github.com/rhysd/actionlint/releases/download/v${version}"
    case "${arch}" in
      x86_64|amd64) archive_arch="amd64" ;;
      arm64|aarch64) archive_arch="arm64" ;;
      *)
        echo "Unsupported actionlint architecture: ${arch}" >&2
        exit 1
        ;;
    esac
    archive="${binary}_${version}_${os}_${archive_arch}.tar.gz"
    version_args="-version"
    ;;
  gitleaks)
    version="8.30.1"
    binary="gitleaks"
    checksum_file="gitleaks_${version}_checksums.txt"
    base_url="https://github.com/gitleaks/gitleaks/releases/download/v${version}"
    case "${arch}" in
      x86_64|amd64) archive_arch="x64" ;;
      arm64|aarch64) archive_arch="arm64" ;;
      *)
        echo "Unsupported gitleaks architecture: ${arch}" >&2
        exit 1
        ;;
    esac
    archive="${binary}_${version}_${os}_${archive_arch}.tar.gz"
    version_args="version"
    ;;
  grype)
    version="0.110.0"
    binary="grype"
    checksum_file="grype_${version}_checksums.txt"
    base_url="https://github.com/anchore/grype/releases/download/v${version}"
    case "${arch}" in
      x86_64|amd64) archive_arch="amd64" ;;
      arm64|aarch64) archive_arch="arm64" ;;
      *)
        echo "Unsupported grype architecture: ${arch}" >&2
        exit 1
        ;;
    esac
    archive="${binary}_${version}_${os}_${archive_arch}.tar.gz"
    version_args="version"
    ;;
  *)
    echo "Unsupported tool: ${tool}" >&2
    exit 1
    ;;
esac

binary_path="${install_dir}/${binary}"

if [ -x "${binary_path}" ] && "${binary_path}" ${version_args} 2>/dev/null | grep -Fq "${version}"; then
  exit 0
fi

tmpdir="$(mktemp -d)"
cleanup() {
  rm -rf "${tmpdir}"
}
trap cleanup EXIT

curl -fsSLo "${tmpdir}/${archive}" "${base_url}/${archive}"
curl -fsSLo "${tmpdir}/checksums.txt" "${base_url}/${checksum_file}"
(
  cd "${tmpdir}"
  grep " ${archive}$" checksums.txt | ${checksum_cmd} -c -
  tar -xzf "${archive}" "${binary}"
)
mv "${tmpdir}/${binary}" "${binary_path}"
chmod +x "${binary_path}"
"${binary_path}" ${version_args} >/dev/null
