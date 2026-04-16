# WASI-SDK toolchain file for quickjs-wasm.
#
# Expects WASI_SDK_PATH to point at an unpacked WASI-SDK tree (defaults to
# ../toolchain relative to this repository). See spec/implementation.md §4.

if(NOT DEFINED WASI_SDK_PATH)
    if(DEFINED ENV{WASI_SDK_PATH})
        set(WASI_SDK_PATH "$ENV{WASI_SDK_PATH}")
    else()
        get_filename_component(_repo_root "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)
        set(WASI_SDK_PATH "${_repo_root}/toolchain")
    endif()
endif()

if(NOT EXISTS "${WASI_SDK_PATH}/bin/clang")
    message(FATAL_ERROR
        "WASI-SDK not found at ${WASI_SDK_PATH}. "
        "Run scripts/install-wasi-sdk.sh or set WASI_SDK_PATH.")
endif()

set(CMAKE_SYSTEM_NAME WASI)
set(CMAKE_SYSTEM_PROCESSOR wasm32)

set(CMAKE_C_COMPILER   "${WASI_SDK_PATH}/bin/clang")
set(CMAKE_CXX_COMPILER "${WASI_SDK_PATH}/bin/clang++")
set(CMAKE_AR           "${WASI_SDK_PATH}/bin/llvm-ar" CACHE FILEPATH "")
set(CMAKE_RANLIB       "${WASI_SDK_PATH}/bin/llvm-ranlib" CACHE FILEPATH "")
set(CMAKE_STRIP        "${WASI_SDK_PATH}/bin/llvm-strip" CACHE FILEPATH "")

set(CMAKE_C_COMPILER_TARGET   wasm32-wasip1)
set(CMAKE_CXX_COMPILER_TARGET wasm32-wasip1)

set(CMAKE_SYSROOT "${WASI_SDK_PATH}/share/wasi-sysroot")
set(CMAKE_FIND_ROOT_PATH "${CMAKE_SYSROOT}")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
