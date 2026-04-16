cmake_minimum_required(VERSION 3.24)

# quickjs-wasm shim. See spec/implementation.md §4, §6.
#
# Builds a WASI reactor binary that statically links QuickJS (vendored at
# ../vendor/quickjs-ng) and exports the qjs_* shim ABI declared in shim.h.
# The two host imports (host_call, host_interrupt) are left unresolved and
# satisfied by the Python bridge at instantiate time.

project(quickjs_wasm_shim LANGUAGES C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

if(NOT CMAKE_SYSTEM_NAME STREQUAL "WASI")
    message(FATAL_ERROR
        "This project must be configured with the WASI-SDK toolchain. "
        "Pass -DCMAKE_TOOLCHAIN_FILE=${CMAKE_CURRENT_SOURCE_DIR}/wasi-sdk.cmake.")
endif()

set(QUICKJS_NG_SRC "${CMAKE_CURRENT_SOURCE_DIR}/../vendor/quickjs-ng")

# Pull in quickjs-ng as a subdirectory. We only need the static `qjs` target;
# skip the CLI and examples.
set(QJS_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(QJS_ENABLE_INSTALL OFF CACHE BOOL "" FORCE)
set(BUILD_SHARED_LIBS  OFF CACHE BOOL "" FORCE)
add_subdirectory("${QUICKJS_NG_SRC}" quickjs-ng-build EXCLUDE_FROM_ALL)

add_executable(quickjs shim.c)
target_include_directories(quickjs PRIVATE "${QUICKJS_NG_SRC}")
target_link_libraries(quickjs PRIVATE qjs)

# Stack / heap sizing. The spec caps linear memory via JS_SetMemoryLimit, but
# we still need a reasonable initial/max wasm linear memory ceiling.
set(WASM_STACK_SIZE  "1048576"   CACHE STRING "Wasm data-stack size in bytes (1 MB)")
set(WASM_MAX_MEMORY  "268435456" CACHE STRING "Wasm linear memory ceiling in bytes (256 MB)")

target_link_options(quickjs PRIVATE
    # Reactor (library) model: no _start, exports callable from the host.
    -mexec-model=reactor
    # Export everything marked JS_EXTERN / __attribute__((export_name)).
    -Wl,--export-dynamic
    # The shim allocates guest buffers for host-provided data (§6.2 qjs_malloc).
    -Wl,--export=malloc
    -Wl,--export=free
    -Wl,--export=realloc
    -Wl,--export=calloc
    # Memory limits.
    "-Wl,-z,stack-size=${WASM_STACK_SIZE}"
    "-Wl,--max-memory=${WASM_MAX_MEMORY}"
    # Reproducibility: no build-time timestamps, deterministic LEB encoding.
    -Wl,--no-entry
)

# Reproducible build (§4.3).
target_compile_options(quickjs PRIVATE -fno-ident)

set_target_properties(quickjs PROPERTIES
    OUTPUT_NAME "quickjs"
    SUFFIX ".wasm"
)
