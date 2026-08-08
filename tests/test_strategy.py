import pytest

from bitinvest.portfolio import MasterView, Position, Snapshot, TargetPosition
from bitinvest.settings import AccumulateSettings, Settings
from bitinvest.strategy import plan_orders


def master(**weights):
    return MasterView(
        positions=tuple(
            TargetPosition(figi=figi, lot_size=1, price=100.0, weight=w,
                           ticker=figi, instrument_uid=f"uid-{figi}")
            for figi, w in weights.items()
        ),
        equity=10_000.0,
    )


def slave(cash, **holdings):
    return Snapshot(
        positions=tuple(
            Position(figi=figi, lot_size=1, price=100.0, quantity=q,
                     ticker=figi, instrument_uid=f"uid-{figi}")
            for figi, q in holdings.items()
        ),
        cash=cash,
    )


def settings(**kwargs):
    # This suite is about strategy.py's own logic, not about which policy
    # config.toml happens to ship as its default — pin both explicitly here
    # so a future default change (like the A/B-driven mode/leverage_policy
    # switch to accumulate/normalize) can't silently change what these
    # hand-picked example weights are supposed to produce.
    accumulate = AccumulateSettings(**kwargs.pop("accumulate", {}))
    kwargs.setdefault("mode", "mirror")
    kwargs.setdefault("leverage_policy", "cap")
    kwargs.setdefault("min_order_value", 0.0)
    return Settings(accumulate=accumulate, **kwargs)


def lots(orders):
    merged: dict[str, int] = {}
    for o in orders:
        merged[o.figi] = merged.get(o.figi, 0) + o.lots
    return merged


# --- mirror -----------------------------------------------------------------

def test_mirror_buys_into_an_empty_account():
    orders = plan_orders(master(A=1.0), slave(cash=10_000.0), settings())
    assert lots(orders) == {"A": 100}


def test_mirror_normalises_leverage_across_the_whole_book():
    # Master at 5x/3x, equity 10 000 on our side: 6250 into A, 3750 into B.
    orders = plan_orders(master(A=5.0, B=3.0), slave(cash=10_000.0), settings())
    assert lots(orders) == {"A": 62, "B": 37}


def test_mirror_leaves_a_matched_position_alone():
    orders = plan_orders(master(A=1.0), slave(cash=0.0, A=100), settings())
    assert orders == []


def test_short_lots_truncate_toward_zero():
    # Target is -2.4 lots. Floor division would give -3, i.e. a deeper short
    # than asked for; truncation gives -2.
    orders = plan_orders(master(A=-0.24), slave(cash=1000.0), settings())
    assert lots(orders) == {"A": -2}


def test_shorts_are_skipped_when_disallowed_and_longs_are_not_stretched():
    orders = plan_orders(master(A=0.6, B=-0.4), slave(cash=10_000.0), settings(allow_short=False))
    assert lots(orders) == {"A": 60}


def test_flipping_from_long_to_short_is_split_into_close_and_reverse():
    # Equity is 15 000 (5000 held + 10 000 cash), so a -0.3 weight targets
    # -4500, i.e. 45 lots short: close the 50 held, then open 45 the other way.
    orders = plan_orders(master(A=-0.3), slave(cash=10_000.0, A=50), settings())
    assert [o.lots for o in orders] == [-50, -45]
    assert "close" in orders[0].reason and "reverse" in orders[1].reason


def test_dust_orders_are_skipped_but_full_exits_are_not():
    # A is 500 RUB below target (under the floor); B is gone from the master
    # entirely and must be sold despite being small (1 lot: the gradual step
    # floor and a full close coincide here, so this holds either way).
    orders = plan_orders(master(A=0.95), slave(cash=500.0, A=95, B=1),
                         settings(min_order_value=1000.0))
    assert lots(orders) == {"B": -1}


def test_mirror_liquidates_gradually_by_default():
    # Master doesn't hold A at all. Default liquidation_mode is "gradual":
    # v0.1's always-on 1-lot-per-cycle unwind, generalised to a percentage
    # (25% here) so it scales with position size instead of taking forever on
    # a large one. ceil(40 * 0.25) = 10 lots this cycle, not the whole 40.
    orders = plan_orders(master(), slave(cash=0.0, A=40), settings())
    assert lots(orders) == {"A": -10}
    assert orders[0].reason == "liquidate (gradual)"


def test_mirror_liquidation_full_mode_closes_in_one_order():
    orders = plan_orders(master(), slave(cash=0.0, A=40), settings(liquidation_mode="full"))
    assert lots(orders) == {"A": -40}
    assert orders[0].reason == "liquidate"


def test_liquidation_step_pct_100_is_equivalent_to_full():
    orders = plan_orders(master(), slave(cash=0.0, A=40), settings(liquidation_step_pct=100.0))
    assert lots(orders) == {"A": -40}


def test_gradual_liquidation_floors_at_one_lot_and_reaches_zero():
    # 25% of 3 lots is 0.75, rounded up to 1 — never zero, and never more than
    # what's held, so a tiny position still empties out lot by lot.
    orders = plan_orders(master(), slave(cash=0.0, A=3), settings())
    assert lots(orders) == {"A": -1}


def test_gradual_liquidation_converges_to_zero_in_finite_cycles():
    current, cycles = 40, 0
    while current != 0:
        orders = plan_orders(master(), slave(cash=0.0, A=current), settings())
        assert orders, "must keep issuing an order every cycle until fully closed"
        current += lots(orders)["A"]
        cycles += 1
        assert cycles < 20, "gradual liquidation must terminate, not asymptote forever"
    assert current == 0


def test_margin_factor_shrinks_every_target():
    orders = plan_orders(master(A=1.0), slave(cash=10_000.0), settings(), margin_factor=0.5)
    assert lots(orders) == {"A": 50}


def test_refuses_to_trade_on_non_positive_equity():
    with pytest.raises(ValueError):
        plan_orders(master(A=1.0), slave(cash=-100.0), settings())


# --- accumulate -------------------------------------------------------------

def acc(**kwargs):
    cash = kwargs.pop("accumulate", {})
    cash.setdefault("deploy_free_cash", "never")
    return settings(mode="accumulate", accumulate=cash, **kwargs)


def test_accumulate_does_not_top_up_a_position_it_already_holds():
    # Master wants 100 lots, we hold 50 and have the cash. The dividend rule
    # says an existing position is not enlarged just because its weight rose.
    orders = plan_orders(master(A=1.0), slave(cash=5000.0, A=50), acc())
    assert orders == []


def test_accumulate_buys_a_position_the_master_just_opened():
    orders = plan_orders(master(A=0.5, B=0.5), slave(cash=5000.0, A=50), acc())
    assert lots(orders) == {"B": 50}


def test_accumulate_ignores_drift_below_the_trim_threshold():
    # Held value is 20% above target — inside the 25% tolerance, so no trim.
    orders = plan_orders(master(A=1.0), slave(cash=-2000.0, A=120), acc())
    assert orders == []


def test_accumulate_trims_once_the_threshold_is_crossed():
    orders = plan_orders(master(A=1.0), slave(cash=-5000.0, A=150), acc())
    assert lots(orders) == {"A": -50}


def test_accumulate_liquidates_gradually_by_default():
    # B is gone from the master; default gradual mode eases out of it over a
    # few cycles rather than dumping all 40 lots in one order.
    orders = plan_orders(master(A=1.0), slave(cash=0.0, A=100, B=40), acc())
    assert lots(orders) == {"B": -10}


def test_accumulate_liquidation_full_mode_closes_in_one_order():
    orders = plan_orders(master(A=1.0), slave(cash=0.0, A=100, B=40),
                         acc(liquidation_mode="full"))
    assert lots(orders) == {"B": -40}


# --- free cash deployment ---------------------------------------------------

def test_free_cash_goes_to_the_biggest_gap_first():
    # Equity 8000, so both targets are 4000. A lags by 3000 and B by 1000, and
    # 4000 of cash is idle: A is served first, then B with what remains.
    orders = plan_orders(master(A=0.5, B=0.5), slave(cash=4000.0, A=10, B=30),
                         settings(mode="accumulate",
                                  accumulate={"deploy_free_cash": "underweight_first"}))
    assert orders[0].figi == "A"
    assert lots(orders) == {"A": 30, "B": 10}


def test_free_cash_never_exceeds_the_target():
    # Only 2000 of room left under A's target even though 9000 is sitting idle.
    orders = plan_orders(master(A=0.3), slave(cash=9000.0, A=10),
                         settings(mode="accumulate",
                                  accumulate={"deploy_free_cash": "underweight_first"}))
    assert lots(orders) == {"A": 20}


def test_cash_buffer_is_left_untouched():
    # Equity 10 000 with 5000 in cash: a 50% buffer leaves nothing investable.
    held = dict(cash=5000.0, A=50)
    deploy = {"deploy_free_cash": "underweight_first"}

    without_buffer = plan_orders(master(A=1.0), slave(**held),
                                 settings(mode="accumulate", accumulate=dict(deploy)))
    with_buffer = plan_orders(master(A=1.0), slave(**held),
                              settings(mode="accumulate",
                                       accumulate=dict(deploy, cash_buffer_pct=50.0)))

    assert lots(without_buffer) == {"A": 50}
    assert with_buffer == []


def test_never_policy_leaves_idle_cash_alone():
    # The literal v0.1 behaviour: both positions lag their target and cash is
    # available, but nothing is bought because the master has not moved.
    held = dict(cash=4000.0, A=10, B=30)

    assert plan_orders(master(A=0.5, B=0.5), slave(**held), acc()) == []
    assert plan_orders(master(A=0.5, B=0.5), slave(**held),
                       settings(mode="accumulate",
                                accumulate={"deploy_free_cash": "underweight_first"}))
