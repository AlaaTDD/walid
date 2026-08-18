import cProfile
import pstats
import time
import uuid

# Setup the exact same LNS call that is failing
from app.nesting.engine import run_nesting
from app.nesting.lns import run_lns_optimization
from app.schemas.project import ProjectSettings
from app.schemas.nesting import LnsIterationLog
from app.nesting.metrics import DEFAULT_OBJECTIVE_WEIGHTS
from app.image import prepare_part_inputs
import json
import os

def run():
    # Load the job data
    parts_mm = prepare_part_inputs(
        job_id="8170c967-045e-4202-9511-83bee48da526",
        dpi=300,
        settings=ProjectSettings()
    )
    
    print("Running initial nesting...")
    t0 = time.time()
    result = run_nesting(
        parts_mm=parts_mm,
        sheet_width_mm=790.0,
        sheet_height_mm=1190.0,
        sheet_margin_mm=5.0,
        clearance_mm=4.10,
        packing_attempts=1,
        mode="exact_nfp", # Use exactly_validated_candidates under the hood since we reverted the flag
        placement_policy="bottom_left",
    )
    t1 = time.time()
    print(f"Initial nesting took {t1-t0:.2f}s, placed {len(result.placed)}")
    
    # Run LNS with profile
    print("Running LNS...")
    profiler = cProfile.Profile()
    profiler.enable()
    
    try:
        lns_result = run_lns_optimization(
            starting_result=result,
            parts_mm=parts_mm,
            sheet_width_mm=790.0,
            sheet_height_mm=1190.0,
            sheet_margin_mm=5.0,
            clearance_mm=4.10,
            placement_policy="bottom_left",
            max_iterations=1,
            destroy_fraction=0.15,
            objective_weights=DEFAULT_OBJECTIVE_WEIGHTS,
        )
    finally:
        profiler.disable()
        stats = pstats.Stats(profiler).sort_stats('cumtime')
        stats.print_stats(30)
        
if __name__ == "__main__":
    run()
