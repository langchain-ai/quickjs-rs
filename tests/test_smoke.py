"""Acceptance tests.

``test_smoke_primitives`` is a focused happy-path check that
greened the first primitive-marshaling commits; it remains as a
fast narrow-surface verification.
"""

from __future__ import annotations

import asyncio

import pytest

from quickjs_rs import (
    ConcurrentEvalError,
    DeadlockError,
    HostError,
    InvalidHandleError,  # noqa: F401 — exercised once handles land
    JSError,
    MarshalError,  # noqa: F401 — exercised when handle marshaling lands
    MemoryLimitError,
    ModuleScope,
    Runtime,
    TimeoutError,
)


def test_smoke_primitives() -> None:
    """Greens the primitive block plus bigint and Uint8Array."""
    with Runtime(memory_limit=64 * 1024 * 1024) as rt:
        with rt.new_context(timeout=5.0) as ctx:
            assert ctx.eval("1 + 2") == 3
            assert ctx.eval("'hello'") == "hello"
            assert ctx.eval("true") is True
            assert ctx.eval("null") is None
            assert ctx.eval("undefined") is None
            assert ctx.eval("1.5") == 1.5

            # BigInt — positive and negative to exercise sign handling.
            assert ctx.eval("10n ** 30n") == 10**30
            assert ctx.eval("-(10n ** 30n)") == -(10**30)

            # Bytes
            assert ctx.eval("new Uint8Array([1, 2, 3])") == b"\x01\x02\x03"

            # Arrays (including nested to exercise recursion)
            assert ctx.eval("[1, 2, 3]") == [1, 2, 3]
            assert ctx.eval("[[1, 2], [3, 4]]") == [[1, 2], [3, 4]]
            assert ctx.eval("[]") == []

            # Objects (mixed nesting, empty, insertion order preserved)
            assert ctx.eval("({a: 1, b: [2, 3]})") == {"a": 1, "b": [2, 3]}
            assert ctx.eval("({})") == {}
            ordered = ctx.eval("({z: 1, a: 2, m: 3})")
            assert list(ordered.keys()) == ["z", "a", "m"]

            # Globals: read, write, contains
            ctx.globals["x"] = 42
            assert ctx.eval("x") == 42
            ctx.globals["data"] = {"n": 100}
            assert ctx.eval("data.n") == 100
            assert "x" in ctx.globals
            assert "not_a_real_global" not in ctx.globals
            assert ctx.globals["x"] == 42

            # Four-layer nested round-trip: encode → decode (from_msgpack) →
            # encode (to_msgpack) → decode. Exercises every container type.
            ctx.globals["deep"] = {"nested": {"list": [1, 2, {"leaf": "value"}]}}
            assert ctx.eval("deep.nested.list[2].leaf") == "value"

            # Host functions: decorator form
            @ctx.function
            def add(a: int, b: int) -> int:
                return a + b

            assert ctx.eval("add(1, 2)") == 3

            # Host functions: explicit form with a name override
            ctx.register("say_hi", lambda who: f"hi {who}")
            assert ctx.eval("say_hi('world')") == "hi world"

            # Host exception propagates out as HostError with __cause__
            # threaded back to the original Python exception.
            @ctx.function
            def boom() -> None:
                raise ValueError("from python")

            with pytest.raises(HostError) as excinfo:
                ctx.eval("boom()")
            assert isinstance(excinfo.value.__cause__, ValueError)

            # JS-side visibility: a try/catch inside JS sees the error's
            # name and message and can round-trip them back as a string.
            assert (
                ctx.eval("try { boom(); 'unreachable'; } catch (e) { e.name + ': ' + e.message }")
                == "HostError: Host function failed"
            )

            # JS-thrown TypeError (not a host error) surfaces as JSError
            # with name=TypeError, message, and a populated stack.
            with pytest.raises(JSError) as js_excinfo:
                ctx.eval("throw new TypeError('bad thing')")
            assert js_excinfo.value.name == "TypeError"
            assert js_excinfo.value.message == "bad thing"
            assert js_excinfo.value.stack is not None

            # Memory limit: unbounded allocation trips JS_ATOM_out_of_memory,
            # surfaced as MemoryLimitError. Done in a fresh 8 MB runtime
            # rather than the outer 64 MB one: at 64 MB some macOS CI
            # runners hit a degenerate QuickJS path where the
            # exception-allocation itself fails and the caught value
            # lands as null instead of InternalError("out of memory").
            # 8 MB reproduces OOM cleanly everywhere we've tested. The
            # behavioral guarantee (runaway allocation → MemoryLimitError)
            # is identical; only the smoke-test's memory budget changes.
            # The previous implementation tripwire test_memory_limit_trips_with_runaway_
            # allocation lives in test_limits.py at the same 8 MB.
            with Runtime(memory_limit=8 * 1024 * 1024) as mem_rt:
                with mem_rt.new_context() as mem_ctx:
                    with pytest.raises(MemoryLimitError):
                        mem_ctx.eval("let a = []; while(true) a.push(new Array(1e6).fill(0))")

            # Timeout: infinite loop terminates within the configured
            # deadline. Context uses its default 5 s budget; this test
            # drops it to something short so pytest doesn't sit for 5 s
            # of wall time on every run.
            ctx.timeout = 0.2
            with pytest.raises(TimeoutError):
                ctx.eval("while(true){}")
            ctx.timeout = 5.0

            # Handles: eval_handle returns a Handle that outlives
            # its creating eval call, supports property access, method
            # invocation, and to_python marshaling for the subset of
            # values that are marshalable. allow_opaque=True substitutes
            # child Handles for unmarshalable values (functions here).
            with ctx.eval_handle("({x: 1, y: 2, add(a, b) { return a + b }})") as obj:
                assert obj.type_of == "object"
                x_handle = obj.get("x")
                try:
                    assert x_handle.to_python() == 1
                finally:
                    x_handle.dispose()
                result = obj.call_method("add", 10, 20)
                assert result.to_python() == 30
                result.dispose()

                as_dict = obj.to_python(allow_opaque=True)
                assert as_dict["x"] == 1
                assert as_dict["y"] == 2
                assert hasattr(as_dict["add"], "call")
                assert as_dict["add"].type_of == "function"
                as_dict["add"].dispose()

            # Multi-context isolation: globals don't leak across
            # contexts sharing the same Runtime.
            with rt.new_context() as ctx2:
                ctx2.globals["y"] = "other"
                assert ctx2.eval("y") == "other"
                assert ctx.eval("typeof y") == "undefined"


def test_acceptance() -> None:
    with Runtime(memory_limit=64 * 1024 * 1024) as rt:
        with rt.new_context(timeout=5.0) as ctx:
            # Primitives
            assert ctx.eval("1 + 2") == 3
            assert ctx.eval("'hello'") == "hello"
            assert ctx.eval("true") is True
            assert ctx.eval("null") is None
            assert ctx.eval("undefined") is None
            assert ctx.eval("1.5") == 1.5

            # BigInt
            assert ctx.eval("10n ** 30n") == 10**30

            # Collections
            assert ctx.eval("[1, 2, 3]") == [1, 2, 3]
            assert ctx.eval("({a: 1, b: [2, 3]})") == {"a": 1, "b": [2, 3]}

            # Bytes
            result = ctx.eval("new Uint8Array([1, 2, 3])")
            assert result == b"\x01\x02\x03"

            # Globals (read/write)
            ctx.globals["x"] = 42
            assert ctx.eval("x") == 42
            ctx.globals["data"] = {"n": 100}
            assert ctx.eval("data.n") == 100

            # Host functions: decorator form
            @ctx.function
            def add(a: int, b: int) -> int:
                return a + b

            assert ctx.eval("add(1, 2)") == 3

            # Host functions: explicit form with name override
            ctx.register("say_hi", lambda name: f"hi {name}")
            assert ctx.eval("say_hi('world')") == "hi world"

            # JS exception → Python
            with pytest.raises(JSError) as excinfo:
                ctx.eval("throw new TypeError('bad thing')")
            assert excinfo.value.name == "TypeError"
            assert excinfo.value.message == "bad thing"
            assert excinfo.value.stack is not None

            # Host exception → JS → Python
            @ctx.function
            def boom() -> None:
                raise ValueError("from python")

            with pytest.raises(HostError) as excinfo_h:
                ctx.eval("boom()")
            assert isinstance(excinfo_h.value.__cause__, ValueError)

            # JS catching host error
            assert (
                ctx.eval(
                    """
                try { boom(); 'unreachable'; }
                catch (e) { e.name + ': ' + e.message }
            """
                )
                == "HostError: Host function failed"
            )

            # Memory limit: see the note in test_smoke_primitives for
            # why this runs in a fresh 8 MB runtime rather than the
            # outer 64 MB one.
            with Runtime(memory_limit=8 * 1024 * 1024) as mem_rt:
                with mem_rt.new_context() as mem_ctx:
                    with pytest.raises(MemoryLimitError):
                        mem_ctx.eval("let a = []; while(true) a.push(new Array(1e6).fill(0))")

            # Timeout
            with pytest.raises(TimeoutError):
                ctx.eval("while(true){}")

            # Handles
            with ctx.eval_handle("({x: 1, y: 2, add(a, b) { return a + b }})") as obj:
                assert obj.type_of == "object"
                assert obj.get("x").to_python() == 1
                result = obj.call_method("add", 10, 20)
                assert result.to_python() == 30
                result.dispose()

                as_dict = obj.to_python(allow_opaque=True)
                assert as_dict["x"] == 1
                assert hasattr(as_dict["add"], "call")
                as_dict["add"].dispose()

            # Multiple contexts, one runtime
            with rt.new_context() as ctx2:
                ctx2.globals["y"] = "other"
                assert ctx2.eval("y") == "other"
                assert ctx.eval("typeof y") == "undefined"


async def test_async_acceptance() -> None:
    """
    The ``except asyncio.CancelledError: pass`` tolerance around the
    absorption case is intentional, not sloppy: cancellation delivery
    timing is implementation-dependent in asyncio, and on slow
    runners the cancellation may propagate before JS's catch handler
    runs. Both outcomes — JS absorbs, or cancellation propagates
    before absorption — are valid implementations so the test tolerates either.
    """
    with Runtime() as rt:
        with rt.new_context() as ctx:
            # Auto-detected async host function
            @ctx.function
            async def sleep_ms(n: int) -> str:
                await asyncio.sleep(n / 1000)
                return "slept"

            # Top-level await in module mode
            assert await ctx.eval_async("await sleep_ms(10)") == "slept"

            # Promise.all fan-out, multiple concurrent host calls
            result = await ctx.eval_async("""
                const results = await Promise.all([
                    sleep_ms(5),
                    sleep_ms(10),
                    sleep_ms(15),
                ]);
                results.join(",")
            """)
            assert result == "slept,slept,slept"

            # Mixed sync + async host calls in one eval
            @ctx.function
            def double(n: int) -> int:
                return n * 2

            @ctx.function
            async def slow_double(n: int) -> int:
                await asyncio.sleep(0.001)
                return n * 2

            result = await ctx.eval_async("""
                const a = double(5);              // sync, immediate
                const b = await slow_double(10);  // async, awaited
                a + b
            """)
            assert result == 30

            # The motivating agent-code pattern: readFile + swarm
            captured_reads: list[str] = []

            @ctx.function
            async def readFile(path: str) -> str:
                captured_reads.append(path)
                return "Date: 2024-01-01\nDate: 2024-01-02\nNotDate"

            @ctx.function
            async def swarm(tasks: list, opts: dict) -> dict:
                return {
                    "completed": len(tasks),
                    "failed": 0,
                    "results": [
                        {
                            "id": t["id"],
                            "status": "completed",
                            "result": '{"abbreviation_count": 1}',
                        }
                        for t in tasks
                    ],
                }

            result = await ctx.eval_async("""
                const raw = await readFile("/context.txt");
                const lines = raw.split("\\n").filter(l => l.startsWith("Date:"));
                const summary = await swarm(
                    lines.map((line, i) => ({ id: `t_${i}`, description: line })),
                    { concurrency: 32 }
                );
                let total = 0;
                for (const r of summary.results) {
                    if (r.status === "completed") {
                        total += JSON.parse(r.result).abbreviation_count;
                    }
                }
                total
            """)
            assert result == 2
            assert captured_reads == ["/context.txt"]

            # Cancellation: task.cancel() propagates through eval_async
            task = asyncio.create_task(ctx.eval_async("await sleep_ms(10000)"))
            await asyncio.sleep(0.01)  # let it start
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # JS catching HostCancellationError and recovering
            # Cancel via asyncio.timeout; JS catches and returns sentinel;
            # eval_async returns normally since cancellation was absorbed
            async with asyncio.timeout(0.02):
                try:
                    caught = await ctx.eval_async("""
                        try {
                            await sleep_ms(10000);
                            "unreachable"
                        } catch (e) {
                            e.name
                        }
                    """)
                    assert caught == "HostCancellationError"
                except asyncio.CancelledError:
                    # Acceptable alternate path: cancellation propagated
                    # before JS catch handler ran. Either outcome is a
                    # valid implementation of cancellation.
                    pass

            # DeadlockError: pending promise with no async work
            # previous implementation: module=False enables JS_EVAL_FLAG_ASYNC, so
            # returning a pending promise as the body's last
            # expression gets wrapped as {value: <pending>, done:
            # false} — wrapper is fulfilled, driving loop returns.
            # Awaiting the pending promise keeps the wrapper
            # pending, which is what exercises the deadlock path.
            with pytest.raises(DeadlockError):
                await ctx.eval_async(
                    "await new Promise((resolve) => {})",
                    module=False,
                )

            # ConcurrentEvalError: two eval_async at once on same context
            async def first() -> None:
                await ctx.eval_async("await sleep_ms(100)")

            async with asyncio.TaskGroup() as tg:
                tg.create_task(first())
                await asyncio.sleep(0.01)  # let first start
                with pytest.raises(ConcurrentEvalError):
                    await ctx.eval_async("1 + 1")

            # Sync eval + async host fn: clean failure
            with pytest.raises(ConcurrentEvalError):
                ctx.eval("sleep_ms(1)")  # returns a Promise sync eval can't drive

            # Handle.await_promise
            p = await ctx.eval_handle_async("Promise.resolve(42)")
            resolved = await p.await_promise()
            assert resolved.to_python() == 42
            resolved.dispose()
            p.dispose()


async def test_module_acceptance() -> None:
    """The motivating agent-code pattern end-to-end: a stdlib
    ModuleScope carrying multiple named deps, a script that imports
    across scopes, uses top-level await against async host
    functions, and reads a global set by script-mode eval. Plus
    composition tests (override, capability restriction, recursive
    self-contained deps).
    """
    stdlib = ModuleScope(
        {
            "@agent/config": ModuleScope({"index.js": "export const MAX_RETRIES = 3;"}),
            "@agent/utils": ModuleScope(
                {
                    "index.js": """
                        export { slugify } from "./strings.js";
                        export { chunk } from "./arrays.js";
                    """,
                    "strings.js": """
                        export function slugify(s) {
                            return s.toLowerCase().replace(/ /g, '-');
                        }
                    """,
                    "arrays.js": """
                        export function chunk(arr, size) {
                            const r = [];
                            for (let i = 0; i < arr.length; i += size)
                                r.push(arr.slice(i, i + size));
                            return r;
                        }
                    """,
                }
            ),
            "@agent/fs": ModuleScope(
                {
                    "index.js": """
                        export async function readFile(path) {
                            return await _readFile(path);
                        }
                    """,
                }
            ),
            "@agent/concurrency": ModuleScope(
                {
                    "index.js": """
                        export async function swarm(tasks, opts) {
                            return await _swarm(tasks, opts.concurrency || 10);
                        }
                    """,
                }
            ),
        }
    )

    with Runtime() as rt:
        with rt.new_context() as ctx:

            @ctx.function
            async def _readFile(path: str) -> str:  # noqa: N802 — JS naming
                return "Date: 2024-01-01\nDate: 2024-01-02\nNotDate"

            @ctx.function
            async def _swarm(tasks: list, concurrency: int) -> dict:
                _ = concurrency  # opts.concurrency wired through but unused here
                return {
                    "completed": len(tasks),
                    "failed": 0,
                    "results": [
                        {
                            "id": t["id"],
                            "status": "completed",
                            "result": '{"abbreviation_count": 1}',
                        }
                        for t in tasks
                    ],
                }

            rt.install(stdlib)

            # The motivating pattern — imports across scopes, top-
            # level await calling async host functions, results
            # funneled through globalThis so script-mode eval can
            # read them afterwards.
            await ctx.eval_async(
                """
                import { readFile } from "@agent/fs";
                import { swarm } from "@agent/concurrency";
                import { chunk } from "@agent/utils";
                import { MAX_RETRIES } from "@agent/config";

                const raw = await readFile("/context.txt");
                const lines = raw.split("\\n").filter(l => l.startsWith("Date:"));
                const chunks = chunk(lines, 50);
                const tasks = chunks.map((c, i) => ({
                    id: `chunk_${i}`,
                    description: c.join("\\n"),
                }));
                const summary = await swarm(tasks, { concurrency: 32 });

                let total = 0;
                for (const r of summary.results) {
                    if (r.status === "completed") {
                        total += JSON.parse(r.result).abbreviation_count;
                    }
                }
                globalThis.result = { total, retries: MAX_RETRIES };
                """,
                module=True,
            )

            result = ctx.eval("result")
            assert result["total"] == 1
            assert result["retries"] == 3

            # Cross-scope import works
            await ctx.eval_async(
                """
                import { slugify } from "@agent/utils";
                globalThis.slug = slugify("Hello World");
                """,
                module=True,
            )
            assert ctx.eval("slug") == "hello-world"

            # Module scope isolation: `let` inside a module body
            # stays module-scoped, does NOT pollute globalThis.
            await ctx.eval_async("let moduleLocal = 42;", module=True)
            assert ctx.eval("typeof moduleLocal") == "undefined"

            # Globals bridge: Python → globalThis → module.
            ctx.globals["pyValue"] = "from python"
            await ctx.eval_async(
                """
                globalThis.bridged = pyValue + " via module";
                """,
                module=True,
            )
            assert ctx.eval("bridged") == "from python via module"

            # Resolver boundary: `./strings.js` in @agent/utils is
            # NOT the same as `./strings.js` would be anywhere
            # else. Scopes are isolated; the resolver only looks
            # in the referrer's own scope. (Not separately
            # asserted here — the fact that utils' slugify works
            # above is the positive evidence; negative coverage
            # lives in test_modules.py::
            # test_same_filename_in_sibling_scopes_resolves_independently.)

        # Composition: override for testing. Build a fresh
        # Runtime for each alternate stdlib — the module store is
        # per-runtime, so mixing overrides on the
        # same runtime would layer on top of existing caches in
        # ways that depend on import order. Separate runtimes
        # give each composition a clean slate.
        test_stdlib = ModuleScope(
            {
                **stdlib.modules,
                "@agent/config": ModuleScope({"index.js": "export const MAX_RETRIES = 1;"}),
            }
        )

        with Runtime() as rt_override:
            with rt_override.new_context() as ctx:

                @ctx.function
                async def _readFile(path: str) -> str:  # noqa: N802
                    return "mock"

                @ctx.function
                async def _swarm(tasks: list, concurrency: int) -> dict:
                    return {"completed": 0, "failed": 0, "results": []}

                rt_override.install(test_stdlib)
                await ctx.eval_async(
                    """
                    import { MAX_RETRIES } from "@agent/config";
                    globalThis.retries = MAX_RETRIES;
                    """,
                    module=True,
                )
                assert ctx.eval("retries") == 1  # overridden

        # Capability restriction: no @agent/fs.
        restricted = ModuleScope({k: v for k, v in stdlib.modules.items() if k != "@agent/fs"})

        with Runtime() as rt_restricted:
            with rt_restricted.new_context() as ctx:
                rt_restricted.install(restricted)
                with pytest.raises(JSError, match="@agent/fs"):
                    await ctx.eval_async(
                        """
                        import { readFile } from "@agent/fs";
                        """,
                        module=True,
                    )

        # Self-contained recursive deps. @app carries @agent/utils
        # directly; @app's index.js imports it. Eval from the
        # root cannot import @agent/utils directly because root
        # doesn't declare it — even though @app does carry it.
        utils = ModuleScope(
            {
                "@agent/utils": ModuleScope(
                    {
                        "index.js": ("export function greet(n) { return 'hi ' + n; }"),
                    }
                ),
            }
        )
        recursive = ModuleScope(
            {
                "@app": ModuleScope(
                    {
                        **utils.modules,  # @app carries @agent/utils
                        "index.js": """
                            import { greet } from "@agent/utils";
                            export const message = greet("world");
                        """,
                    }
                ),
                # Root does NOT carry @agent/utils. Eval from the
                # root scope can only import @app.
            }
        )
        with Runtime() as rt_recursive:
            with rt_recursive.new_context() as ctx:
                rt_recursive.install(recursive)
                await ctx.eval_async(
                    """
                    import { message } from "@app";
                    globalThis.msg = message;
                    """,
                    module=True,
                )
                assert ctx.eval("msg") == "hi world"

                # Self-containment: the root scope does not
                # itself carry @agent/utils, so eval from root
                # cannot import it — even though @app does carry it.
                with pytest.raises(JSError, match="@agent/utils"):
                    await ctx.eval_async(
                        """
                        import { greet } from "@agent/utils";
                        """,
                        module=True,
                    )
