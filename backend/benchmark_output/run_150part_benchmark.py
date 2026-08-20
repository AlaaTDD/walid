"""Real performance benchmark at production scale (150 parts, 790x1190mm sheet).

Uses the project's own existing, already-tested nesting/benchmark.py harness
(run_one_seed) rather than duplicating its logic. Part count (150) and sheet
size (790x1190mm) are taken directly from real evidence: 16 of 17 stored
production jobs under backend/jobs/ have exactly 151 uploaded parts, and
790x1190mm is the app's own documented default sheet size (see
web/src/lib/constants.ts defaultSheetWidthMm/defaultSheetHeightMm).

This measures wall-clock time for each pipeline stage separately (greedy
placement, LNS optimization, compaction) plus the final validate_layout
re-check, so a report can distinguish "greedy placement is fine but LNS is
slow" from "the whole pipeline is slow" -- useful information a single
end-to-end number would hide.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.nesting.benchmark import generate_benchmark_parts
from app.nesting.collision import validate_layout
from app.nesting.compaction import compact_layout
from app.nesting.engine import _sheet_polygon, run_best_single_sheet_nesting
from app.nesting.lns import run_lns_optimization
from app.nesting.metrics import free_space_from_placed_parts, score_layout

SHEET_WIDTH_MM = 790.0
SHEET_HEIGHT_MM = 1190.0
SHEET_MARGIN_MM = 5.0
CLEARANCE_MM = 4.10
PART_COUNT = 150  # matches 16/17 real stored production jobs (151 uploads each)
SEEDS = [1, 2, 3]
LNS_ITERATIONS = 40  # reduced from benchmark.py's default 60 to bound total wall-clock

results = []

for seed in SEEDS:
    print(f"=== seed={seed} ===", flush=True)
    parts = generate_benchmark_parts(seed, part_count=PART_COUNT)
    usable_area = _sheet_polygon(SHEET_WIDTH_MM, SHEET_HEIGHT_MM, SHEET_MARGIN_MM)
    usable_area_mm2 = usable_area.area

    t0 = time.perf_counter()
    baseline = run_best_single_sheet_nesting(
        parts, SHEET_WIDTH_MM, SHEET_HEIGHT_MM,
        sheet_margin_mm=SHEET_MARGIN_MM, clearance_mm=CLEARANCE_MM,
    )
    greedy_elapsed = time.perf_counter() - t0
    print(f"  greedy: {greedy_elapsed:.1f}s placed={len(baseline.placed)}/{PART_COUNT}", flush=True)

    t0 = time.perf_counter()
    baseline_report = validate_layout(
        baseline.placed, SHEET_WIDTH_MM, SHEET_HEIGHT_MM, SHEET_MARGIN_MM, clearance_mm=CLEARANCE_MM
    )
    validate_elapsed = time.perf_counter() - t0
    print(f"  validate_layout (baseline): {validate_elapsed:.3f}s valid={baseline_report.is_valid} pairs_checked={baseline_report.checked_pairs_count}", flush=True)

    t0 = time.perf_counter()
    lns_result = run_lns_optimization(
        baseline, parts, SHEET_WIDTH_MM, SHEET_HEIGHT_MM,
        sheet_margin_mm=SHEET_MARGIN_MM, clearance_mm=CLEARANCE_MM,
        max_iterations=LNS_ITERATIONS, destroy_fraction=0.15, seed=seed,
    )
    lns_elapsed = time.perf_counter() - t0
    print(f"  lns ({LNS_ITERATIONS} iters): {lns_elapsed:.1f}s placed={len(lns_result.best.placed)}/{PART_COUNT}", flush=True)

    t0 = time.perf_counter()
    compaction_result = compact_layout(
        lns_result.best, SHEET_WIDTH_MM, SHEET_HEIGHT_MM,
        sheet_margin_mm=SHEET_MARGIN_MM, clearance_mm=CLEARANCE_MM,
    )
    compaction_elapsed = time.perf_counter() - t0
    print(f"  compaction: {compaction_elapsed:.1f}s moved={compaction_result.moved_count}", flush=True)

    pipeline = compaction_result.result

    t0 = time.perf_counter()
    pipeline_report = validate_layout(
        pipeline.placed, SHEET_WIDTH_MM, SHEET_HEIGHT_MM, SHEET_MARGIN_MM, clearance_mm=CLEARANCE_MM
    )
    final_validate_elapsed = time.perf_counter() - t0
    print(f"  validate_layout (final): {final_validate_elapsed:.3f}s valid={pipeline_report.is_valid} pairs_checked={pipeline_report.checked_pairs_count}", flush=True)

    baseline_free = free_space_from_placed_parts(usable_area, baseline.placed, clearance_mm=CLEARANCE_MM)
    pipeline_free = free_space_from_placed_parts(usable_area, pipeline.placed, clearance_mm=CLEARANCE_MM)
    baseline_util = sum(float(p.placed_shape_mm.area) for p in baseline.placed) / usable_area_mm2
    pipeline_util = sum(float(p.placed_shape_mm.area) for p in pipeline.placed) / usable_area_mm2

    total_elapsed = greedy_elapsed + validate_elapsed + lns_elapsed + compaction_elapsed + final_validate_elapsed

    results.append({
        "seed": seed,
        "part_count": PART_COUNT,
        "greedy_seconds": round(greedy_elapsed, 2),
        "validate_baseline_seconds": round(validate_elapsed, 4),
        "lns_seconds": round(lns_elapsed, 2),
        "compaction_seconds": round(compaction_elapsed, 2),
        "validate_final_seconds": round(final_validate_elapsed, 4),
        "total_pipeline_seconds": round(total_elapsed, 2),
        "baseline_placed_count": len(baseline.placed),
        "pipeline_placed_count": len(pipeline.placed),
        "baseline_valid": baseline_report.is_valid,
        "pipeline_valid": pipeline_report.is_valid,
        "baseline_utilization": round(baseline_util, 4),
        "pipeline_utilization": round(pipeline_util, 4),
        "baseline_free_area_mm2": round(baseline_free.total_free_area_mm2, 1),
        "pipeline_free_area_mm2": round(pipeline_free.total_free_area_mm2, 1),
    })
    print(f"  TOTAL: {total_elapsed:.1f}s", flush=True)

output_path = Path(__file__).parent / "150part_benchmark_results.json"
output_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
print(f"\nWrote results to {output_path}", flush=True)
print("\n=== SUMMARY ===", flush=True)
for r in results:
    print(json.dumps(r, ensure_ascii=False), flush=True)
