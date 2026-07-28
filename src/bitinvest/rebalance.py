from __future__ import annotations

import pandas as pd

RUB_FIGI = "RUB000UTSTOM"


def master_to_slave_balance_adaptation(master_df: pd.DataFrame, slave_df: pd.DataFrame) -> pd.DataFrame:
    """Scale master's target percentages into raw-unit quantities sized for
    the slave account's actual balance; leftover cash goes to RUB."""
    slave_balance = (slave_df.quantity * slave_df.price).sum()
    delta = slave_balance
    rows = []
    for _, m in master_df.iterrows():
        quantity = (slave_balance * m.percentage / 100) // m.price
        rows.append({"figi": m.figi, "quantity": quantity, "price": m.price})
        if m.figi != RUB_FIGI:
            delta -= quantity * m.price
    df = pd.DataFrame(rows)
    df.loc[df.figi == RUB_FIGI, "quantity"] = delta
    df["percentage"] = df.quantity * df.price * 100 / slave_balance
    return df


def check_deltas(master_df: pd.DataFrame, slave_df: pd.DataFrame) -> pd.DataFrame:
    """Compare the slave's current holdings against the master-adapted target
    and return the lot quantity to buy (+) or sell (-) per figi.

    A row is emitted whenever target and actual holdings differ by at least
    one lot. Any master figi the slave also holds counts as "matched" even
    when no order is needed (delta < 1 lot) — the previous version instead
    checked "did this figi already get an order", which meant a perfectly
    balanced position (delta exactly 0, so no order emitted) fell through to
    the cleanup pass below and got force-sold by 1 lot. Tracking matches by
    presence in master_df instead of by emitted order fixes that.
    """
    target = master_to_slave_balance_adaptation(master_df, slave_df)
    target_by_figi = target.set_index("figi")

    orders: list[dict] = []
    matched_figi: set[str] = set()

    for _, m in master_df.iterrows():
        if m.figi == RUB_FIGI:
            continue
        slave_match = slave_df[slave_df.figi == m.figi]
        target_quantity = target_by_figi.loc[m.figi, "quantity"]
        if not slave_match.empty:
            matched_figi.add(m.figi)
            current_quantity = slave_match.iloc[0].quantity
        else:
            current_quantity = 0
        lot_delta = (target_quantity - current_quantity) // m.lot_size
        if lot_delta != 0:
            orders.append({"figi": m.figi, "quantity": lot_delta})

    for _, s in slave_df.iterrows():
        if s.figi == RUB_FIGI or s.figi in matched_figi or s.quantity == 0:
            continue
        lots_held = s.quantity // s.lot_size
        if lots_held != 0:
            orders.append({"figi": s.figi, "quantity": -lots_held})

    return pd.DataFrame(orders, columns=["figi", "quantity"])
