"""Snapshot V1 tracking groundwork tests."""

from __future__ import annotations

import json
import struct

import pytest

from quickjs_rs import JSError, QuickJSError, Runtime, Snapshot
from quickjs_rs.handle import Handle


def test_registry_tracks_top_level_decls_and_destructuring() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval(
                """
                const { a, b: c, ...rest } = { a: 1, b: 2, d: 3 };
                let [x, , y = 3, ...z] = [1, 2, 3, 4];
                function f() {}
                class K {}
                """
            )
            assert ctx._debug_snapshot_registry_names() == (
                "a",
                "c",
                "rest",
                "x",
                "y",
                "z",
                "f",
                "K",
            )


def test_registry_dedupes_first_seen_across_evals() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval("var a = 1; var b = 2;")
            ctx.eval("var b = 3; var c = 4;")
            assert ctx._debug_snapshot_registry_names() == ("a", "b", "c")


def test_registry_ignores_nested_declarations() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval(
                """
                if (true) {
                    const hidden = 1;
                    function nope() {}
                }
                const top = 1;
                """
            )
            assert ctx._debug_snapshot_registry_names() == ("top",)


def test_parser_error_does_not_corrupt_registry() -> None:
    from quickjs_rs import JSError

    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval("const ok = 1;")
            with pytest.raises(JSError):
                ctx.eval("const =")
            assert ctx._debug_snapshot_registry_names() == ("ok",)


def test_eval_handle_and_eval_handle_async_update_registry() -> None:
    async def run() -> tuple[str, ...]:
        with Runtime() as rt:
            with rt.new_context() as ctx:
                with ctx.eval_handle("const fromHandle = 1; fromHandle;"):
                    pass
                with await ctx.eval_handle_async("const fromHandleAsync = 2; fromHandleAsync;"):
                    pass
                return ctx._debug_snapshot_registry_names()

    import asyncio

    assert asyncio.run(run()) == ("fromHandle", "fromHandleAsync")


def test_create_snapshot_roundtrip() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval("const test = 123;")
            data = ctx.create_snapshot().to_bytes()

    with Runtime() as rt2:
        with rt2.new_context() as ctx2:
            snap = Snapshot.from_bytes(data)
            rt2.restore_snapshot(snap, ctx2, inject_globals=True)
            assert ctx2.eval("test") == 123


def test_snapshot_restore_preserves_aliasing() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval("const shared = { n: 1 }; const a = shared; const b = shared;")
            snap = ctx.create_snapshot()

    with Runtime() as rt2:
        with rt2.new_context() as ctx2:
            rt2.restore_snapshot(snap, ctx2)
            assert ctx2.eval("a === b") is True


def test_snapshot_missing_name_policies() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with pytest.raises(JSError):
                ctx.eval("throw new Error('boom'); const late = 1;")
            skip_snap = ctx.create_snapshot(on_missing_name="skip")
            tomb_snap = ctx.create_snapshot(on_missing_name="tombstone")
            assert isinstance(skip_snap, Snapshot)
            assert isinstance(tomb_snap, Snapshot)
            with pytest.raises(JSError):
                ctx.create_snapshot(on_missing_name="error")

    with Runtime() as rt2:
        with rt2.new_context() as ctx2:
            rt2.restore_snapshot(tomb_snap, ctx2)
            assert ctx2.eval("Object.prototype.hasOwnProperty.call(globalThis, 'late')") is True
            with pytest.raises(
                JSError,
                match="Value for 'late' was not captured because the identifier was not resolvable",
            ):
                ctx2.eval("late")

    with Runtime() as rt3:
        with rt3.new_context() as ctx3:
            rt3.restore_snapshot(skip_snap, ctx3)
            assert ctx3.eval("Object.prototype.hasOwnProperty.call(globalThis, 'late')") is False
            with pytest.raises(JSError, match="late is not defined"):
                ctx3.eval("late")


def test_snapshot_unserializable_policies() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval("const fn = () => 1;")
            snap = ctx.create_snapshot(on_unserializable="tombstone")
            with pytest.raises(QuickJSError, match="not serializable"):
                ctx.create_snapshot(on_unserializable="error")

    with Runtime() as rt2:
        with rt2.new_context() as ctx2:
            rt2.restore_snapshot(snap, ctx2)
            with pytest.raises(
                JSError,
                match="Value for 'fn' was not restored because it is not serializable",
            ):
                ctx2.eval("fn")


def test_restore_snapshot_overwrites_existing_globals() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval("const keep = 7;")
            snap = ctx.create_snapshot()

    with Runtime() as rt2:
        with rt2.new_context() as ctx2:
            ctx2.eval("globalThis.keep = 999;")
            assert ctx2.eval("keep") == 999
            rt2.restore_snapshot(snap, ctx2)
            assert ctx2.eval("keep") == 7


def test_restore_snapshot_unknown_version_rejected() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval("const x = 1;")
            data = bytearray(ctx.create_snapshot().to_bytes())

    data[4] = 2
    with Runtime() as rt2:
        with rt2.new_context() as ctx2:
            with pytest.raises(ValueError, match="format version"):
                rt2.restore_snapshot(Snapshot.from_bytes(bytes(data)), ctx2)


def test_restore_snapshot_rquickjs_version_rejected() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            ctx.eval("const x = 1;")
            data = _rewrite_snapshot_header(
                ctx.create_snapshot().to_bytes(),
                {"rquickjs_version": "0.0.0-test"},
            )

    with Runtime() as rt2:
        with rt2.new_context() as ctx2:
            with pytest.raises(ValueError, match="rquickjs version"):
                rt2.restore_snapshot(Snapshot.from_bytes(data), ctx2)


def test_create_snapshot_module_mode_guard_sync() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with ctx.eval_handle("globalThis.modTouched = 1", module=True):
                pass
            with pytest.raises(NotImplementedError, match="module=True"):
                ctx.create_snapshot()


async def test_create_snapshot_module_mode_guard_async() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            await ctx.eval_async("globalThis.modTouched = 1", module=True)
            with pytest.raises(NotImplementedError, match="module=True"):
                ctx.create_snapshot()


def test_engine_dump_load_handle_roundtrip() -> None:
    with Runtime() as rt:
        with rt.new_context() as ctx:
            with ctx.eval_handle("({ n: 7, items: [1, 2, 3] })") as h:
                blob = ctx._engine_ctx.dump_handle(h._require_live())
            loaded_engine = ctx._engine_ctx.load_handle(blob)
            loaded = Handle(ctx, loaded_engine)
            try:
                assert loaded.get("n").to_python() == 7
                items = loaded.get("items")
                try:
                    assert items.get_index(2).to_python() == 3
                finally:
                    items.dispose()
            finally:
                loaded.dispose()


def test_engine_dump_load_invalid_bytes_raises() -> None:
    import quickjs_rs._engine as _engine

    with Runtime() as rt:
        with rt.new_context() as ctx:
            with pytest.raises((_engine.JSError, _engine.QuickJSError)):
                ctx._engine_ctx.load_handle(b"not-valid-qjs-blob")


def _rewrite_snapshot_header(data: bytes, updates: dict[str, str]) -> bytes:
    if len(data) < 9:
        raise ValueError("snapshot payload too short")
    magic = data[:4]
    version = data[4:5]
    header_len = struct.unpack("<I", data[5:9])[0]
    header_start = 9
    header_end = header_start + header_len
    header = json.loads(data[header_start:header_end].decode("utf-8"))
    header.update(updates)
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return b"".join(
        [
            magic,
            version,
            struct.pack("<I", len(header_bytes)),
            header_bytes,
            data[header_end:],
        ]
    )
