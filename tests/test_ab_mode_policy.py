"""Mechanical smoke tests for the mode A/B harness — reproducibility and the
one directional claim that follows straight from the algorithms themselves
(accumulate's deadband trades less than mirror's constant chasing), not a
verdict on which mode wins on equity."""

import random

import ab_mode_policy as ab


def test_deterministic_for_a_fixed_seed():
    a = ab.run_all(ab.SEED)
    b = ab.run_all(ab.SEED)
    for mode in ab.MODES:
        assert a[mode].cash == b[mode].cash
        assert a[mode].trades == b[mode].trades


def test_accumulate_trades_less_than_mirror():
    # Structural, not a coincidence of one seed: mirror rebalances toward
    # target weights every cycle, accumulate only acts past a deadband.
    accounts = ab.run_all(ab.SEED)
    assert accounts["accumulate"].trades < accounts["mirror"].trades


def test_both_modes_end_with_positive_equity():
    accounts = ab.run_all(ab.SEED)
    series = ab.price_series(random.Random(ab.SEED))
    final = series[-1]
    for mode in ab.MODES:
        assert accounts[mode].snapshot(final).equity > 0
