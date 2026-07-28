"""The A/B harness is a decision tool, so it gets tests of its own — mostly to
guarantee it stays deterministic and keeps running the real strategy code."""

import random

import ab_cash_policy as ab


def series():
    return ab.price_series(random.Random(ab.SEED))


def test_prices_are_reproducible():
    assert series() == series()


def test_both_policies_see_identical_conditions():
    prices = series()
    assert ab.run("never", prices).snapshot(prices[-1]).equity == \
        ab.run("never", prices).snapshot(prices[-1]).equity


def test_never_leaves_cash_idle_and_deploying_does_not():
    prices = series()

    idle = ab.run("never", prices)
    deployed = ab.run("underweight_first", prices)

    # No claim about which ends up richer — that depends on the market. The
    # mechanical difference is the point: one parks the deposits, one invests
    # them, and investing costs more in commission.
    assert idle.cash > deployed.cash
    assert deployed.trades > idle.trades
    assert deployed.commission > idle.commission
