# Phase 1 — Architecture Analysis (verified by direct code inspection, 2026-08-18)

## Verified pipeline (main.py:926-1032)
run_best_single_sheet_nesting -> run_lns_optimization (seed=42, max_iter=10, budget=60s)
-> compact_layout -> validate_layout. Matches spec section 3 flow exactly.

## packing_attempts = 1 lock
- api/schemas.py:60 -> Field(default=1, ge=1, le=1). API-level lock is real.
- engine.py `_PACKING_STRATEGIES` tuple (line 31-38) still holds 6 strategy
  entries, NOT 1. Operational effect is already correct (only entry [0] is
  ever sliced since packing_attempts<=1 everywhere it's called from main.py),
  but the tuple itself contradicts spec section 5 literally ("_PACKING_STRATEGIES
  يجب أن تحتوي على strategy واحدة فقط"). Dead entries: 5 of 6.
- engine.py:PartInput-level functions (run_nesting, run_multi_sheet_nesting,
  run_best_single_sheet_nesting) still accept packing_attempts as a plain
  int param with NO le=1 constraint of their own -- only schemas.py enforces it.
  tests/test_nesting_capacity.py calls these directly with packing_attempts=5,
  bypassing the API lock entirely.

## LNS (lns.py) -- confirmed by full read
- 3 destroy operators only: random, worst_gap_adjacent, smallest (matches
  table). NO neighborhood/similarity/cluster destruction.
- Operator selection: `rng.choice(operator_names)` (line 500) -- uniform
  random, NO adaptive/scored selection. Spec section 12 gap confirmed real.
- Repair: largest-first re-placement via engine's own exact primitives.
  Every candidate re-validated with validate_layout before acceptance
  (never-worse guarantee, checked not assumed).
- Acceptance: real simulated annealing (_accept, line 332), cooling_rate
  applied per iteration. Matches spec section 13.
- Deterministic given seed (rng constructed once from seed, no other
  randomness source). Matches spec section 14.

## Compaction (compaction.py) -- confirmed by full read
- 4-direction (left/down/right/up) exact bisection slide, multi-pass until
  convergence. Acceptance gated on free-area monotonicity (provable
  geometric fact for pure translation), NOT on score_layout.total --
  documented reasoning in code (line 319-341) is sound: score_layout's
  compactness_bonus can penalize a strictly-better translation.
- This is genuinely different from LNS destroy/repair (membership fixed,
  translation only) -- correctly separated per spec's Geometry/Optimization
  layer split.

## Objective function (metrics.py) -- confirmed by full read
- ObjectiveWeights: placed_count=1000, utilization=100, fragmentation_penalty=20,
  enclosed_penalty=15, compactness_bonus=10. Centralized, documented per-field
  (spec section 9 requirement met).
- Gap vs spec section 9: "largest empty region" (item 4) and "boundary
  efficiency" (item 8) are NOT separate weighted terms in score_layout,
  though largest_pocket_area_mm2 exists as an unused FreeSpaceAnalysis
  property. Partial gap, not a full miss.
- `_single_sheet_quality` (engine.py:1454, old placed-count+bbox tiebreak)
  still exists standalone, now operating over a 1-candidate list post-lock
  -> effectively a no-op tiebreak, not deleted, not wrong, just now inert.

## Rotation (rotation.py) -- confirmed by full read
- 24 locked angles (0,15,...,345), fully documented rationale in Arabic
  docstring re: floating-point tradeoff at non-cardinal angles.
- This IS the "coarse rotation search" spec section 7 asks for (was 4
  angles before, matches historical evidence in tests' own comments).
- NO fine rotation refinement anywhere (no continuous/local angle search
  around a good placement). Confirmed gap, matches table.

## NFP (nfp.py) -- confirmed by full read
- Exact Minkowski sum via GEOS triangle-pair union, no discretization.
  Well-documented Arabic docstring citing the mathematical definition.
  This is the correct geometric foundation per spec's "don't replace
  working geometry" rule -- keep as-is.
- Known, pre-existing, documented GEOS robustness limitation on this exact
  path: ~30% of seeded benchmark runs hit GEOSException or a ~1.2-micron
  clearance shortfall (benchmark.py:35-59, KnownGeosLimitation). Explicitly
  out-of-scope per prior task determination. Relevant context for any new
  benchmarking I run -- not a regression to fix now.

## Fast path is the ONLY path actually used (critical finding)
- `_should_use_fast_candidate_path` (engine.py:206) returns `True`
  unconditionally. The `not use_fast_candidate_path` branch (exact-NFP
  early-stop via `_has_any_remaining_fit`) is dead code in both
  run_nesting AND lns.py's _repair -- never reached from any current
  caller.
- Fast path IS geometrically exact where it matters: bounding-box distance
  is used only as a provable-safe early ACCEPT (envelopes >= clearance
  apart => contours >= clearance apart, a mathematical fact, not an
  approximation), falling through to real GEOS `.dwithin()` for any
  ambiguous envelope overlap (_resolve_ambiguous_candidate, line 758-773).
  No accuracy sacrificed. Matches spec section 20's "fast path=optimization,
  exact path=authority" principle correctly, just via a different exact/
  fast split than the module's own `_place_one_part` (occupied-zone NFP)
  vs `_place_one_part_fast` (STRtree distance) naming suggests.

## Test suite baseline (RUN, not assumed) -- 67 passed, 2 failed
Command: .venv/bin/python3 -m pytest tests/ -v --tb=short (148.8s wall clock)
Full log: .claude-surgical/verification/baseline_full_run.json

FAIL 1: test_nesting_stops_when_no_remaining_shape_can_fit
  Expects processed_count==3, actual=10. Root cause (verified via isolated
  repro script): early-stop via _has_any_remaining_fit only fires when
  `not use_fast_candidate_path` (engine.py:1263) -- unreachable given fast
  path is always True. placed_count (2) and sheet_full (True) ARE correct
  in both expected and actual -- only the internal "how many were tried
  before giving up" bookkeeping differs. NOT a geometric correctness bug;
  a stale assertion against a code path (`processed_count` semantics under
  fast-path) that changed after the 4->24 rotation expansion made fast path
  universal in practice, before the assertion was updated to match.

FAIL 2: test_multi_attempt_packing_fills_mixed_dimensions_more_densely
  Calls run_multi_sheet_nesting directly with packing_attempts=5 and
  asserts 5-strategy result (7 parts) beats 1-strategy result (5 parts).
  This test's own premise (multi-attempt greedy search finds a denser
  layout) is EXACTLY the approach spec section 5 calls "مرفوض تمامًا"
  (rejected entirely) in favor of LNS-based single-attempt + optimization.
  This test currently contradicts the spec's own architectural mandate,
  not a code defect -- it's testing for the old (rejected) behavior.
  Bypasses the API's packing_attempts<=1 lock by calling engine.py directly.

## Confirmed spec gaps (Phase 2 candidates, priority as originally stated
## in prompet.md section 31, re-confirmed against actual code):
1. Additional destroy operators (neighborhood, similarity-based, cluster) - HIGH
2. Adaptive operator selection (currently uniform rng.choice) - HIGH
3. Fine rotation refinement (24-angle grid is coarse-only, no local search) - MED
4. Local re-optimization as an explicit targeted loop (currently folded into
   worst_gap_adjacent as one of 3 equally-weighted random-picked operators,
   not a dedicated "find worst region -> repeatedly improve it -> move on"
   loop as spec section 5 literally describes) - MED
5. Local exact optimization (MIP/CP for small hard neighborhoods) - LOW
6. `_PACKING_STRATEGIES` tuple still has 6 entries though only 1 is ever
   used operationally -- literal-compliance gap, zero behavioral impact - LOW
7. Objective function missing explicit largest-empty-region and
   boundary-efficiency terms (partial gap vs spec section 9's 9-item list) - LOW-MED

## Pre-existing test failures needing a decision before any Phase 4 work:
- Both failures predate any change I've made. Neither is a geometric
  correctness regression. Both need explicit disposition (update stale
  assertion vs the test asserting rejected old behavior) before touching
  engine.py/lns.py, so a future test run can distinguish "I broke something"
  from "this was already broken."
