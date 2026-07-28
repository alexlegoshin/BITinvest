import pytest

from bitinvest.margin import (
    MarginState,
    RiskRate,
    margin_cap,
    needs_margin_check,
    required_margin,
)


def test_starting_margin_uses_the_side_specific_rate():
    rates = {"a": RiskRate(long=0.4, short=0.8)}
    assert required_margin({"a": 1000.0}, rates) == pytest.approx(400.0)
    assert required_margin({"a": -1000.0}, rates) == pytest.approx(800.0)


def test_unknown_instrument_demands_full_collateral():
    assert required_margin({"a": 1000.0}, {}) == pytest.approx(1000.0)


def test_margin_cap_shrinks_the_target_when_shorts_are_expensive():
    # 1000 long at 0.4 plus 1000 short at 0.8 needs 1200 of margin against a
    # 1000 liquid portfolio, so at 90% safety only 75% of the size fits.
    rates = {"a": RiskRate(0.4, 0.8), "b": RiskRate(0.4, 0.8)}
    cap = margin_cap({"a": 1000.0, "b": -1000.0}, rates, liquid_portfolio=1000.0, safety=0.9)
    assert cap == pytest.approx(0.75)


def test_margin_cap_is_unbounded_without_positions():
    assert margin_cap({}, {}, 1000.0, 0.9) == float("inf")


def test_long_only_within_leverage_one_skips_the_api_call():
    assert not needs_margin_check({"a": 0.6, "b": 0.4}, 1.0)


def test_shorts_or_leverage_above_one_require_the_check():
    assert needs_margin_check({"a": 0.6, "b": -0.4}, 1.0)
    assert needs_margin_check({"a": 2.0}, 1.0)


def test_margin_state_zones():
    healthy = MarginState(liquid_portfolio=1000, starting_margin=500, minimal_margin=250)
    assert healthy.can_open_new and not healthy.margin_call

    stuck = MarginState(liquid_portfolio=400, starting_margin=500, minimal_margin=250)
    assert not stuck.can_open_new and not stuck.margin_call

    called = MarginState(liquid_portfolio=200, starting_margin=500, minimal_margin=250)
    assert called.margin_call
