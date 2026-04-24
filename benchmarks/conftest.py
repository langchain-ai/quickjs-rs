"""Shared fixtures for the benchmark suite. See benchmarks/README.md.

These benchmarks measure time, not correctness — setup goes in
fixtures so only the operation under test lands inside
``benchmark(...)``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest

from quickjs_rs import Context, Runtime


@pytest.fixture(scope="module")
def rt() -> Iterator[Runtime]:
    """Module-scoped Runtime so the instantiation cost
    doesn't get counted against every benchmark in a file. Benchmarks
    that measure startup construct their own Runtime explicitly."""
    runtime = Runtime()
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.fixture
def ctx(rt: Runtime) -> Iterator[Context]:
    """Fresh Context per benchmark. Cheap relative to Runtime
    (see bench_context_create in test_startup.py). Closing on
    teardown keeps the runtime clean for the next benchmark —
    leaking contexts would leak globals state that could skew
    later benchmarks in subtle ways."""
    context = rt.new_context()
    try:
        yield context
    finally:
        context.close()


@pytest.fixture
async def async_ctx() -> AsyncIterator[Context]:
    """Async-friendly context with a pre-registered immediate async
    host function. Used by test_eval_async.py benchmarks that need
    to measure async pipeline overhead without the host function's
    own latency contributing (``instant`` returns immediately).

    Owns its own Runtime so async lifecycle doesn't interact with
    other benchmarks' module-scoped runtime.
    """
    runtime = Runtime()
    context = runtime.new_context()

    @context.function
    async def instant(x: int) -> int:
        return x

    try:
        yield context
    finally:
        context.close()
        runtime.close()
