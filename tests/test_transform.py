from quickjs_rs import (
    Runtime,
    SourceTransform,
    default_module_transform_flags,
    transform_source,
)
from quickjs_rs._engine import _quickjs_artifact
from quickjs_rs._transform import (
    FLAG_SOURCE_TS,
    FLAG_SOURCE_TSX,
    FLAG_STATIC_IMPORT_TO_DYNAMIC_IMPORT,
    FLAG_STRIP_TYPESCRIPT,
    FLAG_TOP_LEVEL_CONST_TO_VAR,
    SourceTransformer,
    transform_module_source,
)


def test_source_kind_is_explicit_host_policy_not_filename() -> None:
    source = "export function pick(o: {n: number}): number { return o.n; }"

    transformed = transform_module_source(
        "not-a-tsx-extension",
        source,
        flags=FLAG_SOURCE_TSX | FLAG_STRIP_TYPESCRIPT,
    )

    assert "number" not in transformed
    assert "export function pick(o)" in transformed


def test_top_level_const_to_var_rewrites_only_top_level_declarations() -> None:
    source = """
const a = 1;
{
  const nested = 2;
}
export const b = 3;
function f() {
  const local = 4;
}
"""

    transformed = transform_module_source(
        "plain.js",
        source,
        flags=FLAG_TOP_LEVEL_CONST_TO_VAR,
    )

    assert "var a = 1;" in transformed
    assert "export var b = 3;" in transformed
    assert "const nested = 2;" in transformed
    assert "const local = 4;" in transformed


def test_public_transform_source_exposes_top_level_const_rewriter() -> None:
    transformed = transform_source(
        "plain.js",
        "export const value = 1;",
        flags=SourceTransform.TOP_LEVEL_CONST_TO_VAR,
    )

    assert "export var value = 1;" in transformed


def test_static_import_to_dynamic_import_rewrites_imports_without_changing_specifiers() -> None:
    source = """
import value, { thing as alias, other } from "./file.ts";
import * as ns from "../pkg/view.tsx";
import "./setup.mts";
import { keep } from "pkg.ts";
import { plain } from "./plain.js";
export const result = [value, alias, other, ns, keep, plain];
"""

    transformed = transform_source(
        "plain.js",
        source,
        flags=SourceTransform.STATIC_IMPORT_TO_DYNAMIC_IMPORT,
    )

    assert (
        'const { default: value, thing: alias, other } = await import("./file.ts");'
        in transformed
    )
    assert 'const ns = await import("../pkg/view.tsx");' in transformed
    assert 'await import("./setup.mts");' in transformed
    assert 'const { keep } = await import("pkg.ts");' in transformed
    assert 'const { plain } = await import("./plain.js");' in transformed


def test_static_import_to_dynamic_import_handles_default_namespace_import() -> None:
    transformed = transform_source(
        "plain.js",
        'import value, * as ns from "./file.ts"; export const result = [value, ns];',
        flags=SourceTransform.STATIC_IMPORT_TO_DYNAMIC_IMPORT,
    )

    assert 'const ns = await import("./file.ts");' in transformed
    assert "const { default: value } = ns;" in transformed


def test_static_import_to_dynamic_import_does_not_restore_type_only_imports() -> None:
    transformed = transform_module_source(
        "model.ts",
        'import type { Model } from "./types.ts"; export const value: number = 1;',
        flags=(
            FLAG_SOURCE_TS
            | FLAG_STRIP_TYPESCRIPT
            | FLAG_STATIC_IMPORT_TO_DYNAMIC_IMPORT
        ),
    )

    assert 'import("./types.ts")' not in transformed
    assert "export const value = 1;" in transformed


def test_public_default_module_transform_flags() -> None:
    assert default_module_transform_flags("plain.js") == SourceTransform.NONE
    assert default_module_transform_flags("model.ts") == (
        SourceTransform.SOURCE_TS | SourceTransform.STRIP_TYPESCRIPT
    )
    assert default_module_transform_flags("view.tsx") == (
        SourceTransform.SOURCE_TSX | SourceTransform.STRIP_TYPESCRIPT
    )


def test_public_transform_source_none_disables_default_policy() -> None:
    source = "export const value: number = 1;"

    assert transform_source("model.ts", source, flags=SourceTransform.NONE) == source


def test_runtime_transform_flags_apply_to_eval() -> None:
    with Runtime(transform_flags=SourceTransform.TOP_LEVEL_CONST_TO_VAR) as rt:
        with rt.new_context() as ctx:
            ctx.eval("const runtimeEvalValue = 11;")

            assert ctx.eval("globalThis.runtimeEvalValue") == 11


def test_eval_transform_flags_override_runtime_policy() -> None:
    with Runtime(transform_flags=SourceTransform.TOP_LEVEL_CONST_TO_VAR) as rt:
        with rt.new_context() as ctx:
            ctx.eval("const scopedValue = 12;", transform_flags=SourceTransform.NONE)

            assert ctx.eval("'scopedValue' in globalThis") is False


def test_eval_handle_transform_flags_apply_to_top_level_eval() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with ctx.eval_handle(
                "const handleEvalValue = {answer: 13}; globalThis.handleEvalValue",
                transform_flags=SourceTransform.TOP_LEVEL_CONST_TO_VAR,
            ) as handle:
                answer = handle.get("answer")
                try:
                    assert answer.to_python() == 13
                finally:
                    answer.dispose()


async def test_runtime_transform_flags_apply_to_eval_async() -> None:
    with Runtime(transform_flags=SourceTransform.TOP_LEVEL_CONST_TO_VAR) as rt:
        with rt.new_context() as ctx:
            assert (
                await ctx.eval_async(
                    "const asyncEvalValue = 14; globalThis.asyncEvalValue"
                )
                == 14
            )


async def test_eval_handle_async_transform_flags_apply_to_top_level_eval() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            handle = await ctx.eval_handle_async(
                "const asyncHandleValue: number = 15; asyncHandleValue",
                transform_flags=SourceTransform.SOURCE_TS | SourceTransform.STRIP_TYPESCRIPT,
            )
            try:
                assert handle.to_python() == 15
            finally:
                handle.dispose()


def test_source_transformer_reuses_owned_instance_and_cache() -> None:
    transformer = SourceTransformer()
    source = "export const x: number = 1;"
    flags = FLAG_SOURCE_TSX | FLAG_STRIP_TYPESCRIPT

    try:
        assert transformer.transform("same.tsx", source, flags=flags)
        first_instance = transformer._instance
        assert first_instance is not None

        assert transformer.transform("same.tsx", source, flags=flags)

        assert transformer._instance is first_instance
        assert len(transformer._cache) == 1
    finally:
        transformer.close()


def test_source_transformers_have_distinct_instances() -> None:
    a = SourceTransformer()
    b = SourceTransformer()
    source = "export const x: number = 1;"
    flags = FLAG_SOURCE_TSX | FLAG_STRIP_TYPESCRIPT

    try:
        assert a.transform("same.tsx", source, flags=flags)
        assert b.transform("same.tsx", source, flags=flags)

        assert a._instance is not None
        assert b._instance is not None
        assert a._instance is not b._instance
    finally:
        a.close()
        b.close()


def test_transform_and_quickjs_artifacts_do_not_store_engine() -> None:
    transformer = SourceTransformer()
    quickjs_artifact = _quickjs_artifact()
    source = "export const x: number = 1;"
    flags = FLAG_SOURCE_TSX | FLAG_STRIP_TYPESCRIPT

    try:
        transformer.transform("same.tsx", source, flags=flags)

        assert transformer._artifact is not None
        assert not hasattr(transformer._artifact, "engine")
        assert not hasattr(quickjs_artifact, "engine")
    finally:
        transformer.close()


def test_module_loader_uses_context_owned_transformers(monkeypatch) -> None:
    seen_transformers: list[int] = []
    original_transform = SourceTransformer.transform

    def record_transform(
        self: SourceTransformer,
        name: str,
        source: str,
        *,
        flags: int | None = None,
    ) -> str:
        if name == "x.ts":
            seen_transformers.append(id(self))
        return original_transform(self, name, source, flags=flags)

    monkeypatch.setattr("quickjs_rs._engine.SourceTransformer.transform", record_transform)

    with Runtime() as rt:
        rt.set_module_loader(load=lambda name: "export const value: number = 5;")
        with rt.new_context() as ctx1:
            with ctx1.eval_handle(
                "import { value } from 'x.ts'; export const r = value;",
                module=True,
            ):
                pass
        with rt.new_context() as ctx2:
            with ctx2.eval_handle(
                "import { value } from 'x.ts'; export const r = value;",
                module=True,
            ):
                pass

    assert len(seen_transformers) == 2
    assert seen_transformers[0] != seen_transformers[1]
