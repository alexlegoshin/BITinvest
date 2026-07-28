import pandas as pd

from bitinvest.rebalance import check_deltas, master_to_slave_balance_adaptation


def test_master_to_slave_balance_adaptation_basic():
    master_df = pd.DataFrame(
        [
            {"figi": "X", "lot_size": 1, "percentage": 100.0, "price": 10.0},
            {"figi": "RUB000UTSTOM", "lot_size": 1, "percentage": 0.0, "price": 1.0},
        ]
    )
    slave_df = pd.DataFrame(
        [
            {"figi": "X", "quantity": 100, "lot_size": 1, "price": 10.0},
            {"figi": "RUB000UTSTOM", "quantity": 0, "lot_size": 1, "price": 1.0},
        ]
    )

    adapted = master_to_slave_balance_adaptation(master_df, slave_df).set_index("figi")

    assert adapted.loc["X", "quantity"] == 100
    # leftover cash after buying X absorbs into RUB, not lost
    assert adapted.loc["RUB000UTSTOM", "quantity"] == 0


def test_check_deltas_matched_zero_delta_not_force_sold():
    # X's target exactly matches current holdings -> no order should be
    # emitted, and the cleanup pass must not force-sell it either (previous
    # version's cleanup pass checked "did an order fire for this figi", so a
    # perfectly balanced position fell through and got sold by 1 lot).
    master_df = pd.DataFrame(
        [
            {"figi": "X", "lot_size": 1, "percentage": 100.0, "price": 10.0},
            {"figi": "RUB000UTSTOM", "lot_size": 1, "percentage": 0.0, "price": 1.0},
        ]
    )
    slave_df = pd.DataFrame(
        [
            {"figi": "X", "quantity": 100, "lot_size": 1, "price": 10.0},
            {"figi": "RUB000UTSTOM", "quantity": 0, "lot_size": 1, "price": 1.0},
        ]
    )

    orders = check_deltas(master_df, slave_df)

    assert orders.empty


def test_check_deltas_full_liquidation_of_unmatched_position():
    # Slave holds "C", which master does not want at all -> the whole
    # position should be sold, not a hardcoded 1 lot.
    master_df = pd.DataFrame(
        [
            {"figi": "A", "lot_size": 10, "percentage": 50.0, "price": 100.0},
            {"figi": "RUB000UTSTOM", "lot_size": 1, "percentage": 50.0, "price": 1.0},
        ]
    )
    slave_df = pd.DataFrame(
        [
            {"figi": "A", "quantity": 500, "lot_size": 10, "price": 100.0},
            {"figi": "C", "quantity": 5, "lot_size": 1, "price": 20.0},
            {"figi": "RUB000UTSTOM", "quantity": 100, "lot_size": 1, "price": 1.0},
        ]
    )

    orders = check_deltas(master_df, slave_df).set_index("figi")

    assert orders.loc["C", "quantity"] == -5


def test_check_deltas_buys_new_position_master_wants():
    # Master wants "B" but slave holds none of it yet -> a buy order for the
    # full target size should appear.
    master_df = pd.DataFrame(
        [
            {"figi": "B", "lot_size": 1, "percentage": 100.0, "price": 50.0},
            {"figi": "RUB000UTSTOM", "lot_size": 1, "percentage": 0.0, "price": 1.0},
        ]
    )
    slave_df = pd.DataFrame(
        [
            {"figi": "RUB000UTSTOM", "quantity": 1000, "lot_size": 1, "price": 1.0},
        ]
    )

    orders = check_deltas(master_df, slave_df).set_index("figi")

    assert orders.loc["B", "quantity"] == 20
