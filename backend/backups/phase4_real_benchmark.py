import sys
import time
import json
from pathlib import Path

BACKEND = "/Volumes/alaassD/walid/backend"
sys.path.insert(0, BACKEND)

from app.nesting.benchmark import run_one_seed, run_benchmark, render_before_after_diff

print("=== Phase 4: calibration probe (1 seed, full default scale) ===", flush=True)
probe_started = time.perf_counter()
probe = run_one_seed(seed=9001)  # defaults: part_count=40, 300x300mm, clearance=4.10, lns_iterations=60
probe_elapsed = time.perf_counter() - probe_started
print(f"probe seed=9001 elapsed={probe_elapsed:.2f}s "
      f"baseline_placed={probe.baseline_placed_count} pipeline_placed={probe.pipeline_placed_count} "
      f"baseline_util={probe.baseline_utilization:.4f} pipeline_util={probe.pipeline_utilization:.4f} "
      f"pipeline_valid={probe.pipeline_valid}", flush=True)

# Adaptive N: keep the full run within a reasonable wall-clock budget.
# Target roughly 8-10 minutes total for the statistical run.
TARGET_TOTAL_SECONDS = 550.0
n_seeds = max(10, min(30, int(TARGET_TOTAL_SECONDS / max(probe_elapsed, 1.0))))
print(f"Chosen N={n_seeds} seeds based on probe timing.", flush=True)

seeds = list(range(1, n_seeds + 1))
print(f"=== Running full statistical benchmark: seeds={seeds} ===", flush=True)
run_started = time.perf_counter()
report = run_benchmark(seeds)
run_elapsed = time.perf_counter() - run_started
print(f"Full benchmark run complete in {run_elapsed:.2f}s", flush=True)

print("\n" + "=" * 70, flush=True)
print(report.summary_text(), flush=True)
print("=" * 70, flush=True)

# Persist a structured, durable record (numeric fields only -- geometry
# objects are not trivially JSON-serializable and are not needed for the
# statistical report itself).
output_dir = Path(BACKEND) / "benchmark_output" / "phase4_final_report"
output_dir.mkdir(parents=True, exist_ok=True)

per_seed = []
for r in report.seed_results:
    per_seed.append({
        "seed": r.seed,
        "part_count": r.part_count,
        "baseline_placed_count": r.baseline_placed_count,
        "pipeline_placed_count": r.pipeline_placed_count,
        "baseline_utilization": r.baseline_utilization,
        "pipeline_utilization": r.pipeline_utilization,
        "utilization_delta": r.pipeline_utilization - r.baseline_utilization,
        "baseline_score": r.baseline_score,
        "pipeline_score": r.pipeline_score,
        "baseline_free_area_mm2": r.baseline_free_area_mm2,
        "pipeline_free_area_mm2": r.pipeline_free_area_mm2,
        "pipeline_valid": r.pipeline_valid,
        "elapsed_seconds": r.elapsed_seconds,
    })

def stats_dict(s):
    return {"best": s.best, "worst": s.worst, "mean": s.mean, "median": s.median, "stdev": s.stdev}

structured_report = {
    "n_seeds_requested": n_seeds,
    "seeds_requested": seeds,
    "excluded_seeds_pipeline_invalid": list(report.excluded_seeds),
    "excluded_geos_limitation_seeds": [{"seed": s, "reason": reason} for s, reason in report.excluded_geos_limitation_seeds],
    "n_seeds_included_in_stats": len(report.seed_results) - len(report.excluded_seeds),
    "baseline_utilization_stats": stats_dict(report.baseline_utilization_stats),
    "pipeline_utilization_stats": stats_dict(report.pipeline_utilization_stats),
    "utilization_delta_stats": stats_dict(report.utilization_delta_stats),
    "baseline_placed_count_stats": stats_dict(report.baseline_placed_count_stats),
    "pipeline_placed_count_stats": stats_dict(report.pipeline_placed_count_stats),
    "placed_count_delta_stats": stats_dict(report.placed_count_delta_stats),
    "total_elapsed_seconds": report.total_elapsed_seconds,
    "per_seed": per_seed,
    "calibration_probe": {
        "seed": probe.seed,
        "elapsed_seconds": probe_elapsed,
        "baseline_placed_count": probe.baseline_placed_count,
        "pipeline_placed_count": probe.pipeline_placed_count,
    },
}

json_path = output_dir / "report.json"
json_path.write_text(json.dumps(structured_report, indent=2, ensure_ascii=False))
print(f"\nSaved structured JSON report to {json_path}", flush=True)

text_path = output_dir / "report.txt"
text_path.write_text(report.summary_text())
print(f"Saved plain-text summary to {text_path}", flush=True)

# Persist before/after/diff visuals for a representative sample of seeds
# (not all N, to keep output reasonable) -- first, middle, last of the
# INCLUDED results, plus the single worst and single best delta for a
# concrete visual of both ends of the outcome distribution.
included = [r for r in report.seed_results if r.pipeline_valid]
included_sorted_by_delta = sorted(included, key=lambda r: r.pipeline_utilization - r.baseline_utilization)
sample_results = {
    "worst_delta": included_sorted_by_delta[0],
    "median_delta": included_sorted_by_delta[len(included_sorted_by_delta) // 2],
    "best_delta": included_sorted_by_delta[-1],
}
print("\nRendering before/after/diff PNGs for representative seeds...", flush=True)
for label, r in sample_results.items():
    before_p, after_p, diff_p = render_before_after_diff(r, 300.0, 300.0, output_dir)
    delta = r.pipeline_utilization - r.baseline_utilization
    print(f"  {label}: seed={r.seed} delta={delta:+.4f} -> {before_p.name}, {after_p.name}, {diff_p.name}", flush=True)

print("\n=== DONE ===", flush=True)
