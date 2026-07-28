import pytest

from bitinvest.leverage import apply_scale, drop_shorts, gross_leverage, scale_factor


def test_five_and_three_normalise_to_one_keeping_the_ratio():
    # The worked example: master runs 5x in A and 3x in B. At target 1.0 the
    # whole book shrinks by one factor, so 5:3 survives as 0.625:0.375.
    weights = {"A": 5.0, "B": 3.0}
    factor = scale_factor(gross_leverage(weights), target=1.0, policy="cap")

    scaled = apply_scale(weights, factor)

    assert scaled == pytest.approx({"A": 0.625, "B": 0.375})
    assert gross_leverage(scaled) == pytest.approx(1.0)
    assert scaled["A"] / scaled["B"] == pytest.approx(5 / 3)


def test_short_at_leverage_one_needs_no_borrowed_money():
    # Long 0.625 + short 0.375 is gross 1.0; the short's proceeds leave cash
    # positive, which is the "short as an alternative to cash" case.
    weights = {"A": 5.0, "B": -3.0}
    scaled = apply_scale(weights, scale_factor(gross_leverage(weights), 1.0, "cap"))

    assert scaled == pytest.approx({"A": 0.625, "B": -0.375})
    cash_weight = 1 - sum(scaled.values())
    assert cash_weight == pytest.approx(0.75)


def test_cap_respects_a_master_sitting_in_cash():
    weights = {"A": 0.5}
    assert scale_factor(gross_leverage(weights), target=1.0, policy="cap") == pytest.approx(1.0)


def test_normalize_deploys_the_masters_cash():
    weights = {"A": 0.5}
    assert scale_factor(gross_leverage(weights), target=1.0, policy="normalize") == pytest.approx(2.0)


def test_empty_master_scales_to_nothing():
    assert scale_factor(0.0, target=1.0, policy="cap") == 0.0


def test_unknown_policy_is_rejected():
    with pytest.raises(ValueError):
        scale_factor(2.0, 1.0, "whatever")


def test_dropping_shorts_does_not_stretch_the_longs():
    # 0.6 long stays 0.6; the freed 0.4 goes to cash rather than being bet on
    # the longs, which would be our own decision, not the master's.
    assert drop_shorts({"A": 0.6, "B": -0.4}) == {"A": 0.6}
