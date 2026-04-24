"""Configurable memory-pressure experiment runner for quickjs-rs.

This script sweeps runtime/context mix configurations, records process-RSS
and QuickJS memory counters at key lifecycle phases, and writes CSV output
that can be plotted in notebooks or attached to CI artifacts.
"""

from __future__ import annotations

import argparse
import csv
import gc
import math
import os
import platform
import resource
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from quickjs_rs import Runtime

MB = 1024 * 1024
DEFAULT_PROCESS_CAP_MB = 1024
DEFAULT_RUNTIMES = "1,2,4,8,12,16"
DEFAULT_CONTEXTS_PER_RUNTIME = "1,2,4,8"
DEFAULT_MEMORY_LIMIT_MB = "16,32,64"
DEFAULT_PAYLOAD_MB_PER_CONTEXT = "0,1,4"

MEMORY_USAGE_FIELDS = (
    "malloc_size",
    "malloc_limit",
    "memory_used_size",
    "malloc_count",
    "memory_used_count",
    "atom_count",
    "atom_size",
    "str_count",
    "str_size",
    "obj_count",
    "obj_size",
    "prop_count",
    "prop_size",
    "shape_count",
    "shape_size",
    "js_func_count",
    "js_func_size",
    "js_func_code_size",
    "js_func_pc2line_count",
    "js_func_pc2line_size",
    "c_func_count",
    "array_count",
    "fast_array_count",
    "fast_array_elements",
    "binary_object_count",
    "binary_object_size",
)


@dataclass(frozen=True)
class ExperimentConfig:
    runtimes: int
    contexts_per_runtime: int
    memory_limit_mb: int
    payload_mb_per_context: int

    @property
    def total_contexts(self) -> int:
        return self.runtimes * self.contexts_per_runtime

    @property
    def memory_limit_bytes(self) -> int | None:
        if self.memory_limit_mb <= 0:
            return None
        return self.memory_limit_mb * MB

    @property
    def payload_bytes_per_context(self) -> int:
        return self.payload_mb_per_context * MB


@dataclass
class ExperimentResult:
    runtimes: int
    contexts_per_runtime: int
    total_contexts: int
    memory_limit_mb: int
    payload_mb_per_context: int
    rss_probe_kind: str
    baseline_rss_bytes: int
    after_spawn_rss_bytes: int
    after_payload_rss_bytes: int
    after_gc_rss_bytes: int
    after_close_rss_bytes: int
    peak_rss_bytes: int
    rss_delta_spawn_bytes: int
    rss_delta_payload_bytes: int
    rss_delta_gc_bytes: int
    rss_delta_close_bytes: int
    over_cap_after_payload: bool
    process_cap_bytes: int
    estimated_bytes_per_runtime_bundle: int
    estimated_max_runtimes_same_mix: int
    max_runtimes_if_all_hit_memory_limit: int
    quickjs_malloc_size_after_payload: int
    quickjs_memory_used_size_after_payload: int
    quickjs_malloc_limit_after_payload: int
    quickjs_malloc_size_after_gc: int
    quickjs_memory_used_size_after_gc: int
    quickjs_malloc_limit_after_gc: int
    error: str | None = None


def _mb(n: int) -> float:
    return n / MB


def parse_int_csv(raw: str) -> list[int]:
    values: list[int] = []
    for chunk in raw.split(","):
        token = chunk.strip()
        if not token:
            continue
        values.append(int(token))
    if not values:
        raise ValueError(f"expected at least one integer, got {raw!r}")
    return values


def current_rss_bytes() -> tuple[int, str]:
    """Return current RSS bytes and probe source."""
    status_path = "/proc/self/status"
    if os.path.exists(status_path):
        with open(status_path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024, "proc_status_vmrss"

    # Fallback: ru_maxrss is peak RSS, not current, on some platforms.
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if platform.system() == "Darwin":
        return int(ru), "ru_maxrss_peak_bytes"
    return int(ru) * 1024, "ru_maxrss_peak_kib"


def peak_rss_bytes() -> int:
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if platform.system() == "Darwin":
        return int(ru)
    return int(ru) * 1024


def aggregate_memory_usage(runtimes: list[Runtime]) -> dict[str, int]:
    totals = {field: 0 for field in MEMORY_USAGE_FIELDS}
    for rt in runtimes:
        usage = rt.memory_usage()
        for field in MEMORY_USAGE_FIELDS:
            totals[field] += int(usage.get(field, 0))
    return totals


def allocate_payload(ctx: Any, payload_bytes: int) -> None:
    if payload_bytes <= 0:
        ctx.eval("globalThis.__memory_profile_payload = undefined")
        return
    ctx.eval(
        f"""
        (() => {{
            const payload = [];
            let remaining = {payload_bytes};
            const chunk = 256 * 1024;
            while (remaining > 0) {{
                const n = Math.min(chunk, remaining);
                const buf = new Uint8Array(n);
                if (n > 0) buf[0] = payload.length & 0xff;
                payload.push(buf);
                remaining -= n;
            }}
            globalThis.__memory_profile_payload = payload;
            return payload.length;
        }})()
        """
    )


def clear_payload(ctx: Any) -> None:
    ctx.eval("globalThis.__memory_profile_payload = undefined")


def run_single_config(
    config: ExperimentConfig,
    *,
    process_cap_bytes: int,
) -> ExperimentResult:
    runtimes: list[Runtime] = []
    contexts: list[Any] = []

    baseline_rss, rss_probe_kind = current_rss_bytes()
    after_spawn_rss = baseline_rss
    after_payload_rss = baseline_rss
    after_gc_rss = baseline_rss
    after_close_rss = baseline_rss
    peak_rss = baseline_rss
    quickjs_after_payload = {field: 0 for field in MEMORY_USAGE_FIELDS}
    quickjs_after_gc = {field: 0 for field in MEMORY_USAGE_FIELDS}
    error: str | None = None

    try:
        for _ in range(config.runtimes):
            rt = Runtime(memory_limit=config.memory_limit_bytes)
            runtimes.append(rt)
            for _ in range(config.contexts_per_runtime):
                contexts.append(rt.new_context(timeout=5.0))

        after_spawn_rss, _ = current_rss_bytes()

        for ctx in contexts:
            allocate_payload(ctx, config.payload_bytes_per_context)
        after_payload_rss, _ = current_rss_bytes()
        quickjs_after_payload = aggregate_memory_usage(runtimes)

        for ctx in contexts:
            clear_payload(ctx)
        for rt in runtimes:
            rt.run_gc()
        gc.collect()

        after_gc_rss, _ = current_rss_bytes()
        peak_rss = peak_rss_bytes()
        quickjs_after_gc = aggregate_memory_usage(runtimes)
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        for ctx in reversed(contexts):
            try:
                ctx.close()
            except Exception:
                pass
        for rt in reversed(runtimes):
            try:
                rt.close()
            except Exception:
                pass
        gc.collect()
        after_close_rss, _ = current_rss_bytes()
        peak_rss = max(peak_rss, peak_rss_bytes())

    delta = max(after_payload_rss - baseline_rss, 0)
    estimated_per_runtime = math.ceil(delta / max(config.runtimes, 1))
    if estimated_per_runtime <= 0:
        estimated_max = 0
    else:
        estimated_max = max((process_cap_bytes - baseline_rss) // estimated_per_runtime, 0)
    if config.memory_limit_mb <= 0:
        max_if_all_hit_limit = -1
    else:
        max_if_all_hit_limit = process_cap_bytes // (config.memory_limit_mb * MB)

    return ExperimentResult(
        runtimes=config.runtimes,
        contexts_per_runtime=config.contexts_per_runtime,
        total_contexts=config.total_contexts,
        memory_limit_mb=config.memory_limit_mb,
        payload_mb_per_context=config.payload_mb_per_context,
        rss_probe_kind=rss_probe_kind,
        baseline_rss_bytes=baseline_rss,
        after_spawn_rss_bytes=after_spawn_rss,
        after_payload_rss_bytes=after_payload_rss,
        after_gc_rss_bytes=after_gc_rss,
        after_close_rss_bytes=after_close_rss,
        peak_rss_bytes=peak_rss,
        rss_delta_spawn_bytes=after_spawn_rss - baseline_rss,
        rss_delta_payload_bytes=after_payload_rss - baseline_rss,
        rss_delta_gc_bytes=after_gc_rss - baseline_rss,
        rss_delta_close_bytes=after_close_rss - baseline_rss,
        over_cap_after_payload=after_payload_rss > process_cap_bytes,
        process_cap_bytes=process_cap_bytes,
        estimated_bytes_per_runtime_bundle=estimated_per_runtime,
        estimated_max_runtimes_same_mix=int(estimated_max),
        max_runtimes_if_all_hit_memory_limit=int(max_if_all_hit_limit),
        quickjs_malloc_size_after_payload=quickjs_after_payload["malloc_size"],
        quickjs_memory_used_size_after_payload=quickjs_after_payload["memory_used_size"],
        quickjs_malloc_limit_after_payload=quickjs_after_payload["malloc_limit"],
        quickjs_malloc_size_after_gc=quickjs_after_gc["malloc_size"],
        quickjs_memory_used_size_after_gc=quickjs_after_gc["memory_used_size"],
        quickjs_malloc_limit_after_gc=quickjs_after_gc["malloc_limit"],
        error=error,
    )


def write_csv(path: Path, rows: list[ExperimentResult]) -> None:
    fieldnames = list(asdict(rows[0]).keys()) if rows else []
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def to_mb(bytes_value: int) -> float:
    return bytes_value / MB


def _save_plot(fig: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    fig.clf()


def _plot_scatter(rows: list[ExperimentResult], out_path: Path, plt: Any) -> None:
    valid = [r for r in rows if not r.error]
    fig, ax = plt.subplots(figsize=(8, 5))
    x = [_mb(r.quickjs_malloc_size_after_payload) for r in valid]
    y = [_mb(r.after_payload_rss_bytes) for r in valid]
    c = [r.total_contexts for r in valid]
    s = [40 + 12 * r.runtimes for r in valid]
    sc = ax.scatter(
        x,
        y,
        c=c,
        s=s,
        cmap="viridis",
        alpha=0.8,
        edgecolors="black",
        linewidths=0.4,
    )
    ax.set_title("Whole-Process RSS vs QuickJS malloc (after payload)")
    ax.set_xlabel("QuickJS malloc_size (MB)")
    ax.set_ylabel("Process RSS (MB)")
    cb = fig.colorbar(sc, ax=ax)
    cb.set_label("Total contexts")
    _save_plot(fig, out_path)
    plt.close(fig)


def _plot_overhead_stack(rows: list[ExperimentResult], out_path: Path, plt: Any) -> None:
    valid = [r for r in rows if not r.error]
    top = sorted(valid, key=lambda r: r.after_payload_rss_bytes, reverse=True)[:10]
    labels = [
        (
            f"r{r.runtimes}-c{r.contexts_per_runtime}-"
            f"m{r.memory_limit_mb}-p{r.payload_mb_per_context}"
        )
        for r in top
    ]
    qjs = [_mb(r.quickjs_malloc_size_after_payload) for r in top]
    residual = [
        _mb(max(r.after_payload_rss_bytes - r.quickjs_malloc_size_after_payload, 0))
        for r in top
    ]
    fig, ax = plt.subplots(figsize=(12, 5))
    idx = list(range(len(top)))
    ax.bar(idx, qjs, label="QuickJS malloc", color="#1f77b4")
    ax.bar(
        idx,
        residual,
        bottom=qjs,
        label="Residual (RSS - QuickJS malloc)",
        color="#ff7f0e",
    )
    ax.set_xticks(idx)
    ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.set_ylabel("MB")
    ax.set_title("Top RSS configs: QuickJS malloc + residual")
    ax.legend()
    _save_plot(fig, out_path)
    plt.close(fig)


def _plot_phase_deltas(rows: list[ExperimentResult], out_path: Path, plt: Any) -> None:
    valid = [r for r in rows if not r.error]
    top = sorted(valid, key=lambda r: r.after_payload_rss_bytes, reverse=True)[:6]
    labels = [f"r{r.runtimes}-c{r.contexts_per_runtime}" for r in top]
    payload = [_mb(r.after_payload_rss_bytes) for r in top]
    gc_rss = [_mb(r.after_gc_rss_bytes) for r in top]
    close = [_mb(r.after_close_rss_bytes) for r in top]
    x = list(range(len(top)))
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(x, payload, marker="o", label="after_payload_rss")
    ax.plot(x, gc_rss, marker="o", label="after_gc_rss")
    ax.plot(x, close, marker="o", label="after_close_rss")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("MB")
    ax.set_title("Whole-process RSS by lifecycle phase (highest-pressure configs)")
    ax.legend()
    _save_plot(fig, out_path)
    plt.close(fig)


def _plot_heatmap(rows: list[ExperimentResult], out_path: Path, plt: Any) -> str:
    valid = [r for r in rows if not r.error]
    if not valid:
        return "no valid rows"
    target_limit = max(r.memory_limit_mb for r in valid)
    target_payload = max(r.payload_mb_per_context for r in valid)
    subset = [
        r
        for r in valid
        if r.memory_limit_mb == target_limit
        and r.payload_mb_per_context == target_payload
    ]
    if not subset:
        return "subset empty"
    runtimes = sorted({r.runtimes for r in subset})
    contexts = sorted({r.contexts_per_runtime for r in subset})
    mat: list[list[float]] = []
    for c in contexts:
        row_vals: list[float] = []
        for r in runtimes:
            hit = next(
                (
                    x
                    for x in subset
                    if x.runtimes == r and x.contexts_per_runtime == c
                ),
                None,
            )
            row_vals.append(_mb(hit.after_payload_rss_bytes) if hit else float("nan"))
        mat.append(row_vals)

    fig, ax = plt.subplots(figsize=(8, 5))
    im = ax.imshow(mat, cmap="magma", aspect="auto")
    ax.set_xticks(range(len(runtimes)))
    ax.set_xticklabels([str(x) for x in runtimes])
    ax.set_yticks(range(len(contexts)))
    ax.set_yticklabels([str(x) for x in contexts])
    ax.set_xlabel("runtimes")
    ax.set_ylabel("contexts_per_runtime")
    ax.set_title(
        "RSS heatmap (MB)\n"
        f"memory_limit={target_limit}MB payload={target_payload}MB/context"
    )
    for i, row_vals in enumerate(mat):
        for j, value in enumerate(row_vals):
            if math.isnan(value):
                text = "-"
            else:
                text = f"{value:.1f}"
            ax.text(j, i, text, ha="center", va="center", color="white", fontsize=8)
    fig.colorbar(im, ax=ax, label="after_payload_rss (MB)")
    _save_plot(fig, out_path)
    plt.close(fig)
    return (
        f"memory_limit={target_limit}MB,payload={target_payload}MB/context,"
        f"grid={len(contexts)}x{len(runtimes)}"
    )


def build_visual_markdown(
    rows: list[ExperimentResult],
    heatmap_meta: str,
    out_dir: Path,
) -> str:
    valid = [r for r in rows if not r.error]
    if not valid:
        return "### Memory Visual Report\n\nNo valid rows available.\n"
    peak = max(valid, key=lambda r: r.after_payload_rss_bytes)
    peak_residual = max(
        valid,
        key=lambda r: max(r.after_payload_rss_bytes - r.quickjs_malloc_size_after_payload, 0),
    )
    lines = [
        "### Memory Visual Report",
        "",
        f"- Rows: `{len(rows)}` total, `{len(valid)}` valid",
        (
            "- Peak whole-process RSS config: "
            f"`runtimes={peak.runtimes}, contexts_per_runtime={peak.contexts_per_runtime}, "
            f"memory_limit_mb={peak.memory_limit_mb}, "
            f"payload_mb_per_context={peak.payload_mb_per_context}` "
            f"-> `{_mb(peak.after_payload_rss_bytes):.2f} MB`"
        ),
        (
            "- Largest RSS residual (`rss - quickjs_malloc`) after payload: "
            f"`{_mb(max(peak_residual.after_payload_rss_bytes - peak_residual.quickjs_malloc_size_after_payload, 0)):.2f} MB` "
            f"at `runtimes={peak_residual.runtimes}, "
            f"contexts_per_runtime={peak_residual.contexts_per_runtime}`"
        ),
        f"- Heatmap slice: `{heatmap_meta}`",
        "",
        "Generated plots (saved as workflow artifacts):",
        f"- `{out_dir / 'rss_vs_qjs_scatter.png'}`",
        f"- `{out_dir / 'overhead_stacked_top.png'}`",
        f"- `{out_dir / 'phase_rss_lines_top.png'}`",
        f"- `{out_dir / 'rss_heatmap_slice.png'}`",
        "",
    ]
    return "\n".join(lines)


def generate_visual_report(
    rows: list[ExperimentResult],
    *,
    output_plots_dir: Path,
    output_visual_markdown: Path,
) -> str:
    try:
        import matplotlib
    except ImportError as exc:
        raise RuntimeError(
            "matplotlib is required when using --output-plots-dir. "
            "Install bench extras (pip install -e '.[bench]') or "
            "add matplotlib to your environment."
        ) from exc

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output_plots_dir.mkdir(parents=True, exist_ok=True)
    _plot_scatter(rows, output_plots_dir / "rss_vs_qjs_scatter.png", plt)
    _plot_overhead_stack(rows, output_plots_dir / "overhead_stacked_top.png", plt)
    _plot_phase_deltas(rows, output_plots_dir / "phase_rss_lines_top.png", plt)
    heatmap_meta = _plot_heatmap(rows, output_plots_dir / "rss_heatmap_slice.png", plt)

    report = build_visual_markdown(rows, heatmap_meta, output_plots_dir)
    output_visual_markdown.parent.mkdir(parents=True, exist_ok=True)
    output_visual_markdown.write_text(report, encoding="utf-8")
    return report


def summarize_markdown(rows: list[ExperimentResult]) -> str:
    if not rows:
        return "No rows produced.\n"

    by_peak = sorted(rows, key=lambda r: r.after_payload_rss_bytes, reverse=True)
    top = by_peak[: min(12, len(by_peak))]
    lines = [
        "### Memory Profiling Summary",
        "",
        (
            "| runtimes | ctx/runtime | limit_mb | payload_mb/ctx | "
            "rss_after_payload_mb | qjs_malloc_mb | "
            "est_max_runtimes_same_mix | over_cap | error |"
        ),
        "|---:|---:|---:|---:|---:|---:|---:|:---:|:---|",
    ]
    for row in top:
        lines.append(
            "| "
            f"{row.runtimes} | "
            f"{row.contexts_per_runtime} | "
            f"{row.memory_limit_mb} | "
            f"{row.payload_mb_per_context} | "
            f"{to_mb(row.after_payload_rss_bytes):.2f} | "
            f"{to_mb(row.quickjs_malloc_size_after_payload):.2f} | "
            f"{row.estimated_max_runtimes_same_mix} | "
            f"{'yes' if row.over_cap_after_payload else 'no'} | "
            f"{row.error or ''} |"
        )
    return "\n".join(lines) + "\n"


def build_configs(args: argparse.Namespace) -> list[ExperimentConfig]:
    runtimes = parse_int_csv(args.runtimes)
    contexts_per_runtime = parse_int_csv(args.contexts_per_runtime)
    memory_limits = parse_int_csv(args.memory_limit_mb)
    payloads = parse_int_csv(args.payload_mb_per_context)

    configs: list[ExperimentConfig] = []
    for r in runtimes:
        for c in contexts_per_runtime:
            for m in memory_limits:
                for p in payloads:
                    cfg = ExperimentConfig(
                        runtimes=r,
                        contexts_per_runtime=c,
                        memory_limit_mb=m,
                        payload_mb_per_context=p,
                    )
                    if cfg.total_contexts > args.max_total_contexts:
                        continue
                    configs.append(cfg)
    return configs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run runtime/context memory-pressure experiments for quickjs-rs."
    )
    parser.add_argument("--runtimes", default=DEFAULT_RUNTIMES)
    parser.add_argument(
        "--contexts-per-runtime",
        dest="contexts_per_runtime",
        default=DEFAULT_CONTEXTS_PER_RUNTIME,
    )
    parser.add_argument("--memory-limit-mb", default=DEFAULT_MEMORY_LIMIT_MB)
    parser.add_argument(
        "--payload-mb-per-context",
        default=DEFAULT_PAYLOAD_MB_PER_CONTEXT,
    )
    parser.add_argument(
        "--process-cap-mb",
        type=int,
        default=DEFAULT_PROCESS_CAP_MB,
        help="Process memory cap used for theoretical bound calculations.",
    )
    parser.add_argument(
        "--max-total-contexts",
        type=int,
        default=512,
        help="Safety guard to avoid accidentally spawning extreme context counts.",
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=Path("artifacts/memory/memory-profile.csv"),
    )
    parser.add_argument(
        "--output-markdown",
        type=Path,
        default=None,
        help="Optional markdown summary output path.",
    )
    parser.add_argument(
        "--output-plots-dir",
        type=Path,
        default=None,
        help="Optional matplotlib output directory (enables plot generation).",
    )
    parser.add_argument(
        "--output-visual-markdown",
        type=Path,
        default=None,
        help=(
            "Optional markdown path for plot-based visual report. "
            "If omitted while plots are enabled, defaults to "
            "artifacts/memory/memory-report.md."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configs = build_configs(args)
    if not configs:
        raise SystemExit("No configs generated after applying filters.")

    process_cap_bytes = args.process_cap_mb * MB
    rows = [run_single_config(cfg, process_cap_bytes=process_cap_bytes) for cfg in configs]
    write_csv(args.output_csv, rows)
    summary = summarize_markdown(rows)
    print(summary)
    print(f"CSV written to: {args.output_csv}")

    if args.output_markdown is not None:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(summary, encoding="utf-8")
        print(f"Markdown summary written to: {args.output_markdown}")

    if args.output_plots_dir is not None:
        visual_md = args.output_visual_markdown
        if visual_md is None:
            visual_md = Path("artifacts/memory/memory-report.md")
        visual_report = generate_visual_report(
            rows,
            output_plots_dir=args.output_plots_dir,
            output_visual_markdown=visual_md,
        )
        print(visual_report)
        print(f"Visual report markdown written to: {visual_md}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
