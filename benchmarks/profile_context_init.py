"""CPU profiling harness for context-init cold-path benchmarks.

Run as module for clean imports:
    python -m benchmarks.profile_context_init --case quickjs_base_context_create_close

Capture raw cProfile output:
    python -m cProfile -o artifacts/profiles/context-base.prof \
      -m benchmarks.profile_context_init --case quickjs_base_context_create_close
"""

from __future__ import annotations

import argparse
import cProfile
import gc
import time
from pathlib import Path
from pstats import SortKey, Stats
from typing import Callable, Literal

try:
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
except ModuleNotFoundError:
    # Allow running as a file path: python benchmarks/profile_context_init.py ...
    from context_init_cases import (  # type: ignore[no-redef]
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

Case = Literal[
    "quickjs_eval_only",
    "quickjs_full_context_create_close",
    "quickjs_base_context_create_close",
    "quickjs_custom_eval_context_create_close",
    "quickjs_custom_all_context_create_close",
    "quickjs_host_call_noop",
    "quickjs_host_call_int",
    "quickjs_host_call_dict",
    "quickjs_custom_all_host_call_int",
    "quickjs_custom_eval_host_call_int",
]
SortArg = Literal["cumtime", "tottime", "calls"]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        choices=(
            "quickjs_eval_only",
            "quickjs_full_context_create_close",
            "quickjs_base_context_create_close",
            "quickjs_custom_eval_context_create_close",
            "quickjs_custom_all_context_create_close",
            "quickjs_host_call_noop",
            "quickjs_host_call_int",
            "quickjs_host_call_dict",
            "quickjs_custom_all_host_call_int",
            "quickjs_custom_eval_host_call_int",
        ),
        default="quickjs_base_context_create_close",
        help="Which context-init benchmark case to profile.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=2000,
        help="Number of measured iterations after warmup.",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=25,
        help="Warmup iterations before profiling starts.",
    )
    parser.add_argument(
        "--sort",
        choices=("cumtime", "tottime", "calls"),
        default="cumtime",
        help="Sort key for printed pstats table.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=30,
        help="How many pstats rows to print.",
    )
    parser.add_argument(
        "--dump-stats",
        type=Path,
        default=None,
        help="Optional output .prof path.",
    )
    return parser.parse_args()


def _case_runner(case: Case) -> tuple[str, Callable[[], object], object]:
    if case == "quickjs_eval_only":
        return case, make_quickjs_eval_only_end_to_end_cold_runner(), 1
    if case == "quickjs_full_context_create_close":
        return case, make_quickjs_full_context_create_close_cold_runner(), 1
    if case == "quickjs_base_context_create_close":
        return case, make_quickjs_base_context_create_close_cold_runner(), None
    if case == "quickjs_custom_eval_context_create_close":
        return case, make_quickjs_custom_eval_context_create_close_cold_runner(), 1
    if case == "quickjs_custom_all_context_create_close":
        return case, make_quickjs_custom_all_context_create_close_cold_runner(), 1
    if case == "quickjs_host_call_noop":
        return case, make_quickjs_host_call_noop_end_to_end_cold_runner(), None
    if case == "quickjs_host_call_int":
        return case, make_quickjs_host_call_end_to_end_cold_runner(), 1
    if case == "quickjs_host_call_dict":
        return case, make_quickjs_host_call_dict_end_to_end_cold_runner(), {"a": 1, "b": 2, "c": 3}
    if case == "quickjs_custom_all_host_call_int":
        return case, make_quickjs_custom_all_host_call_end_to_end_cold_runner(), 1
    if case == "quickjs_custom_eval_host_call_int":
        return case, make_quickjs_custom_eval_host_call_end_to_end_cold_runner(), 1
    raise RuntimeError(f"unknown case: {case}")


def _run_profile(
    label: str,
    func: Callable[[], object],
    *,
    iterations: int,
    warmup: int,
    sort_arg: SortArg,
    top: int,
    dump_path: Path | None,
) -> None:
    for _ in range(warmup):
        func()
    gc.collect()

    start = time.perf_counter()
    profiler: cProfile.Profile | None = cProfile.Profile()
    using_external_profiler = False
    try:
        profiler.enable()
    except ValueError:
        profiler = None
        using_external_profiler = True

    if profiler is None:
        for _ in range(iterations):
            func()
    else:
        for _ in range(iterations):
            func()
        profiler.disable()
    elapsed = time.perf_counter() - start

    if dump_path is not None and profiler is not None:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        profiler.dump_stats(str(dump_path))

    print(f"\n== {label} ==")
    print(
        f"iterations={iterations} warmup={warmup} "
        f"total={elapsed:.6f}s per_call={elapsed / max(iterations, 1):.6e}s"
    )
    if dump_path is not None and profiler is not None:
        print(f"wrote profile: {dump_path}")
    elif dump_path is not None and profiler is None:
        print(
            "note: --dump-stats skipped because an external profiler is active "
            "(use the external profiler output file instead)"
        )

    if profiler is None:
        print(
            "note: internal pstats table is disabled because an external "
            "profiler is active"
        )
        if using_external_profiler:
            print("note: profile data was recorded by the external profiler")
        return

    sort_key: SortKey = SortKey.CUMULATIVE
    if sort_arg == "tottime":
        sort_key = SortKey.TIME
    elif sort_arg == "calls":
        sort_key = SortKey.CALLS
    Stats(profiler).strip_dirs().sort_stats(sort_key).print_stats(top)


def main() -> int:
    args = _parse_args()
    case: Case = args.case
    iterations: int = args.iterations
    warmup: int = args.warmup
    sort_arg: SortArg = args.sort
    top: int = args.top
    dump_stats: Path | None = args.dump_stats

    if iterations <= 0:
        raise SystemExit("--iterations must be >= 1")
    if warmup < 0:
        raise SystemExit("--warmup must be >= 0")
    if top <= 0:
        raise SystemExit("--top must be >= 1")

    label, fn, expected = _case_runner(case)
    got = fn()
    assert got == expected, f"{label} sanity check failed: expected {expected!r}, got {got!r}"
    _run_profile(
        label=label,
        func=fn,
        iterations=iterations,
        warmup=warmup,
        sort_arg=sort_arg,
        top=top,
        dump_path=dump_stats,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
