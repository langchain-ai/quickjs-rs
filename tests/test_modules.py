"""ES module resolution via a host loader callback pair.

Module *capability* (``eval(module=True)``, ``import``/``export``) is intact;
the host supplies modules through ``rt.set_module_loader(normalize=, load=)``
rather than the old ``ModuleScope`` registry. The host owns ALL resolution
policy in ``normalize`` — there is no built-in scope tree / sandbox; relative
path joining, aliasing, and any sandboxing live in the host callback.
"""

from __future__ import annotations

import posixpath

import pytest

from quickjs_rs import JSError, Runtime, SourceTransform, default_module_transform_flags


def _flat_loader(sources: dict[str, str]):
    """A simple host loader: bare specifiers map by name; relative specifiers
    (./ , ../) are joined against the importer's directory (posix)."""

    def normalize(base: str, spec: str) -> str | None:
        if not spec.startswith("."):
            return spec
        return posixpath.normpath(posixpath.join(posixpath.dirname(base), spec))

    def load(name: str) -> str | None:
        return sources.get(name)

    return normalize, load


def _eval_module(ctx, code: str):
    """eval a module and return its namespace handle (caller disposes)."""
    return ctx.eval_handle(code, module=True)


# --- basic resolution -------------------------------------------------------


def test_bare_specifier_resolves() -> None:
    normalize, load = _flat_loader({"math": "export const add = (a, b) => a + b;"})
    with Runtime() as rt:
        rt.set_module_loader(normalize=normalize, load=load)
        with rt.new_context() as ctx:
            with _eval_module(ctx, "import { add } from 'math'; export const r = add(3, 4);") as ns:
                r = ns.get("r")
                try:
                    assert r.to_python() == 7
                finally:
                    r.dispose()


def test_relative_specifier_resolves_via_host_normalize() -> None:
    normalize, load = _flat_loader(
        {
            "pkg/main": "import { greet } from './util'; export const msg = greet('world');",
            "pkg/util": "export const greet = (n) => `hi ${n}`;",
        }
    )
    with Runtime() as rt:
        rt.set_module_loader(normalize=normalize, load=load)
        with rt.new_context() as ctx:
            with _eval_module(ctx, "import { msg } from 'pkg/main'; export const out = msg;") as ns:
                out = ns.get("out")
                try:
                    assert out.to_python() == "hi world"
                finally:
                    out.dispose()


def test_re_exports_resolve() -> None:
    normalize, load = _flat_loader(
        {
            "base": "export const x = 10;",
            "mid": "export { x } from 'base'; export const y = 20;",
        }
    )
    with Runtime() as rt:
        rt.set_module_loader(normalize=normalize, load=load)
        with rt.new_context() as ctx:
            with _eval_module(ctx, "import { x, y } from 'mid'; export const sum = x + y;") as ns:
                s = ns.get("sum")
                try:
                    assert s.to_python() == 30
                finally:
                    s.dispose()


def test_default_normalize_is_identity() -> None:
    """With no normalize callback, the specifier passes through unchanged, so a
    bare name maps straight to a load() key."""
    with Runtime() as rt:
        rt.set_module_loader(load=lambda name: {"only": "export const v = 99;"}.get(name))
        with rt.new_context() as ctx:
            with _eval_module(ctx, "import { v } from 'only'; export const r = v;") as ns:
                r = ns.get("r")
                try:
                    assert r.to_python() == 99
                finally:
                    r.dispose()


def test_namespace_exposes_multiple_exports() -> None:
    normalize, load = _flat_loader({"consts": "export const A = 1; export const B = 2;"})
    with Runtime() as rt:
        rt.set_module_loader(normalize=normalize, load=load)
        with rt.new_context() as ctx:
            with _eval_module(ctx, "export { A, B } from 'consts'; export const C = 3;") as ns:
                a, b, c = ns.get("A"), ns.get("B"), ns.get("C")
                try:
                    assert a.to_python() == 1
                    assert b.to_python() == 2
                    assert c.to_python() == 3
                finally:
                    a.dispose()
                    b.dispose()
                    c.dispose()


# --- resolution policy is the host's: normalize can sandbox -----------------


def test_host_normalize_can_reject_to_sandbox() -> None:
    """Sandboxing is the host's job in normalize() — e.g. refuse a specifier
    that escapes a prefix. The engine imposes no scope model."""
    sources = {
        "app/main": "import { x } from '../secret'; export const r = x;",
        "secret": "export const x = 'leaked';",
    }

    def normalize(base: str, spec: str) -> str | None:
        if not spec.startswith("."):
            return spec
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(base), spec))
        # Host policy: a module may not resolve outside its own top dir.
        top = base.split("/", 1)[0]
        if not joined.startswith(top + "/") and joined != top:
            return None  # refuse — escapes the sandbox
        return joined

    with Runtime() as rt:
        rt.set_module_loader(normalize=normalize, load=lambda n: sources.get(n))
        with rt.new_context() as ctx:
            with pytest.raises(JSError):
                ctx.eval("import { r } from 'app/main'; r", module=True)


# --- failure modes ----------------------------------------------------------


def test_unknown_module_raises() -> None:
    normalize, load = _flat_loader({})  # nothing registered
    with Runtime() as rt:
        rt.set_module_loader(normalize=normalize, load=load)
        with rt.new_context() as ctx:
            with pytest.raises(JSError):
                ctx.eval("import { x } from 'no_such_module'; x", module=True)


def test_normalize_returning_none_raises() -> None:
    with Runtime() as rt:
        rt.set_module_loader(
            normalize=lambda base, spec: None,  # unresolvable
            load=lambda name: "export const v = 1;",
        )
        with rt.new_context() as ctx:
            with pytest.raises(JSError):
                ctx.eval("import { v } from 'whatever'; v", module=True)


def test_no_loader_set_makes_imports_fail() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with pytest.raises(JSError):
                ctx.eval("import { x } from 'anything'; x", module=True)


# --- at-most-once-per-edge resolution (the guest's per-context cache) --------


def test_normalize_called_at_most_once_per_edge() -> None:
    calls: list[tuple[str, str]] = []
    sources = {"lib": "export const v = 42;"}

    def normalize(base: str, spec: str) -> str | None:
        calls.append((base, spec))
        return spec if not spec.startswith(".") else None

    with Runtime() as rt:
        rt.set_module_loader(normalize=normalize, load=lambda n: sources.get(n))
        with rt.new_context() as ctx:
            with _eval_module(ctx, "import { v } from 'lib'; export const r = v;") as ns:
                r = ns.get("r")
                try:
                    assert r.to_python() == 42
                finally:
                    r.dispose()
    lib_edges = [c for c in calls if c[1] == "lib"]
    assert len(lib_edges) == 1, lib_edges


# --- shared across contexts on one runtime ----------------------------------


def test_loader_shared_across_contexts() -> None:
    normalize, load = _flat_loader({"shared": "export const v = 7;"})
    with Runtime() as rt:
        rt.set_module_loader(normalize=normalize, load=load)
        with rt.new_context() as ctx1, rt.new_context() as ctx2:
            for ctx in (ctx1, ctx2):
                with _eval_module(ctx, "import { v } from 'shared'; export const r = v;") as ns:
                    r = ns.get("r")
                    try:
                        assert r.to_python() == 7
                    finally:
                        r.dispose()


# --- TypeScript type-stripping (host transform artifact, via OXC) -----------


def test_ts_module_is_type_stripped() -> None:
    """A `.ts` module is type-stripped before QuickJS sees it; type
    annotations that would be a SyntaxError in plain JS erase away."""
    normalize, load = _flat_loader(
        {"math.ts": "export const add = (a: number, b: number): number => a + b;"}
    )
    with Runtime() as rt:
        rt.set_module_loader(normalize=normalize, load=load)
        with rt.new_context() as ctx:
            with _eval_module(
                ctx, "import { add } from 'math.ts'; export const r = add(3, 4);"
            ) as ns:
                r = ns.get("r")
                try:
                    assert r.to_python() == 7
                finally:
                    r.dispose()


def test_ts_interfaces_and_type_aliases_erase() -> None:
    normalize, load = _flat_loader(
        {
            "model.ts": (
                "interface User { name: string; age: number }\n"
                "type Id = string | number;\n"
                "export function label(u: User, id: Id): string {\n"
                "  return `${u.name}#${id}`;\n"
                "}\n"
            )
        }
    )
    with Runtime() as rt:
        rt.set_module_loader(normalize=normalize, load=load)
        with rt.new_context() as ctx:
            with _eval_module(
                ctx,
                "import { label } from 'model.ts';export const r = label({name: 'a', age: 1}, 7);",
            ) as ns:
                r = ns.get("r")
                try:
                    assert r.to_python() == "a#7"
                finally:
                    r.dispose()


def test_ts_enum_is_transformed() -> None:
    """Enums aren't pure erasure; OXC transforms them to runtime objects."""
    normalize, load = _flat_loader(
        {"colors.ts": "export enum Color { Red, Green, Blue } export const g = Color.Green;"}
    )
    with Runtime() as rt:
        rt.set_module_loader(normalize=normalize, load=load)
        with rt.new_context() as ctx:
            with _eval_module(ctx, "import { g } from 'colors.ts'; export const r = g;") as ns:
                r = ns.get("r")
                try:
                    assert r.to_python() == 1  # Green == 1
                finally:
                    r.dispose()


def test_ts_namespace_is_transformed() -> None:
    normalize, load = _flat_loader(
        {"names.ts": "export namespace Names { export const answer = 42; }"}
    )
    with Runtime() as rt:
        rt.set_module_loader(normalize=normalize, load=load)
        with rt.new_context() as ctx:
            with _eval_module(
                ctx, "import { Names } from 'names.ts'; export const r = Names.answer;"
            ) as ns:
                r = ns.get("r")
                try:
                    assert r.to_python() == 42
                finally:
                    r.dispose()


def test_ts_parameter_property_is_transformed() -> None:
    normalize, load = _flat_loader(
        {
            "box.ts": (
                "export class Box { constructor(public value: number) {} } "
                "export const v = new Box(9).value;"
            )
        }
    )
    with Runtime() as rt:
        rt.set_module_loader(normalize=normalize, load=load)
        with rt.new_context() as ctx:
            with _eval_module(ctx, "import { v } from 'box.ts'; export const r = v;") as ns:
                r = ns.get("r")
                try:
                    assert r.to_python() == 9
                finally:
                    r.dispose()


def test_tsx_module_is_stripped() -> None:
    """A `.tsx` module strips TS type annotations on the .tsx path. (We avoid
    JSX elements — they'd need a runtime — and the `<T>` arrow-generic, which is
    genuinely ambiguous with JSX in .tsx and must be written `<T,>`.)"""
    normalize, load = _flat_loader(
        {
            "util.tsx": "export function pick(o: {n: number}): number { return o.n; } "
            "export const v = pick({n: 42});"
        }
    )
    with Runtime() as rt:
        rt.set_module_loader(normalize=normalize, load=load)
        with rt.new_context() as ctx:
            with _eval_module(ctx, "import { v } from 'util.tsx'; export const r = v;") as ns:
                r = ns.get("r")
                try:
                    assert r.to_python() == 42
                finally:
                    r.dispose()


def test_js_module_is_not_stripped() -> None:
    """A `.js` (or extensionless) source passes through unchanged — TS syntax in
    a non-TS module is a real SyntaxError, not silently stripped."""
    normalize, load = _flat_loader(
        {"bad.js": "export const add = (a: number) => a;"}  # `: number` is invalid JS
    )
    with Runtime() as rt:
        rt.set_module_loader(normalize=normalize, load=load)
        with rt.new_context() as ctx:
            with pytest.raises(JSError):
                ctx.eval("import { add } from 'bad.js'; add", module=True)


def test_module_loader_can_apply_public_transform_flags_to_any_name() -> None:
    """The host can explicitly choose TypeScript parsing independent of name."""
    normalize, load = _flat_loader(
        {"typed.js": "export const add = (a: number, b: number): number => a + b;"}
    )
    with Runtime() as rt:
        rt.set_module_loader(
            normalize=normalize,
            load=load,
            transform_flags=SourceTransform.SOURCE_TS | SourceTransform.STRIP_TYPESCRIPT,
        )
        with rt.new_context() as ctx:
            with _eval_module(
                ctx, "import { add } from 'typed.js'; export const r = add(3, 4);"
            ) as ns:
                r = ns.get("r")
                try:
                    assert r.to_python() == 7
                finally:
                    r.dispose()


def test_module_loader_can_disable_default_transform_policy() -> None:
    normalize, load = _flat_loader({"typed.ts": "export const value: number = 5;"})
    with Runtime() as rt:
        rt.set_module_loader(
            normalize=normalize,
            load=load,
            transform_flags=SourceTransform.NONE,
        )
        with rt.new_context() as ctx:
            with pytest.raises(JSError):
                ctx.eval("import { value } from 'typed.ts'; value", module=True)


def test_module_loader_inherits_runtime_transform_policy() -> None:
    normalize, load = _flat_loader({"typed.js": "export const value: number = 16;"})
    with Runtime(
        transform_flags=SourceTransform.SOURCE_TS | SourceTransform.STRIP_TYPESCRIPT
    ) as rt:
        rt.set_module_loader(normalize=normalize, load=load)
        with rt.new_context() as ctx:
            with _eval_module(
                ctx, "import { value } from 'typed.js'; export const r = value;"
            ) as ns:
                r = ns.get("r")
                try:
                    assert r.to_python() == 16
                finally:
                    r.dispose()


def test_module_loader_transform_flags_override_runtime_policy() -> None:
    normalize, load = _flat_loader({"typed.ts": "export const value: number = 17;"})
    with Runtime(
        transform_flags=SourceTransform.SOURCE_TS | SourceTransform.STRIP_TYPESCRIPT
    ) as rt:
        rt.set_module_loader(
            normalize=normalize,
            load=load,
            transform_flags=SourceTransform.NONE,
        )
        with rt.new_context() as ctx:
            with pytest.raises(JSError):
                ctx.eval("import { value } from 'typed.ts'; value", module=True)


def test_module_loader_transform_flags_callback_can_extend_defaults(monkeypatch) -> None:
    seen_flags: list[int] = []

    from quickjs_rs._transform import SourceTransformer

    original_transform = SourceTransformer.transform

    def record_transform(
        self: SourceTransformer,
        name: str,
        source: str,
        *,
        flags: int | None = None,
    ) -> str:
        if name == "model.ts":
            assert flags is not None
            seen_flags.append(flags)
        return original_transform(self, name, source, flags=flags)

    monkeypatch.setattr("quickjs_rs._engine.SourceTransformer.transform", record_transform)

    normalize, load = _flat_loader({"model.ts": "export const value: number = 5;"})

    def flags_for(name: str) -> SourceTransform:
        return default_module_transform_flags(name) | SourceTransform.TOP_LEVEL_CONST_TO_VAR

    with Runtime() as rt:
        rt.set_module_loader(normalize=normalize, load=load, transform_flags=flags_for)
        with rt.new_context() as ctx:
            with _eval_module(
                ctx, "import { value } from 'model.ts'; export const r = value;"
            ) as ns:
                r = ns.get("r")
                try:
                    assert r.to_python() == 5
                finally:
                    r.dispose()

    assert seen_flags == [
        int(
            SourceTransform.SOURCE_TS
            | SourceTransform.STRIP_TYPESCRIPT
            | SourceTransform.TOP_LEVEL_CONST_TO_VAR
        )
    ]


async def test_ts_extension_import_to_dynamic_import_uses_module_loader() -> None:
    normalize, load = _flat_loader({"dep.ts": "export const value = 41;"})

    with Runtime() as rt:
        rt.set_module_loader(normalize=normalize, load=load)
        with rt.new_context() as ctx:
            await ctx.eval_async(
                "import { value } from './dep.ts'; globalThis.dynamicImportResult = value + 1;",
                module=True,
                transform_flags=SourceTransform.TS_EXTENSION_IMPORT_TO_DYNAMIC_IMPORT,
            )

            assert ctx.eval("globalThis.dynamicImportResult") == 42


def test_malformed_ts_surfaces_as_error() -> None:
    normalize, load = _flat_loader(
        {"broken.ts": "export const x: = ;"}  # malformed TS
    )
    with Runtime() as rt:
        rt.set_module_loader(normalize=normalize, load=load)
        with rt.new_context() as ctx:
            with pytest.raises(JSError):
                ctx.eval("import { x } from 'broken.ts'; x", module=True)
