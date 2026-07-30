"""Mechanical smoke tests for the accumulate-tuning A/B harness — reproducibility
and grid shape, not a verdict on which grid point wins."""

import ab_accumulate_tuning as ab


def test_deterministic_for_a_fixed_seed():
    a = ab.run_all(ab.SEED)
    b = ab.run_all(ab.SEED)
    for axis in ab.AXES:
        for label in a[axis]:
            assert a[axis][label].cash == b[axis][label].cash
            assert a[axis][label].trades == b[axis][label].trades


def test_every_grid_point_is_present():
    grids = ab.run_all(ab.SEED)
    assert set(grids["trim_threshold_pct"]) == {ab._label(v) for v in ab.TRIM_GRID}
    assert set(grids["cash_buffer_pct"]) == {ab._label(v) for v in ab.BUFFER_GRID}
    assert set(grids["min_order_value"]) == {ab._label(v) for v in ab.MIN_ORDER_GRID}


def test_wider_trim_deadband_never_trades_more():
    # Structural: a wider deadband can only suppress trims, never add them.
    grids = ab.run_all(ab.SEED)
    trim = grids["trim_threshold_pct"]
    ordered = sorted(ab.TRIM_GRID)
    trades = [trim[ab._label(v)].trades for v in ordered]
    assert trades == sorted(trades, reverse=True)
