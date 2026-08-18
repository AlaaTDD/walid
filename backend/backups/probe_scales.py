"""Sixth probe: compare pocket bounding-box dimensions against unplaced part
bounding-box dimensions directly, to confirm the max_inscribed_diameter_mm
necessary-not-sufficient shape check is what's actually failing here (not
just area). Also directly attempt to place the smallest unplaced part into
the current baseline layout via the SAME exact primitives the engine uses,
to see exactly where/why it fails.
"""
import sys
sys.path.insert(0, ".")

from app.nesting.benchmark import generate_benchmark_parts
from app.nesting.engine import run_best_single_sheet_nesting, _sheet_polygon, _prepare_rotations
from app.nesting.metrics import free_space_from_placed_parts

SHEET_W = SHEET_H = 100.0
MARGIN = 5.0
CLEARANCE = 4.10
PART_COUNT = 20
seed = 5

parts = generate_benchmark_parts(seed, part_count=PART_COUNT)
baseline = run_best_single_sheet_nesting(
    parts, SHEET_W, SHEET_H, sheet_margin_mm=MARGIN, clearance_mm=CLEARANCE,
)
usable = _sheet_polygon(SHEET_W, SHEET_H, MARGIN)
free_space = free_space_from_placed_parts(usable, baseline.placed, clearance_mm=CLEARANCE)

print("=== Pockets (sorted by area, largest first) ===")
for pocket in sorted(free_space.pockets, key=lambda p: -p.area_mm2):
    minx, miny, maxx, maxy = pocket.polygon_mm.bounds
    print(
        f"  area={pocket.area_mm2:7.1f}mm2  bbox={maxx-minx:.1f}x{maxy-miny:.1f}mm  "
        f"max_inscribed_diameter={pocket.max_inscribed_diameter_mm:.1f}mm  "
        f"touches_boundary={pocket.touches_boundary}  compactness={pocket.compactness:.3f}"
    )

print()
print("=== Unplaced part bounding-box dimensions (smallest area first) ===")
unplaced_sorted = sorted(baseline.unplaced_part_ids, key=lambda pid: parts[pid].shape_mm.area)
for pid in unplaced_sorted:
    shape = parts[pid].shape_mm
    minx, miny, maxx, maxy = shape.bounds
    w, h = maxx - minx, maxy - miny
    min_dim = min(w, h)
    print(f"  part {pid}: area={shape.area:.1f}mm2  bbox={w:.1f}x{h:.1f}mm  min_bbox_dim={min_dim:.1f}mm")

print()
largest_pocket_diam = max(p.max_inscribed_diameter_mm for p in free_space.pockets)
print(f"largest pocket's max_inscribed_diameter: {largest_pocket_diam:.1f}mm")
smallest_unplaced_min_dim = min(
    min(parts[pid].shape_mm.bounds[2]-parts[pid].shape_mm.bounds[0],
        parts[pid].shape_mm.bounds[3]-parts[pid].shape_mm.bounds[1])
    for pid in baseline.unplaced_part_ids
)
print(f"smallest unplaced part's smallest bbox dimension: {smallest_unplaced_min_dim:.1f}mm")
print(f"Can ANY pocket geometrically admit the smallest-dimension unplaced part (necessary condition)? {largest_pocket_diam >= smallest_unplaced_min_dim}")
