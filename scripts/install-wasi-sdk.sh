#!/usr/bin/env bash
# Download and unpack WASI-SDK into ./toolchain. See spec/implementation.md §4.
#
# Idempotent: if toolchain/bin/clang already exists, exits successfully.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLCHAIN="${REPO_ROOT}/toolchain"

# Pinned in §4.1. Bump deliberately and rebuild the reproducibility baseline.
WASI_SDK_VERSION="${WASI_SDK_VERSION:-24}"
WASI_SDK_PATCH="${WASI_SDK_PATCH:-0}"

if [ -x "${TOOLCHAIN}/bin/clang" ]; then
    echo "WASI-SDK already present at ${TOOLCHAIN}; skipping."
    exit 0
fi

uname_s="$(uname -s)"
uname_m="$(uname -m)"
case "${uname_s}-${uname_m}" in
    Linux-x86_64)   platform="x86_64-linux" ;;
    Linux-aarch64)  platform="arm64-linux" ;;
    Darwin-x86_64)  platform="x86_64-macos" ;;
    Darwin-arm64)   platform="arm64-macos" ;;
    *)
        echo "error: unsupported platform ${uname_s}-${uname_m}" >&2
        exit 1
        ;;
esac

tarball="wasi-sdk-${WASI_SDK_VERSION}.${WASI_SDK_PATCH}-${platform}.tar.gz"
url="https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-${WASI_SDK_VERSION}/${tarball}"

echo "downloading ${url}"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
curl -fsSL "${url}" -o "${tmp}/${tarball}"
mkdir -p "${TOOLCHAIN}"
tar -xzf "${tmp}/${tarball}" -C "${TOOLCHAIN}" --strip-components=1

echo "installed WASI-SDK ${WASI_SDK_VERSION}.${WASI_SDK_PATCH} at ${TOOLCHAIN}"
