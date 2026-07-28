import pytest

from bitinvest.portfolio import Position, Snapshot, blend


def pos(figi, quantity, price=100.0, lot=1, uid=None):
    return Position(figi=figi, lot_size=lot, price=price, quantity=quantity,
                    ticker=figi, instrument_uid=uid or f"uid-{figi}")


def test_equity_counts_borrowed_money_as_negative_cash():
    # 500 of A + 300 of B bought with 700 borrowed: 100 is actually ours.
    snapshot = Snapshot(positions=(pos("A", 5, 100.0), pos("B", 3, 100.0)), cash=-700.0)
    assert snapshot.equity == pytest.approx(100.0)
    assert snapshot.gross_leverage == pytest.approx(8.0)
    assert snapshot.weights() == pytest.approx({"A": 5.0, "B": 3.0})


def test_short_is_a_negative_position_but_adds_to_gross_exposure():
    # Long 500 of A, short 300 of B; the short's proceeds sit in cash.
    snapshot = Snapshot(positions=(pos("A", 5, 100.0), pos("B", -3, 100.0)), cash=-100.0)
    assert snapshot.equity == pytest.approx(100.0)
    assert snapshot.gross_leverage == pytest.approx(8.0)
    assert snapshot.weights()["B"] == pytest.approx(-3.0)


def test_weights_reject_non_positive_equity():
    snapshot = Snapshot(positions=(pos("A", 1, 100.0),), cash=-100.0)
    with pytest.raises(ValueError):
        snapshot.weights()


def test_blend_gives_declared_weight_the_say_not_account_size():
    # Second master holds ten times the money. With equal declared weights the
    # blend must be 50/50 — v0.1 multiplied weight by account value and would
    # have produced roughly 9/91 here.
    small = Snapshot(positions=(pos("A", 1, 100.0),), cash=0.0)
    large = Snapshot(positions=(pos("B", 10, 100.0),), cash=0.0)

    view = blend([small, large], [1.0, 1.0])

    weights = {p.figi: p.weight for p in view.positions}
    assert weights == pytest.approx({"A": 0.5, "B": 0.5})
    assert view.cash_weight == pytest.approx(0.0)


def test_blend_nets_out_opposite_positions():
    long_master = Snapshot(positions=(pos("A", 1, 100.0),), cash=0.0)
    short_master = Snapshot(positions=(pos("A", -1, 100.0),), cash=200.0)

    view = blend([long_master, short_master], [1.0, 1.0])

    assert [p.figi for p in view.positions] == []
    assert view.gross_leverage == pytest.approx(0.0)


def test_cash_weight_is_the_residual():
    half_invested = Snapshot(positions=(pos("A", 5, 100.0),), cash=500.0)
    view = blend([half_invested], [1.0])
    assert view.cash_weight == pytest.approx(0.5)
