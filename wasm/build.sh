#!/usr/bin/env bash
# Build quickjs.wasm. See spec/implementation.md §4.
#
# Pipeline: cmake (WASI-SDK) -> wasm-opt -O3 -> strip -> copy into
# quickjs_wasm/_resources/quickjs.wasm. Reproducible given the pinned
# submodule commit and pinned toolchain.
set -euo pipefail

WASM_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${WASM_DIR}/.." && pwd)"
BUILD_DIR="${WASM_DIR}/build"
OUT_DIR="${REPO_ROOT}/quickjs_wasm/_resources"
OUT="${OUT_DIR}/quickjs.wasm"

: "${WASI_SDK_PATH:=${REPO_ROOT}/toolchain}"
export WASI_SDK_PATH

# Reproducibility (§4.3).
export SOURCE_DATE_EPOCH=0
export ZERO_AR_DATE=1

if [ ! -x "${WASI_SDK_PATH}/bin/clang" ]; then
    echo "error: WASI-SDK not found at ${WASI_SDK_PATH}" >&2
    echo "       Run scripts/install-wasi-sdk.sh or set WASI_SDK_PATH." >&2
    exit 1
fi

if ! command -v cmake >/dev/null 2>&1; then
    echo "error: cmake not found on PATH" >&2
    exit 1
fi

if ! command -v wasm-opt >/dev/null 2>&1; then
    echo "error: wasm-opt (binaryen) not found on PATH" >&2
    exit 1
fi

mkdir -p "${BUILD_DIR}" "${OUT_DIR}"

cmake -S "${WASM_DIR}" -B "${BUILD_DIR}" \
    -DCMAKE_TOOLCHAIN_FILE="${WASM_DIR}/wasi-sdk.cmake" \
    -DCMAKE_BUILD_TYPE=Release \
    -DWASI_SDK_PATH="${WASI_SDK_PATH}"

cmake --build "${BUILD_DIR}" --target quickjs --parallel

RAW="${BUILD_DIR}/quickjs.wasm"
if [ ! -f "${RAW}" ]; then
    echo "error: expected ${RAW} to exist after build" >&2
    exit 1
fi

wasm-opt -O3 --strip-debug --strip-producers "${RAW}" -o "${OUT}"

# Refresh the checksum used by verify-reproducible.sh.
(cd "${OUT_DIR}" && shasum -a 256 quickjs.wasm > quickjs.wasm.sha256)

echo "built: ${OUT} ($(wc -c < "${OUT}") bytes)"
