"""Seventh probe: test whether run_local_reoptimization (the dedicated,
isolated-region-around-worst-pocket loop, already implemented and wired
after LNS+compaction in main.py) does any better at this same fragmentation
scenario, since it's specifically designed to target one bad pocket with a
tunable isolation radius rather than LNS's fixed small global destroy count.
"""
import sys
sys.path.insert(0, ".")

from app.nesting.benchmark import generate_benchmark_parts
from app.nesting.engine import run_best_single_sheet_nesting
from app.nesting.lns import run_lns_optimization, run_local_reoptimization
from app.nesting.compaction import compact_layout

SHEET_W = SHEET_H = 100.0
MARGIN = 5.0
CLEARANCE = 4.10
PART_COUNT = 20
seed = 5

parts = generate_benchmark_parts(seed, part_count=PART_COUNT)
baseline = run_best_single_sheet_nesting(
    parts, SHEET_W, SHEET_H, sheet_margin_mm=MARGIN, clearance_mm=CLEARANCE,
)
print(f"baseline placed={len(baseline.placed)}")

lns_result = run_lns_optimization(
    baseline, parts, SHEET_W, SHEET_H,
    sheet_margin_mm=MARGIN, clearance_mm=CLEARANCE,
    max_iterations=60, destroy_fraction=0.15, seed=seed,
)
print(f"after LNS placed={len(lns_result.best.placed)}")

compaction_result = compact_layout(
    lns_result.best, SHEET_W, SHEET_H,
    sheet_margin_mm=MARGIN, clearance_mm=CLEARANCE,
)
print(f"after compaction placed={len(compaction_result.result.placed)}")

# Try several isolation radii -- the docstring mentions this is a tunable
# parameter and its default may not suit this scale.
for radius in (25.0, 40.0, 60.0, 90.0):
    local_result = run_local_reoptimization(
        compaction_result.result, parts, SHEET_W, SHEET_H,
        sheet_margin_mm=MARGIN, clearance_mm=CLEARANCE,
        max_rounds=5, isolation_radius_mm=radius, seed=seed, time_budget_seconds=30.0,
    )
    print(
        f"radius={radius:5.1f}mm  placed={len(local_result.best.placed)}  "
        f"rounds_run={local_result.rounds_run}  improved={local_result.improved}"
    )
