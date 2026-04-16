#!/usr/bin/env bash
# Rebuild quickjs.wasm and byte-compare it against the committed checksum.
# See spec/implementation.md §4.3.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RESOURCES="${REPO_ROOT}/quickjs_wasm/_resources"
CHECKSUM="${RESOURCES}/quickjs.wasm.sha256"

if [ ! -f "${CHECKSUM}" ]; then
    echo "error: ${CHECKSUM} not found; nothing to verify against." >&2
    echo "       Run ./wasm/build.sh to establish a baseline." >&2
    exit 1
fi

expected="$(awk '{print $1}' "${CHECKSUM}")"

# Rebuild into a scratch location to avoid clobbering the committed artifact.
scratch="$(mktemp -d)"
trap 'rm -rf "${scratch}"' EXIT

cp -R "${REPO_ROOT}/wasm" "${scratch}/wasm"
export SOURCE_DATE_EPOCH=0
export ZERO_AR_DATE=1

(
    cd "${scratch}"
    mkdir -p quickjs_wasm/_resources
    ln -s "${REPO_ROOT}/vendor" vendor
    ln -s "${REPO_ROOT}/toolchain" toolchain
    ./wasm/build.sh >/dev/null
    actual="$(shasum -a 256 quickjs_wasm/_resources/quickjs.wasm | awk '{print $1}')"
    if [ "${actual}" != "${expected}" ]; then
        echo "reproducibility failure:" >&2
        echo "  expected: ${expected}" >&2
        echo "  actual:   ${actual}"   >&2
        exit 1
    fi
    echo "reproducible: ${actual}"
)
