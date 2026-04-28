"""Context-initialization cold-path benchmarks.

Each benchmark is self-contained and creates a fresh runtime/context per
iteration to measure end-to-end cold-path costs.
"""

from __future__ import annotations

from pytest_codspeed import BenchmarkFixture

from benchmarks.context_init_cases import (
    make_quickjs_base_context_create_close_cold_runner,
    make_quickjs_custom_all_context_create_close_cold_runner,
    make_quickjs_custom_all_host_call_end_to_end_cold_runner,
    make_quickjs_custom_eval_context_create_close_cold_runner,
    make_quickjs_custom_eval_host_call_end_to_end_cold_runner,
    make_quickjs_eval_only_end_to_end_cold_runner,
    make_quickjs_full_context_create_close_cold_runner,
    make_quickjs_host_call_dict_end_to_end_cold_runner,
    make_quickjs_host_call_end_to_end_cold_runner,
    make_quickjs_host_call_noop_end_to_end_cold_runner,
)


def bench_quickjs_eval_only_end_to_end_cold(benchmark: BenchmarkFixture) -> None:
    """User-facing API cold path, eval-only baseline."""
    run_once = make_quickjs_eval_only_end_to_end_cold_runner()
    assert run_once() == 1
    benchmark(run_once)


def bench_quickjs_full_context_create_close_cold(benchmark: BenchmarkFixture) -> None:
    """Context create/eval/close cold path with explicit full intrinsic set."""
    run_once = make_quickjs_full_context_create_close_cold_runner()
    assert run_once() == 1
    benchmark(run_once)


def bench_quickjs_base_context_create_close_cold(benchmark: BenchmarkFixture) -> None:
    """Context create/close cold path with base intrinsic set."""
    run_once = make_quickjs_base_context_create_close_cold_runner()
    assert run_once() is None
    benchmark(run_once)


def bench_quickjs_custom_eval_context_create_close_cold(
    benchmark: BenchmarkFixture,
) -> None:
    """Context create/eval/close cold path with custom_eval intrinsic set."""
    run_once = make_quickjs_custom_eval_context_create_close_cold_runner()
    assert run_once() == 1
    benchmark(run_once)


def bench_quickjs_custom_all_context_create_close_cold(
    benchmark: BenchmarkFixture,
) -> None:
    """Context create/eval/close cold path with custom_all intrinsic set."""
    run_once = make_quickjs_custom_all_context_create_close_cold_runner()
    assert run_once() == 1
    benchmark(run_once)


def bench_quickjs_host_call_noop_end_to_end_cold(benchmark: BenchmarkFixture) -> None:
    """User-facing API cold path with minimal host-call marshaling."""
    run_once = make_quickjs_host_call_noop_end_to_end_cold_runner()
    assert run_once() is None
    benchmark(run_once)


def bench_quickjs_host_call_end_to_end_cold(benchmark: BenchmarkFixture) -> None:
    """User-facing API cold path with int arg/return marshaling."""
    run_once = make_quickjs_host_call_end_to_end_cold_runner()
    assert run_once() == 1
    benchmark(run_once)


def bench_quickjs_custom_all_host_call_end_to_end_cold(
    benchmark: BenchmarkFixture,
) -> None:
    """User-facing API cold path using experimental `custom_all` intrinsics."""
    run_once = make_quickjs_custom_all_host_call_end_to_end_cold_runner()
    assert run_once() == 1
    benchmark(run_once)


def bench_quickjs_host_call_dict_end_to_end_cold(benchmark: BenchmarkFixture) -> None:
    """User-facing API cold path with dict-return marshaling."""
    run_once = make_quickjs_host_call_dict_end_to_end_cold_runner()
    assert run_once() == {"a": 1, "b": 2, "c": 3}
    benchmark(run_once)


def bench_quickjs_custom_eval_host_call_end_to_end_cold(
    benchmark: BenchmarkFixture,
) -> None:
    """User-facing API cold path using experimental `custom_eval` intrinsics."""
    run_once = make_quickjs_custom_eval_host_call_end_to_end_cold_runner()
    assert run_once() == 1
    benchmark(run_once)
