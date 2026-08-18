"""Tests for app.nesting.benchmark: the seeded statistical harness.

These use small part_count/sheet sizes deliberately -- the point is to prove
generate_benchmark_parts/run_one_seed/run_benchmark's own logic (seeded
reproducibility, statistical aggregation, exclusion bookkeeping) is correct,
not to reproduce the full-scale (part_count=40, 300x300mm) numbers that a
real benchmark run reports. Every test still exercises the REAL
baseline -> LNS -> compaction pipeline end to end, just at a size that keeps
the suite fast.
"""
from __future__ import annotations

import pytest

from app.nesting.benchmark import (
    BenchmarkError,
    KnownGeosLimitation,
    MetricStats,
    SeedRunResult,
    _metric_stats,
    generate_benchmark_parts,
    render_before_after_diff,
    run_benchmark,
    run_one_seed,
)


# ---------------------------------------------------------------------------
# generate_benchmark_parts: seeded reproducibility (spec requirement)
# ---------------------------------------------------------------------------


def test_generate_benchmark_parts_is_reproducible_for_the_same_seed():
    first = generate_benchmark_parts(seed=42, part_count=12)
    second = generate_benchmark_parts(seed=42, part_count=12)

    assert set(first) == set(second)
    for part_id in first:
        # Exact coordinate equality, not just matching area/bounds -- numpy's
        # own Generator(seed) is deterministic bit-for-bit, and a real
        # before/after visualization depends on this holding exactly.
        assert first[part_id].shape_mm.equals_exact(second[part_id].shape_mm, tolerance=1e-12)


def test_generate_benchmark_parts_differs_across_seeds():
    first = generate_benchmark_parts(seed=1, part_count=12)
    second = generate_benchmark_parts(seed=2, part_count=12)

    assert any(
        not first[part_id].shape_mm.equals_exact(second[part_id].shape_mm, tolerance=1e-9)
        for part_id in first
    )


def test_generate_benchmark_parts_every_shape_is_valid_with_positive_area():
    parts = generate_benchmark_parts(seed=7, part_count=40)
    assert len(parts) == 40
    for part in parts.values():
        assert part.shape_mm.is_valid
        assert part.shape_mm.area > 0


def test_generate_benchmark_parts_rejects_non_positive_part_count():
    with pytest.raises(BenchmarkError):
        generate_benchmark_parts(seed=1, part_count=0)


# ---------------------------------------------------------------------------
# MetricStats / _metric_stats
# ---------------------------------------------------------------------------


def test_metric_stats_reports_correct_best_worst_mean_median():
    stats = _metric_stats([1.0, 2.0, 3.0, 4.0, 10.0])
    assert isinstance(stats, MetricStats)
    assert stats.best == 10.0
    assert stats.worst == 1.0
    assert stats.mean == pytest.approx(4.0)
    assert stats.median == pytest.approx(3.0)
    assert stats.stdev > 0.0


def test_metric_stats_single_value_has_zero_stdev_not_a_crash():
    # statistics.stdev raises on <2 points; a 1-seed smoke-test call into
    # run_benchmark must not crash for this reason.
    stats = _metric_stats([5.0])
    assert stats.best == stats.worst == stats.mean == stats.median == 5.0
    assert stats.stdev == 0.0


def test_metric_stats_rejects_empty_input():
    with pytest.raises(BenchmarkError):
        _metric_stats([])


# ---------------------------------------------------------------------------
# run_one_seed: real, small, controlled end-to-end runs
# ---------------------------------------------------------------------------


def test_run_one_seed_produces_a_valid_self_consistent_result():
    result = run_one_seed(
        seed=101,
        sheet_width_mm=150.0,
        sheet_height_mm=150.0,
        sheet_margin_mm=5.0,
        clearance_mm=2.0,
        part_count=8,
        lns_iterations=10,
    )

    assert isinstance(result, SeedRunResult)
    assert result.pipeline_valid is True
    assert result.part_count == 8
    assert 0.0 <= result.baseline_utilization <= 1.0
    assert 0.0 <= result.pipeline_utilization <= 1.0
    assert result.baseline_placed_count == len(result.baseline.placed)
    assert result.pipeline_placed_count == len(result.pipeline.placed)
    # With the default ObjectiveWeights (placed_count=1000 vs a combined
    # max swing of roughly 150 across every other term), LNS's own
    # never-worse-by-score guarantee means placed_count essentially cannot
    # decrease from baseline to pipeline; compact_layout never changes
    # placed_count at all (pure translation). If this ever fails for a real
    # seed it is worth investigating directly rather than loosening silently.
    assert result.pipeline_placed_count >= result.baseline_placed_count
    assert result.elapsed_seconds > 0.0


def test_run_one_seed_is_reproducible_for_the_same_seed():
    first = run_one_seed(
        seed=55, sheet_width_mm=120.0, sheet_height_mm=120.0,
        clearance_mm=2.0, part_count=6, lns_iterations=8,
    )
    second = run_one_seed(
        seed=55, sheet_width_mm=120.0, sheet_height_mm=120.0,
        clearance_mm=2.0, part_count=6, lns_iterations=8,
    )

    assert first.baseline_placed_count == second.baseline_placed_count
    assert first.pipeline_placed_count == second.pipeline_placed_count
    assert first.pipeline_utilization == pytest.approx(second.pipeline_utilization)


# ---------------------------------------------------------------------------
# run_benchmark: aggregation + exclusion bookkeeping (spec section 18)
# ---------------------------------------------------------------------------


def test_run_benchmark_rejects_empty_seed_list():
    with pytest.raises(BenchmarkError):
        run_benchmark([])


def test_run_benchmark_aggregates_stats_across_multiple_small_seeds():
    report = run_benchmark(
        [201, 202, 203],
        sheet_width_mm=120.0,
        sheet_height_mm=120.0,
        sheet_margin_mm=5.0,
        clearance_mm=2.0,
        part_count=6,
        lns_iterations=8,
    )

    assert len(report.seed_results) == 3
    assert report.excluded_seeds == ()
    assert report.excluded_geos_limitation_seeds == ()
    deltas = [r.pipeline_utilization - r.baseline_utilization for r in report.seed_results]
    assert report.utilization_delta_stats.best == pytest.approx(max(deltas))
    assert report.utilization_delta_stats.worst == pytest.approx(min(deltas))
    assert "baseline" in report.summary_text()
    assert "pipeline" in report.summary_text()


def test_run_benchmark_excludes_pipeline_invalid_seeds_from_every_stat(monkeypatch):
    """Exercises the exclusion path via one controlled fake result rather than
    depending on ever forcing a real re-validation failure -- which, after
    the candidate-selection fix, should not happen. Confirms run_benchmark
    still records the invalid seed (transparency) while excluding it from
    every statistic (correctness)."""
    import app.nesting.benchmark as benchmark_module

    real_run_one_seed = benchmark_module.run_one_seed

    def fake_run_one_seed(seed, **kwargs):
        result = real_run_one_seed(seed, **kwargs)
        if seed == 302:
            result = SeedRunResult(
                seed=result.seed,
                part_count=result.part_count,
                baseline=result.baseline,
                pipeline=result.pipeline,
                baseline_utilization=result.baseline_utilization,
                pipeline_utilization=result.pipeline_utilization,
                baseline_placed_count=result.baseline_placed_count,
                pipeline_placed_count=result.pipeline_placed_count,
                baseline_score=result.baseline_score,
                pipeline_score=result.pipeline_score,
                baseline_free_area_mm2=result.baseline_free_area_mm2,
                pipeline_free_area_mm2=result.pipeline_free_area_mm2,
                pipeline_valid=False,
                elapsed_seconds=result.elapsed_seconds,
            )
        return result

    monkeypatch.setattr(benchmark_module, "run_one_seed", fake_run_one_seed)

    report = benchmark_module.run_benchmark(
        [301, 302],
        sheet_width_mm=120.0,
        sheet_height_mm=120.0,
        sheet_margin_mm=5.0,
        clearance_mm=2.0,
        part_count=6,
        lns_iterations=8,
    )

    assert report.excluded_seeds == (302,)
    assert len(report.seed_results) == 2  # both raw results kept for transparency
    included_result = next(r for r in report.seed_results if r.seed == 301)
    assert report.baseline_placed_count_stats.best == float(included_result.baseline_placed_count)
    assert report.baseline_placed_count_stats.worst == float(included_result.baseline_placed_count)


def test_run_benchmark_records_geos_limitation_seeds_without_aborting(monkeypatch):
    """Confirms a KnownGeosLimitation on one seed is caught, recorded, and the
    run continues with the remaining seeds -- without depending on whether
    the real GEOS numerical edge case reproduces on this machine."""
    import app.nesting.benchmark as benchmark_module

    real_run_one_seed = benchmark_module.run_one_seed

    def fake_run_one_seed(seed, **kwargs):
        if seed == 402:
            raise KnownGeosLimitation(seed, "simulated for test isolation")
        return real_run_one_seed(seed, **kwargs)

    monkeypatch.setattr(benchmark_module, "run_one_seed", fake_run_one_seed)

    report = benchmark_module.run_benchmark(
        [401, 402],
        sheet_width_mm=120.0,
        sheet_height_mm=120.0,
        sheet_margin_mm=5.0,
        clearance_mm=2.0,
        part_count=6,
        lns_iterations=8,
    )

    assert report.excluded_geos_limitation_seeds == ((402, "simulated for test isolation"),)
    assert len(report.seed_results) == 1
    assert report.seed_results[0].seed == 401
    assert "402" in report.summary_text()


def test_run_benchmark_raises_when_every_seed_is_pipeline_invalid(monkeypatch):
    import app.nesting.benchmark as benchmark_module

    real_run_one_seed = benchmark_module.run_one_seed

    def fake_run_one_seed(seed, **kwargs):
        result = real_run_one_seed(seed, **kwargs)
        return SeedRunResult(
            seed=result.seed,
            part_count=result.part_count,
            baseline=result.baseline,
            pipeline=result.pipeline,
            baseline_utilization=result.baseline_utilization,
            pipeline_utilization=result.pipeline_utilization,
            baseline_placed_count=result.baseline_placed_count,
            pipeline_placed_count=result.pipeline_placed_count,
            baseline_score=result.baseline_score,
            pipeline_score=result.pipeline_score,
            baseline_free_area_mm2=result.baseline_free_area_mm2,
            pipeline_free_area_mm2=result.pipeline_free_area_mm2,
            pipeline_valid=False,
            elapsed_seconds=result.elapsed_seconds,
        )

    monkeypatch.setattr(benchmark_module, "run_one_seed", fake_run_one_seed)

    with pytest.raises(BenchmarkError):
        benchmark_module.run_benchmark(
            [501], sheet_width_mm=120.0, sheet_height_mm=120.0,
            clearance_mm=2.0, part_count=6, lns_iterations=8,
        )


# ---------------------------------------------------------------------------
# Visualization renderers: real files, real content
# ---------------------------------------------------------------------------


def test_render_before_after_diff_writes_three_real_png_files(tmp_path):
    result = run_one_seed(
        seed=601, sheet_width_mm=120.0, sheet_height_mm=120.0,
        clearance_mm=2.0, part_count=6, lns_iterations=8,
    )

    before_path, after_path, diff_path = render_before_after_diff(result, 120.0, 120.0, tmp_path)

    for path in (before_path, after_path, diff_path):
        assert path.exists()
        assert path.stat().st_size > 0
        assert path.suffix == ".png"
