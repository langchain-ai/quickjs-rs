"""Shared one-shot callables for context-init cold-path benchmarks."""

from __future__ import annotations

from typing import Callable


def make_quickjs_eval_only_end_to_end_cold_runner() -> Callable[[], int]:
    """User-facing API cold path, eval-only baseline."""
    from quickjs_rs import Runtime

    def run_once() -> int:
        rt = Runtime()
        ctx = rt.new_context()
        try:
            return int(ctx.eval("1"))
        finally:
            ctx.close()
            rt.close()

    return run_once


def make_quickjs_full_context_create_close_cold_runner() -> Callable[[], int]:
    """Context create/eval/close cold path with explicit full intrinsic set."""
    from quickjs_rs import Runtime

    def run_once() -> int:
        rt = Runtime()
        ctx = rt.new_context(experimental_intrinsics="full")
        try:
            return int(ctx.eval("1"))
        finally:
            ctx.close()
            rt.close()

    return run_once


def make_quickjs_base_context_create_close_cold_runner() -> Callable[[], None]:
    """Context create/close cold path with base intrinsic set.

    The base intrinsic set intentionally does not support `eval()`.
    """
    from quickjs_rs import Runtime

    def run_once() -> None:
        rt = Runtime()
        ctx = rt.new_context(experimental_intrinsics="base")
        try:
            return None
        finally:
            ctx.close()
            rt.close()

    return run_once


def make_quickjs_custom_eval_context_create_close_cold_runner() -> Callable[[], int]:
    """Context create/eval/close cold path with custom_eval intrinsic set."""
    from quickjs_rs import Runtime

    def run_once() -> int:
        rt = Runtime()
        ctx = rt.new_context(experimental_intrinsics="custom_eval")
        try:
            return int(ctx.eval("1"))
        finally:
            ctx.close()
            rt.close()

    return run_once


def make_quickjs_custom_all_context_create_close_cold_runner() -> Callable[[], int]:
    """Context create/eval/close cold path with custom_all intrinsic set."""
    from quickjs_rs import Runtime

    def run_once() -> int:
        rt = Runtime()
        ctx = rt.new_context(experimental_intrinsics="custom_all")
        try:
            return int(ctx.eval("1"))
        finally:
            ctx.close()
            rt.close()

    return run_once


def make_quickjs_host_call_noop_end_to_end_cold_runner() -> Callable[[], None]:
    """User-facing API cold path with minimal host-call marshaling."""
    from quickjs_rs import Runtime

    def run_once() -> None:
        rt = Runtime()
        ctx = rt.new_context()

        @ctx.function
        def noop() -> None:
            return None

        try:
            return ctx.eval("noop()")
        finally:
            ctx.close()
            rt.close()

    return run_once


def make_quickjs_host_call_end_to_end_cold_runner() -> Callable[[], int]:
    """User-facing API cold path with int arg/return marshaling."""
    from quickjs_rs import Runtime

    def run_once() -> int:
        rt = Runtime()
        ctx = rt.new_context(experimental_intrinsics="full")

        @ctx.function
        def ident(n: int) -> int:
            return n

        try:
            return int(ctx.eval("ident(1)"))
        finally:
            ctx.close()
            rt.close()

    return run_once


def make_quickjs_custom_all_host_call_end_to_end_cold_runner() -> Callable[[], int]:
    """User-facing API cold path using experimental `custom_all` intrinsics."""
    from quickjs_rs import Runtime

    def run_once() -> int:
        rt = Runtime()
        ctx = rt.new_context(experimental_intrinsics="custom_all")

        @ctx.function
        def ident(n: int) -> int:
            return n

        try:
            return int(ctx.eval("ident(1)"))
        finally:
            ctx.close()
            rt.close()

    return run_once


def make_quickjs_host_call_dict_end_to_end_cold_runner() -> Callable[[], dict[str, int]]:
    """User-facing API cold path with dict-return marshaling."""
    from quickjs_rs import Runtime

    def run_once() -> dict[str, int]:
        rt = Runtime()
        ctx = rt.new_context()

        @ctx.function
        def make_dict() -> dict[str, int]:
            return {"a": 1, "b": 2, "c": 3}

        try:
            value = ctx.eval("make_dict()")
            return dict(value)
        finally:
            ctx.close()
            rt.close()

    return run_once


def make_quickjs_custom_eval_host_call_end_to_end_cold_runner() -> Callable[[], int]:
    """User-facing API cold path using experimental `custom_eval` intrinsics."""
    from quickjs_rs import Runtime

    def run_once() -> int:
        rt = Runtime()
        ctx = rt.new_context(experimental_intrinsics="custom_eval")

        @ctx.function
        def ident(n: int) -> int:
            return n

        try:
            return int(ctx.eval("ident(1)"))
        finally:
            ctx.close()
            rt.close()

    return run_once
