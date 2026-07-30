"""Mechanical smoke tests for the margin_safety A/B harness. No network here:
simulate() takes a plain price list, so these use small crafted price paths
instead of a real fetch — the real-data plumbing itself (tools/ab_runner.py's
real_basket slot) is exercised by the runner's --once smoke run, not here."""

import ab_margin_safety as ab


def test_capital_used_is_monotonic_in_safety():
    # min(1, cap) where cap grows with safety — never less capital deployed
    # at a higher safety setting, for the same entry conditions.
    flat = [100.0] * (ab.HOLD_DAYS + 1)
    used = [ab.simulate(s, flat, 0)["capital_used_pct"] for s in ab.SAFETY_GRID]
    assert used == sorted(used)


def test_flat_prices_never_trigger_a_margin_call():
    flat = [100.0] * (ab.HOLD_DAYS + 1)
    for safety in ab.SAFETY_GRID:
        assert ab.simulate(safety, flat, 0)["margin_call"] == 0.0


def test_a_severe_enough_crash_triggers_a_margin_call():
    crash = [100.0 * (0.97 ** d) for d in range(ab.HOLD_DAYS + 1)]
    for safety in ab.SAFETY_GRID:
        assert ab.simulate(safety, crash, 0)["margin_call"] == 1.0


def test_higher_safety_is_never_safer_against_the_same_shock():
    # Less cushion at higher capital use: the worst equity/minimal-margin
    # ratio reached can only get worse (or equal) as safety increases.
    moderate = [100.0 * (0.994 ** d) for d in range(ab.HOLD_DAYS + 1)]
    ratios = [ab.simulate(s, moderate, 0)["worst_margin_ratio"] for s in ab.SAFETY_GRID]
    assert ratios == sorted(ratios, reverse=True)
