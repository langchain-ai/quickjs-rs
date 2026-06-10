"""ES module loading via runtime import handlers."""

from __future__ import annotations

import pytest

from quickjs_rs import JSError, Runtime


def _handler_from_sources(sources: dict[str, str]):
    def handler(requested_key: str, _referrer: str | None, _specifier: str) -> str | None:
        return sources.get(requested_key)

    return handler


async def test_import_handler_loads_bare_specifier() -> None:
    calls: list[tuple[str, str | None, str]] = []

    def handler(requested_key: str, referrer: str | None, specifier: str) -> str | None:
        calls.append((requested_key, referrer, specifier))
        if requested_key == "@agent/backend":
            return "export const VALUE = 9;"
        return None

    with Runtime() as rt:
        rt.set_import_handler(handler)
        with rt.new_context() as ctx:
            await ctx.eval_async(
                """
                const mod = await import("@agent/backend");
                globalThis.r = mod.VALUE;
                """,
                module=True,
            )
            assert ctx.eval("r") == 9

    assert calls == [("@agent/backend", None, "@agent/backend")]


async def test_import_handler_loads_relative_dependency() -> None:
    calls: list[tuple[str, str | None, str]] = []

    def handler(requested_key: str, referrer: str | None, specifier: str) -> str | None:
        calls.append((requested_key, referrer, specifier))
        if requested_key == "@pkg/index.js":
            return 'import { helper } from "./helper.js"; export const out = helper();'
        if requested_key == "@pkg/helper.js":
            return "export function helper() { return 'ok'; }"
        return None

    with Runtime() as rt:
        rt.set_import_handler(handler)
        with rt.new_context() as ctx:
            await ctx.eval_async(
                """
                const mod = await import("@pkg/index.js");
                globalThis.r = mod.out;
                """,
                module=True,
            )
            assert ctx.eval("r") == "ok"

    assert calls == [
        ("@pkg/index.js", None, "@pkg/index.js"),
        ("@pkg/helper.js", "@pkg/index.js", "./helper.js"),
    ]


async def test_bare_import_is_passed_to_handler_once() -> None:
    calls: list[tuple[str, str | None, str]] = []

    def handler(requested_key: str, referrer: str | None, specifier: str) -> str | None:
        calls.append((requested_key, referrer, specifier))
        if requested_key == "@agent/backend":
            return "export const N = 42;"
        return None

    with Runtime() as rt:
        rt.set_import_handler(handler)
        with rt.new_context() as ctx:
            await ctx.eval_async(
                """
                const mod = await import("@agent/backend");
                globalThis.r = mod.N;
                """,
                module=True,
            )
            assert ctx.eval("r") == 42

    assert calls == [("@agent/backend", None, "@agent/backend")]


async def test_runtime_import_handler_is_shared_across_contexts() -> None:
    with Runtime() as rt:
        rt.set_import_handler(
            _handler_from_sources({"@runtime/backend": "export const VALUE = 17;"})
        )
        with rt.new_context() as ctx_a:
            with rt.new_context() as ctx_b:
                await ctx_a.eval_async(
                    """
                    const mod = await import("@runtime/backend");
                    globalThis.a = mod.VALUE;
                    """,
                    module=True,
                )
                await ctx_b.eval_async(
                    """
                    const mod = await import("@runtime/backend");
                    globalThis.b = mod.VALUE;
                    """,
                    module=True,
                )
                assert ctx_a.eval("a") == 17
                assert ctx_b.eval("b") == 17


async def test_context_import_handler_alias_updates_runtime() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx_a:
            with rt.new_context() as ctx_b:
                ctx_a.set_import_handler(
                    _handler_from_sources(
                        {"@ctx/backend": "export const VALUE = 'ctx-a';"}
                    )
                )
                await ctx_b.eval_async(
                    """
                    const mod = await import("@ctx/backend");
                    globalThis.b = mod.VALUE;
                    """,
                    module=True,
                )
                assert ctx_b.eval("b") == "ctx-a"


async def test_import_handler_can_be_cleared() -> None:
    with Runtime() as rt:
        rt.set_import_handler(
            _handler_from_sources({"@present": "export const VALUE = 1;"})
        )
        with rt.new_context() as ctx:
            await ctx.eval_async(
                """
                const mod = await import("@present");
                globalThis.present = mod.VALUE;
                """,
                module=True,
            )
            assert ctx.eval("present") == 1

            rt.set_import_handler(None)
            with pytest.raises(JSError):
                await ctx.eval_async('await import("@missing");', module=True)


async def test_import_handler_none_miss_fails_resolution() -> None:
    with Runtime() as rt:
        rt.set_import_handler(lambda *_args: None)
        with rt.new_context() as ctx:
            with pytest.raises(JSError):
                await ctx.eval_async('await import("@missing");', module=True)


async def test_import_handler_invalid_return_fails_resolution() -> None:
    def handler(_requested_key: str, _referrer: str | None, _specifier: str) -> object:
        return {"not": "source"}

    with Runtime() as rt:
        rt.set_import_handler(handler)  # type: ignore[arg-type]
        with rt.new_context() as ctx:
            with pytest.raises(JSError, match="import handler must return str"):
                await ctx.eval_async('await import("@bad");', module=True)


async def test_dynamic_import_is_cached() -> None:
    calls = 0

    def handler(requested_key: str, _referrer: str | None, _specifier: str) -> str | None:
        nonlocal calls
        if requested_key == "@stateful":
            calls += 1
            return f"export const N = {calls};"
        return None

    with Runtime() as rt:
        rt.set_import_handler(handler)
        with rt.new_context() as ctx:
            await ctx.eval_async(
                """
                const a = await import("@stateful");
                const b = await import("@stateful");
                globalThis.same = a === b;
                globalThis.values = [a.N, b.N];
                """,
                module=True,
            )
            assert ctx.eval("same") is True
            assert ctx.eval("values") == [1, 1]

    assert calls == 1


async def test_typescript_source_is_stripped_for_dynamic_handler() -> None:
    with Runtime() as rt:
        rt.set_import_handler(
            _handler_from_sources(
                {
                    "@util/index.ts": """
                        export enum Mode { Strict = 1, Loose = 2 }
                        export function slug(s: string, mode: Mode): string {
                            return s.toLowerCase().replace(/ /g, mode === Mode.Strict ? '_' : '-');
                        }
                    """,
                }
            )
        )
        with rt.new_context() as ctx:
            await ctx.eval_async(
                """
                const mod = await import("@util/index.ts");
                globalThis.slug = mod.slug("Hello World", mod.Mode.Strict);
                """,
                module=True,
            )
            assert ctx.eval("slug") == "hello_world"


async def test_relative_import_from_eval_cannot_escape_root() -> None:
    with Runtime() as rt:
        rt.set_import_handler(lambda *_args: "export const V = 1;")
        with rt.new_context() as ctx:
            with pytest.raises(JSError):
                await ctx.eval_async('import "../outside.js";', module=True)


def test_set_import_handler_rejects_non_callable() -> None:
    with Runtime() as rt:
        with pytest.raises(TypeError, match="handler must be callable or None"):
            rt.set_import_handler("not callable")  # type: ignore[arg-type]
