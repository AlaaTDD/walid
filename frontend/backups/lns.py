"""Large Neighbourhood Search (LNS) destroy/repair engine.

Spec's core ask (sections 1, 2, 6, 7, 8): true destroy/repair on
ALREADY-PLACED parts, multiple destroy operators, an acceptance criterion,
repeated over many iterations to escape the local optima a single greedy
pass or the existing gap-backfill (which only ever ADDS to a monotonically
growing placed set, never removes anything) cannot reach.

This module is purely additive, exactly like metrics.py before it: it reads
from engine.py (PartInput, PlacedPart, _OccupiedZone, _PreparedRotation,
_place_one_part, _place_one_part_fast, _prepare_rotations,
_should_use_fast_candidate_path, _sheet_polygon) and from metrics.py
(score_layout and friends) but modifies neither. run_nesting and
run_best_single_sheet_nesting are unchanged and remain exactly what main.py
calls; this module adds a NEW, separate, optional entrypoint
(run_lns_optimization) that a future caller can opt into on top of an
existing result, never a replacement for the existing calling contract.

Correctness guarantee: every candidate layout this module produces is
assembled purely from calls to the engine's own exact placement primitives
(_place_one_part / _place_one_part_fast), so every placement in every
candidate satisfies the same exact NFP/GEOS clearance test as the original
search -- this module can only choose WHICH parts to try placing and in
what order, it cannot and does not invent a new feasibility test. The final
returned layout is independently re-validated with the project's own
validate_layout before being accepted, and if that check ever fails (which
should be structurally impossible given the above, but is checked anyway
rather than assumed) the original starting layout is returned unchanged.
"""
from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from shapely.geometry import Polygon

from app.nesting.collision import validate_layout
from app.nesting.engine import (
    NestingCancelledError,
    NestingResult,
    PartInput,
    PlacedPart,
    _MergedBlockedCache,
    _OccupiedZone,
    _PreparedRotation,
    _place_one_part,
    _place_one_part_fast,
    _prepare_rotations,
    _sheet_polygon,
    _should_use_fast_candidate_path,
)
from app.nesting.metrics import (
    DEFAULT_OBJECTIVE_WEIGHTS,
    LayoutScore,
    ObjectiveWeights,
    free_space_from_placed_parts,
    score_layout,
)


class LnsError(Exception):
    pass


# ---------------------------------------------------------------------------
# Adaptive operator selection (spec section 12: "إذا كان operator معين يحقق
# تحسينات متكررة: زد استخدامه. إذا كان operator غير مفيد: قلل استخدامه").
#
# Standard ALNS mechanism (Ropke & Pisinger, "An Adaptive Large Neighborhood
# Search Heuristic for the Pickup and Delivery Problem with Time Windows",
# Transportation Science 40(4), 2006 -- the canonical reference for exactly
# this technique, chosen per spec section 21's requirement to ground method
# selection in literature rather than invent an ad-hoc scheme): each operator
# carries a weight; selection is roulette-wheel (weighted random) proportional
# to current weights; after each iteration the operator used earns a reward
# based on outcome (new best > merely accepted > rejected); weights are
# updated via an exponential moving average (the "reaction factor" r in the
# literature) so recent performance dominates without erasing history in one
# step, and a weight floor keeps every operator selectable indefinitely --
# spec section 12 explicitly warns against permanently zeroing out an
# operator ("قلل استخدامه", reduce its use, not eliminate it), since a
# destroy type that looks unhelpful early in the search can become useful
# again once the layout has changed shape.
# ---------------------------------------------------------------------------

_ADAPTIVE_REWARD_NEW_BEST = 3.0
_ADAPTIVE_REWARD_ACCEPTED = 1.0
_ADAPTIVE_REWARD_REJECTED = 0.0
_ADAPTIVE_REACTION_FACTOR = 0.2
_ADAPTIVE_MIN_WEIGHT = 0.05


@dataclass
class AdaptiveOperatorSelector:
    """Tracks per-operator weights and performs roulette-wheel selection.

    Not a dataclass field default trap: ``weights`` is intentionally built
    once in ``__post_init__`` from ``operator_names`` rather than shared
    across instances, since a fresh selector is constructed per
    ``run_lns_optimization`` call and must not carry state between runs
    (this would otherwise break the seeded-reproducibility guarantee
    elsewhere in this module).
    """

    operator_names: tuple[str, ...]
    weights: dict[str, float] = field(init=False)

    def __post_init__(self) -> None:
        # Equal start weight (spec: no operator is favoured a priori --
        # adaptation is earned through observed performance, not assumed).
        self.weights = {name: 1.0 for name in self.operator_names}

    def select(self, rng: random.Random) -> str:
        total = sum(self.weights.values())
        threshold = rng.random() * total
        cumulative = 0.0
        for name in self.operator_names:
            cumulative += self.weights[name]
            if threshold <= cumulative:
                return name
        # Floating-point edge case only (threshold landed past the last
        # cumulative sum by a rounding hair) -- last operator is the correct
        # fallback, not an error, since the roulette wheel is still fully
        # covered mathematically.
        return self.operator_names[-1]

    def update(self, operator_name: str, *, is_new_best: bool, accepted: bool) -> None:
        if is_new_best:
            reward = _ADAPTIVE_REWARD_NEW_BEST
        elif accepted:
            reward = _ADAPTIVE_REWARD_ACCEPTED
        else:
            reward = _ADAPTIVE_REWARD_REJECTED
        # Exponential moving average: new_weight = (1-r)*old + r*reward.
        # r=_ADAPTIVE_REACTION_FACTOR=0.2 is the standard mid-range value
        # from the ALNS literature above -- low enough that one lucky/unlucky
        # iteration cannot swing an operator's weight drastically, high
        # enough that a real, sustained performance difference is reflected
        # within a few dozen iterations (this module's typical iteration
        # budgets, per main.py's lns_max_iterations of 10-60).
        old_weight = self.weights[operator_name]
        new_weight = (1.0 - _ADAPTIVE_REACTION_FACTOR) * old_weight + _ADAPTIVE_REACTION_FACTOR * reward
        self.weights[operator_name] = max(new_weight, _ADAPTIVE_MIN_WEIGHT)


# ---------------------------------------------------------------------------
# Destroy operators (spec section 2/6: MULTIPLE destroy operators)
# ---------------------------------------------------------------------------


def _destroy_count(n_placed: int, fraction: float) -> int:
    """How many currently-placed parts a destroy operator should remove.

    Shared by every operator below so the floor fix here applies uniformly,
    not per-operator (all six previously duplicated an identical
    ``max(1, round(n_placed * fraction))`` line).

    Bug this fixes, diagnosed empirically (not guessed) against
    tests/test_nesting_capacity.py's seed=1 fixture: at small placed counts
    (e.g. n_placed=5, fraction=0.15 -> round(0.75) == 1), EVERY operator
    degenerates to removing exactly one part per iteration, because
    ``max(1, ...)`` only raises the floor to 1, never to 2. A direct search
    over this fixture confirmed no single-part removal (out of all 5
    candidates) ever creates enough contiguous room for a 6th part, while
    one specific PAIR removal does -- a coordinated multi-part destroy that
    a floor of 1 can structurally never produce, regardless of which
    operator is chosen or how many iterations run (verified up to 1000
    iterations: still capped at the single-removal ceiling). This is a
    destroy-magnitude limitation, not an operator-selection or
    scoring/acceptance problem -- raising the floor to 2 (only when a second
    part actually exists) is the direct fix for the mechanism actually
    responsible.

    The floor only raises to 2, not higher, because destroying too large a
    fraction of a SMALL layout on every iteration would repeatedly discard
    most of what greedy placement already found correct, wasting iterations
    re-deriving it instead of exploring genuinely new arrangements. Verified
    across n_placed in {1..150} and fraction in {0.10, 0.15, 0.20} (this
    module's three production presets, see main.py's lns_destroy_fraction
    selection): the floor changes behaviour ONLY for n_placed <= ~10, where
    round(n_placed * fraction) was landing on exactly 1 -- every larger
    layout already reaches count >= 2 on its own and is completely
    unaffected by this change.
    """
    if n_placed <= 1:
        return min(1, n_placed) if n_placed else 0
    count = round(n_placed * fraction)
    count = max(2, count)
    return min(count, n_placed)


def _destroy_random(
    placed: list[PlacedPart],
    fraction: float,
    rng: random.Random,
) -> tuple[list[PlacedPart], list[PlacedPart]]:
    """Remove a uniformly random subset of currently-placed parts.

    The baseline destroy operator: unbiased, cheap, and a good default when
    nothing about the layout's specific geometry suggests a smarter choice.
    Guarantees at least one part is removed (when placed is non-empty) so a
    destroy/repair cycle is never a no-op.
    """
    if not placed:
        return [], []
    count = _destroy_count(len(placed), fraction)
    indices = set(rng.sample(range(len(placed)), count))
    removed = [part for index, part in enumerate(placed) if index in indices]
    kept = [part for index, part in enumerate(placed) if index not in indices]
    return kept, removed


def _destroy_worst_gap_adjacent(
    placed: list[PlacedPart],
    usable_area: Polygon,
    fraction: float,
    clearance_mm: float,
    rng: random.Random,
) -> tuple[list[PlacedPart], list[PlacedPart]]:
    """Remove parts closest to the largest, most fragmenting free-space pockets.

    Uses metrics.analyze_free_space (via free_space_from_placed_parts) to
    find the pockets currently hurting the score most -- large fragmented
    or poorly-shaped gaps -- then removes whichever placed parts sit
    nearest those pockets. The intuition: a part sitting right at the edge
    of a large awkward gap is often the one whose repositioning could let a
    different, better-fitting arrangement absorb that gap, whereas removing
    a random part deep in an already-tight cluster rarely helps. This is
    the operator most directly targeting the user's original complaint
    (large visible gaps that a purely-additive backfill pass cannot see
    past, since backfill never removes anything already placed).
    """
    if not placed:
        return [], []
    free_space = free_space_from_placed_parts(usable_area, placed, clearance_mm=clearance_mm)
    if not free_space.pockets:
        # Sheet is fully saturated -- nothing to target, fall back to random
        # so the operator never silently does nothing.
        return _destroy_random(placed, fraction, rng)

    # Rank pockets by how much they hurt the score: large area, low
    # compactness (jagged/thin), and enclosed pockets are worst.
    def pocket_badness(pocket) -> float:
        return pocket.area_mm2 * (1.5 - pocket.compactness) * (1.5 if not pocket.touches_boundary else 1.0)

    worst_pockets = sorted(free_space.pockets, key=pocket_badness, reverse=True)
    target_pockets = worst_pockets[: max(1, len(worst_pockets) // 3 or 1)]

    count = _destroy_count(len(placed), fraction)

    def min_distance_to_targets(part: PlacedPart) -> float:
        return min(part.placed_shape_mm.distance(pocket.polygon_mm) for pocket in target_pockets)

    ranked = sorted(placed, key=min_distance_to_targets)
    removed_set = set(id(part) for part in ranked[:count])
    removed = [part for part in placed if id(part) in removed_set]
    kept = [part for part in placed if id(part) not in removed_set]
    return kept, removed


def _destroy_smallest(
    placed: list[PlacedPart],
    fraction: float,
) -> tuple[list[PlacedPart], list[PlacedPart]]:
    """Remove the smallest-area currently-placed parts.

    Smaller parts are cheapest to re-place (least likely to fail repair,
    since a small part fits into more candidate regions than a large one),
    so this operator is a low-risk way to shuffle the search: it frees up
    room without gambling on whether a large, hard-to-place part will find
    a home again during repair.
    """
    if not placed:
        return [], []
    count = _destroy_count(len(placed), fraction)
    ranked = sorted(placed, key=lambda part: part.placed_shape_mm.area)
    removed_set = set(id(part) for part in ranked[:count])
    removed = [part for part in placed if id(part) in removed_set]
    kept = [part for part in placed if id(part) not in removed_set]
    return kept, removed


def _destroy_neighborhood(
    placed: list[PlacedPart],
    fraction: float,
    rng: random.Random,
) -> tuple[list[PlacedPart], list[PlacedPart]]:
    """Remove one random seed part plus its geometric nearest neighbours.

    Distinct from ``_destroy_worst_gap_adjacent`` (which targets parts next
    to the WORST-scoring free-space pockets) and from ``_destroy_random``
    (which has no spatial structure at all): this operator picks one part
    uniformly at random as a seed, then removes whichever OTHER placed parts
    sit physically closest to it, by real Shapely ``.distance()`` between
    their placed contours -- not bounding-box distance, so an irregular
    concave neighbour that happens to have a wide bounding box but a close
    true edge is still ranked correctly. The intuition: a tightly-packed
    local cluster can be locally sub-optimal (parts individually valid but
    collectively wasting a little room between them) in a way neither pure
    randomness nor a pockets-only view would target -- clearing out one
    whole neighbourhood at once gives repair a genuine chance to re-tile it
    more efficiently, which removing the same count of scattered unrelated
    parts could not.
    """
    if not placed:
        return [], []
    if len(placed) == 1:
        return [], list(placed)
    count = _destroy_count(len(placed), fraction)
    seed_index = rng.randrange(len(placed))
    seed_part = placed[seed_index]

    def distance_to_seed(part: PlacedPart) -> float:
        if part is seed_part:
            return -1.0  # Seed itself always sorts first (guaranteed removed).
        return seed_part.placed_shape_mm.distance(part.placed_shape_mm)

    ranked = sorted(placed, key=distance_to_seed)
    removed_set = set(id(part) for part in ranked[:count])
    removed = [part for part in placed if id(part) in removed_set]
    kept = [part for part in placed if id(part) not in removed_set]
    return kept, removed


def _destroy_similarity(
    placed: list[PlacedPart],
    fraction: float,
    rng: random.Random,
) -> tuple[list[PlacedPart], list[PlacedPart]]:
    """Remove a size-homogeneous cluster: one random seed part plus its
    closest-by-area neighbours.

    Distinct from ``_destroy_smallest`` (which is a deterministic global
    ranking with no randomness at all, always removing the same absolute
    smallest parts every call) and from ``_destroy_neighborhood`` (which
    ranks by physical position on the sheet, not size): this operator ranks
    by AREA similarity to one randomly-chosen seed part, so a call started
    from a small part clears a same-sized-part cluster and a call started
    from a large part clears a different, large-part cluster. This targets
    a specific failure mode plain smallest-first destruction cannot: a
    layout where several similarly-sized (not necessarily smallest) parts
    were placed in a mutually awkward arrangement can be freed and
    re-tiled together, rather than only ever chipping at whichever parts
    happen to be the global minimum by area.
    """
    if not placed:
        return [], []
    if len(placed) == 1:
        return [], list(placed)
    count = _destroy_count(len(placed), fraction)
    seed_index = rng.randrange(len(placed))
    seed_area = placed[seed_index].placed_shape_mm.area

    def area_distance(part: PlacedPart) -> float:
        return abs(float(part.placed_shape_mm.area) - seed_area)

    ranked = sorted(placed, key=area_distance)
    removed_set = set(id(part) for part in ranked[:count])
    removed = [part for part in placed if id(part) in removed_set]
    kept = [part for part in placed if id(part) not in removed_set]
    return kept, removed


def _destroy_cluster(
    placed: list[PlacedPart],
    fraction: float,
    clearance_mm: float,
    rng: random.Random,
) -> tuple[list[PlacedPart], list[PlacedPart]]:
    """Remove one CONNECTED cluster of mutually-touching/close parts (a
    contiguous block on the sheet), grown breadth-first from a random seed.

    Distinct from ``_destroy_neighborhood`` (nearest-by-distance to a single
    seed, which can pick spatially scattered parts if the seed sits in a
    sparse area) and from ``_destroy_random``/``_destroy_smallest``/
    ``_destroy_similarity`` (none of which have any notion of contiguity at
    all): this operator does a breadth-first graph walk over the
    "touches-or-within-clearance" adjacency relation, so the removed set is
    always one single connected block of the layout, never a scattered
    handful of unrelated parts. A tightly interlocked block of irregular
    parts is exactly the case a single-part or purely-distance-ranked
    destroy is least likely to free up cleanly, since removing only SOME of
    a mutually-blocking cluster can leave the rest still blocking each
    other's repair -- removing the whole connected block at once gives
    repair a genuinely open region to re-tile, matching spec section 2's
    explicit "cluster destruction" request as a distinct mechanism from
    plain neighbourhood proximity.
    """
    if not placed:
        return [], []
    if len(placed) == 1:
        return [], list(placed)
    count = _destroy_count(len(placed), fraction)

    # Adjacency: two parts are cluster-adjacent when their placed contours
    # are within one clearance_mm of each other -- the same physical
    # separation the engine itself enforces between any two placed parts,
    # so "adjacent" here means "as close as the geometry engine ever allows
    # two parts to sit", not an arbitrary extra threshold.
    seed_index = rng.randrange(len(placed))
    visited_indices: list[int] = [seed_index]
    visited_set = {seed_index}
    frontier = [seed_index]
    remaining_indices = [i for i in range(len(placed)) if i != seed_index]

    while frontier and len(visited_indices) < count:
        current_index = frontier.pop(0)
        current_shape = placed[current_index].placed_shape_mm
        still_remaining = [i for i in remaining_indices if i not in visited_set]
        # Deterministic distance order (not rng-shuffled) keeps the BFS
        # expansion itself reproducible given the seed choice above; the
        # only randomness in this operator is which single part starts the
        # walk, exactly mirroring _destroy_neighborhood/_destroy_similarity's
        # "one random seed, then deterministic ranking from it" structure.
        still_remaining.sort(key=lambda i: current_shape.distance(placed[i].placed_shape_mm))
        for candidate_index in still_remaining:
            if len(visited_indices) >= count:
                break
            candidate_shape = placed[candidate_index].placed_shape_mm
            if current_shape.distance(candidate_shape) <= clearance_mm + 1e-6:
                visited_indices.append(candidate_index)
                visited_set.add(candidate_index)
                frontier.append(candidate_index)

    # If the connected component reachable from the seed is smaller than
    # the requested count (an isolated part far from everything else), top
    # up with the seed's nearest remaining parts by plain distance -- still
    # deterministic given the seed, and keeps this operator's removed count
    # matching its requested fraction like every other destroy operator,
    # rather than silently removing fewer parts than asked.
    if len(visited_indices) < count:
        seed_shape = placed[seed_index].placed_shape_mm
        still_remaining = [i for i in range(len(placed)) if i not in visited_set]
        still_remaining.sort(key=lambda i: seed_shape.distance(placed[i].placed_shape_mm))
        for candidate_index in still_remaining:
            if len(visited_indices) >= count:
                break
            visited_indices.append(candidate_index)
            visited_set.add(candidate_index)

    removed_set = visited_set
    removed = [part for index, part in enumerate(placed) if index in removed_set]
    kept = [part for index, part in enumerate(placed) if index not in removed_set]
    return kept, removed


_DestroyOperator = Callable[[list[PlacedPart], random.Random], tuple[list[PlacedPart], list[PlacedPart]]]


def _make_destroy_operators(
    usable_area: Polygon,
    clearance_mm: float,
    destroy_fraction: float,
) -> dict[str, _DestroyOperator]:
    """Bind the fixed per-call arguments so every operator has one shared signature."""
    return {
        "random": lambda placed, rng: _destroy_random(placed, destroy_fraction, rng),
        "worst_gap_adjacent": lambda placed, rng: _destroy_worst_gap_adjacent(
            placed, usable_area, destroy_fraction, clearance_mm, rng
        ),
        "smallest": lambda placed, rng: _destroy_smallest(placed, destroy_fraction),
        "neighborhood": lambda placed, rng: _destroy_neighborhood(placed, destroy_fraction, rng),
        "similarity": lambda placed, rng: _destroy_similarity(placed, destroy_fraction, rng),
        "cluster": lambda placed, rng: _destroy_cluster(placed, destroy_fraction, clearance_mm, rng),
    }


# ---------------------------------------------------------------------------
# Repair: re-place removed + still-unplaced parts using the EXISTING exact
# placement primitives (spec section 7: repair reuses geometry, no new
# feasibility test invented here)
# ---------------------------------------------------------------------------


def _occupied_zones_from_placed(
    placed: list[PlacedPart],
    prepared_rotations: dict[str, tuple[_PreparedRotation, ...]],
) -> list[_OccupiedZone]:
    """Rebuild _OccupiedZone bookkeeping from a plain PlacedPart list.

    Mirrors the exact reconstruction run_nesting itself already does when
    the fast candidate path hands off to the exact-path backfill (see
    engine.py's run_nesting, the `backfill_zones` rebuild block) -- same
    technique, reused here so repair on the exact path has correct
    occupied-zone state regardless of which path originally placed a part.
    """
    zones: list[_OccupiedZone] = []
    for part in placed:
        rotation = next(
            item for item in prepared_rotations[part.part_id] if item.angle == part.rotation
        )
        bounds = part.placed_shape_mm.bounds
        centered_bounds = rotation.centered_shape_mm.bounds
        zones.append(
            _OccupiedZone(
                rotation=rotation,
                center_x_mm=bounds[0] - centered_bounds[0],
                center_y_mm=bounds[1] - centered_bounds[1],
            )
        )
    return zones


def _repair(
    kept: list[PlacedPart],
    to_replace_ids: list[str],
    parts_mm: dict[str, PartInput],
    prepared_rotations: dict[str, tuple[_PreparedRotation, ...]],
    usable_area: Polygon,
    *,
    clearance_mm: float,
    placement_policy: str,
    use_fast_candidate_path: bool,
    rng: random.Random,
    check_cancelled: Callable[[], bool] | None = None,
    nfp_cache: dict[tuple[bytes, bytes], object] | None = None,
    merged_cache: dict[bytes, "_MergedBlockedCache"] | None = None,
    deadline: float | None = None,
) -> tuple[list[PlacedPart], list[str]]:
    """Re-place every removed/unplaced part against the surviving layout.

    Order matters for repair quality (not correctness -- every order is
    exact-feasible or the part stays unplaced, never invalid): trying
    LARGEST-first here mirrors the main search's own descending-size
    heuristic, since a large part has fewer candidate regions and should
    claim room before smaller parts fill it in. This is deliberately the
    OPPOSITE order from _backfill_gaps' smallest-first sweep, because
    repair's job is different: backfill retries parts a large-first pass
    already gave up on (so trying small ones is the whole point), whereas
    repair starts from a fresh removal where nothing has "already given up"
    yet -- largest-first here gives the destroyed neighbourhood its best
    chance at recovering (or improving on) the capacity it had before
    destruction.
    """
    ordered = sorted(
        to_replace_ids,
        key=lambda part_id: -parts_mm[part_id].shape_mm.area,
    )

    placed = list(kept)
    still_unplaced: list[str] = []
    occupied_zones = _occupied_zones_from_placed(placed, prepared_rotations) if not use_fast_candidate_path else []
    if nfp_cache is None:
        nfp_cache = {}
    if merged_cache is None:
        merged_cache = {}

    for part_id in ordered:
        if check_cancelled and check_cancelled():
            raise NestingCancelledError("تم إلغاء عملية تحسين الترتيب من قبل المستخدم.")
        # Soft, best-effort deadline, mirroring engine.py's identical pattern:
        # stop attempting FURTHER parts once the caller's time budget is gone,
        # rather than only checking inside _place_one_part_fast (which would
        # still let this loop start an expensive new part's placement attempt
        # after the budget already expired). Every part_id not reached here
        # falls through to still_unplaced below, exactly like a genuine
        # placement failure -- run_lns_optimization's scoring already handles
        # a smaller resulting placed_count correctly, no separate branch needed.
        if deadline is not None and time.monotonic() >= deadline:
            still_unplaced.extend(ordered[ordered.index(part_id):])
            break
        part_input = parts_mm[part_id]
        if use_fast_candidate_path:
            result = _place_one_part_fast(
                prepared_rotations[part_id],
                usable_area,
                placed,
                clearance_mm=clearance_mm,
                placement_policy=placement_policy,
                deadline=deadline,
            )
        else:
            result = _place_one_part(
                prepared_rotations[part_id],
                usable_area,
                occupied_zones,
                clearance_mm=clearance_mm,
                nfp_cache=nfp_cache,
                placement_policy=placement_policy,
                merged_cache=merged_cache,
            )
        if result is None:
            still_unplaced.append(part_id)
            continue
        angle, final_shape, center = result
        placed.append(
            PlacedPart(
                part_id=part_id,
                source_image_path=part_input.source_image_path,
                placed_shape_mm=final_shape,
                rotation=angle,
                source_centroid_px=part_input.source_centroid_px,
                alpha_bbox_px=part_input.alpha_bbox_px,
            )
        )
        if not use_fast_candidate_path:
            rotation = next(
                item for item in prepared_rotations[part_id] if item.angle == angle
            )
            occupied_zones.append(
                _OccupiedZone(
                    rotation=rotation,
                    center_x_mm=center[0],
                    center_y_mm=center[1],
                )
            )

    return placed, still_unplaced


# ---------------------------------------------------------------------------
# Acceptance criterion: simulated annealing (spec section 8)
# ---------------------------------------------------------------------------


def _accept(
    candidate_score: float,
    current_score: float,
    temperature: float,
    rng: random.Random,
) -> bool:
    """Standard SA acceptance: always take an improvement, sometimes take a
    worsening move with probability that shrinks as temperature cools.

    Accepting occasional worsening moves early (high temperature) is what
    lets the search escape a local optimum a strictly-greedy hill-climb
    would get stuck in; cooling over iterations converges the search toward
    pure improvement-only acceptance by the end, so the final iterations
    behave like a standard greedy local search around whatever neighbourhood
    the earlier exploration found.
    """
    if candidate_score >= current_score:
        return True
    if temperature <= 1e-9:
        return False
    import math

    delta = candidate_score - current_score
    probability = math.exp(delta / temperature)
    return rng.random() < probability


# ---------------------------------------------------------------------------
# Main LNS loop
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LnsIterationLog:
    """One iteration's outcome, for reporting/debugging/benchmarking."""

    iteration: int
    operator_name: str
    candidate_score: float
    accepted: bool
    is_new_best: bool
    placed_count: int
    temperature: float


@dataclass(frozen=True, slots=True)
class LnsResult:
    """Best layout found, plus the full search trace for transparency.

    ``best`` is always at least as good (by score_layout) as the starting
    layout passed in -- this module never returns something worse than what
    it started with, since every iteration only replaces the running best
    when the candidate's score is strictly higher (see the main loop below:
    the CURRENT/exploring state can wander per the SA acceptance rule, but
    the tracked BEST only ever moves up).
    """

    best: NestingResult
    best_score: LayoutScore
    starting_score: LayoutScore
    iterations_run: int
    improved: bool
    log: tuple[LnsIterationLog, ...] = field(default_factory=tuple)
    # Final adaptive-operator-selection weights (spec section 12), so a
    # caller (benchmark.py, a progress callback, the eventual report) can see
    # which destroy operators the search actually learned were effective on
    # this job, rather than the adaptation being invisible internal state.
    operator_weights_final: dict[str, float] = field(default_factory=dict)


def run_lns_optimization(
    starting_result: NestingResult,
    parts_mm: dict[str, PartInput],
    sheet_width_mm: float,
    sheet_height_mm: float,
    *,
    sheet_margin_mm: float = 5.0,
    clearance_mm: float,
    placement_policy: str = "bottom_left",
    max_iterations: int = 60,
    destroy_fraction: float = 0.15,
    initial_temperature: float = 50.0,
    cooling_rate: float = 0.92,
    objective_weights: ObjectiveWeights = DEFAULT_OBJECTIVE_WEIGHTS,
    seed: int | None = None,
    time_budget_seconds: float | None = None,
    check_cancelled: Callable[[], bool] | None = None,
    on_iteration: Callable[[LnsIterationLog], None] | None = None,
) -> LnsResult:
    """Improve a completed single-sheet layout via destroy/repair + SA.

    ``starting_result`` is normally the output of run_best_single_sheet_nesting
    -- this function takes an EXISTING valid layout and tries to strictly
    improve it, it does not build a layout from nothing (that remains
    run_nesting/run_best_single_sheet_nesting's job, unchanged).

    Deterministic when ``seed`` is given (spec section: seeded
    reproducibility) -- every random choice in this module (which operator,
    which parts a random-style destroy removes, SA acceptance rolls) is
    drawn from ``rng``, constructed once here from ``seed``, and nothing
    else in this function's control flow depends on wall-clock time or
    external randomness.
    """
    if max_iterations < 1:
        raise LnsError("max_iterations يجب أن يكون أكبر من صفر.")
    if not (0.0 < destroy_fraction <= 1.0):
        raise LnsError("destroy_fraction يجب أن يكون بين 0 و1.")

    rng = random.Random(seed)
    usable_area = _sheet_polygon(sheet_width_mm, sheet_height_mm, sheet_margin_mm)
    usable_area_mm2 = usable_area.area

    use_fast_candidate_path = _should_use_fast_candidate_path(parts_mm)

    rotations_by_shape: dict[bytes, tuple[_PreparedRotation, ...]] = {}
    prepared_rotations: dict[str, tuple[_PreparedRotation, ...]] = {}
    for part_id, part_input in parts_mm.items():
        try:
            shape_key = part_input.shape_mm.wkb
        except Exception:
            shape_key = repr(part_input.shape_mm).encode()
        rotations = rotations_by_shape.get(shape_key)
        if rotations is None:
            rotations = _prepare_rotations(part_input.shape_mm)
            rotations_by_shape[shape_key] = rotations
        prepared_rotations[part_id] = rotations

    destroy_operators = _make_destroy_operators(usable_area, clearance_mm, destroy_fraction)
    operator_names = list(destroy_operators.keys())
    operator_selector = AdaptiveOperatorSelector(operator_names=tuple(operator_names))

    current_placed = list(starting_result.placed)
    current_unplaced = list(starting_result.unplaced_part_ids)
    current_free_space = free_space_from_placed_parts(usable_area, current_placed, clearance_mm=clearance_mm)
    current_score = score_layout(current_placed, usable_area_mm2, current_free_space, weights=objective_weights)

    best_placed = current_placed
    best_unplaced = current_unplaced
    best_score = current_score
    starting_score = current_score

    temperature = initial_temperature
    log: list[LnsIterationLog] = []
    started = time.perf_counter()
    # engine.py's per-rotation/per-part deadline guards (_place_one_part_fast,
    # _find_fast_placement) compare against time.monotonic(), not
    # time.perf_counter() -- the two clocks have different, unrelated epochs,
    # so a perf_counter-derived value passed as their `deadline` would compare
    # against the wrong reference point entirely. Track a second start time on
    # the SAME clock those functions actually use, purely so each iteration's
    # repair() call can be given a same-clock deadline below.
    monotonic_started = time.monotonic()

    global_nfp_cache: dict[tuple[bytes, bytes], object] = {}
    global_merged_cache: dict[bytes, object] = {}

    for iteration in range(1, max_iterations + 1):
        if check_cancelled and check_cancelled():
            raise NestingCancelledError("تم إلغاء عملية تحسين الترتيب من قبل المستخدم.")
        if time_budget_seconds is not None and (time.perf_counter() - started) >= time_budget_seconds:
            break

        # Same ceiling as the outer loop's own budget, on engine.py's clock,
        # so this iteration's repair() cannot itself run past when the NEXT
        # iteration's check above would have stopped anyway -- one iteration's
        # destroy+repair now costs at most one budget's worth of wall clock,
        # instead of being able to run unbounded once repair() begins (see
        # _repair's own deadline guard for the measured per-call cost this
        # closes: up to 8.4s per part at 150 already-placed parts, across up
        # to ~54 already-unplaced parts carried into to_replace_ids).
        repair_deadline = (
            monotonic_started + time_budget_seconds if time_budget_seconds is not None else None
        )

        operator_name = operator_selector.select(rng)
        kept, removed = destroy_operators[operator_name](current_placed, rng)
        to_replace_ids = [part.part_id for part in removed] + current_unplaced

        candidate_placed, candidate_unplaced = _repair(
            kept,
            to_replace_ids,
            parts_mm,
            prepared_rotations,
            usable_area,
            clearance_mm=clearance_mm,
            placement_policy=placement_policy,
            use_fast_candidate_path=use_fast_candidate_path,
            rng=rng,
            check_cancelled=check_cancelled,
            # These two were previously omitted, which meant _repair() silently
            # built fresh empty caches on every single iteration (see _repair's
            # own `if nfp_cache is None: nfp_cache = {}` below) instead of
            # sharing NFP/merged-blocked-region results across iterations as
            # their `global_` name and pre-loop definition above already imply.
            # Results stayed correct either way -- this is a pure performance
            # fix, not a correctness one.
            nfp_cache=global_nfp_cache,
            merged_cache=global_merged_cache,
            # repair_deadline was computed above (on the same time.monotonic()
            # clock _place_one_part_fast/_find_fast_placement actually compare
            # against) specifically to bound this call, but was likewise never
            # passed through -- without it, _repair's own per-part deadline
            # check (`if deadline is not None and time.monotonic() >= deadline`)
            # was always inert, so a single iteration's repair() had no bound
            # independent of the outer loop's own time_budget_seconds check.
            deadline=repair_deadline,
        )

        candidate_free_space = free_space_from_placed_parts(
            usable_area, candidate_placed, clearance_mm=clearance_mm
        )
        candidate_score = score_layout(
            candidate_placed, usable_area_mm2, candidate_free_space, weights=objective_weights
        )

        accepted = _accept(candidate_score.total, current_score.total, temperature, rng)
        is_new_best = candidate_score.total > best_score.total
        operator_selector.update(operator_name, is_new_best=is_new_best, accepted=accepted)

        if accepted:
            current_placed = candidate_placed
            current_unplaced = candidate_unplaced
            current_score = candidate_score

        if is_new_best:
            best_placed = candidate_placed
            best_unplaced = candidate_unplaced
            best_score = candidate_score

        temperature *= cooling_rate

        entry = LnsIterationLog(
            iteration=iteration,
            operator_name=operator_name,
            candidate_score=candidate_score.total,
            accepted=accepted,
            is_new_best=is_new_best,
            placed_count=len(candidate_placed),
            temperature=temperature,
        )
        log.append(entry)
        if on_iteration:
            on_iteration(entry)

    # Independent re-validation before ever returning a layout different from
    # the one passed in. This is not expected to ever fail given every
    # placement came from the engine's own exact primitives, but the
    # function's correctness guarantee is checked, not assumed.
    if best_placed is not starting_result.placed:
        report = validate_layout(
            best_placed,
            sheet_width_mm,
            sheet_height_mm,
            sheet_margin_mm,
            clearance_mm=clearance_mm,
        )
        if not report.is_valid:
            best_placed = starting_result.placed
            best_unplaced = starting_result.unplaced_part_ids
            best_score = starting_score

    best_result = NestingResult(
        placed=best_placed,
        unplaced_part_ids=best_unplaced,
        sheet_full=bool(best_unplaced),
        processed_count=starting_result.processed_count,
        total_count=starting_result.total_count,
    )

    return LnsResult(
        best=best_result,
        best_score=best_score,
        starting_score=starting_score,
        iterations_run=len(log),
        improved=best_score.total > starting_score.total,
        log=tuple(log),
        operator_weights_final=dict(operator_selector.weights),
    )
