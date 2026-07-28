from __future__ import annotations

import pandas as pd
from t_tech.invest import Client


def _decimal(value) -> float:
    return value.units + value.nano / 1_000_000_000


def parse(token_list: list[str], weight_list: list[float]) -> pd.DataFrame:
    """Aggregate portfolios across tokens/accounts into per-figi target weights.

    Used for both the master side (weighted blend of one or more accounts,
    written to step.csv) and the slave side (single account, weight=[1.0]).

    `percentage` blends each token's actual position value with its declared
    `weight_list` entry: a token's positions get `weight/total_weight` share
    of the combined target, independent of how much money that token
    actually holds. Values are then renormalized to sum to 100.

    Returns columns: figi, quantity, lot_size, price, percentage.
    `lot_size` is derived from quantity/quantity_lots (works because a held
    position is always a whole number of lots); NaN if the position is empty.
    """
    if len(token_list) != len(weight_list):
        raise ValueError("token_list and weight_list must be the same length")

    per_token_rows: list[list[dict]] = []
    per_token_value: list[float] = []

    for token in token_list:
        rows = []
        with Client(token) as client:
            accounts = client.users.get_accounts()
            for account in accounts.accounts:
                positions = client.operations.get_portfolio(account_id=account.id).positions
                for position in positions:
                    quantity = _decimal(position.quantity)
                    price = _decimal(position.current_price)
                    quantity_lots = _decimal(position.quantity_lots)
                    rows.append(
                        {
                            "figi": position.figi,
                            "quantity": quantity,
                            "lot_size": (quantity / quantity_lots) if quantity_lots else float("nan"),
                            "price": price,
                            "value": price * quantity,
                        }
                    )
        per_token_rows.append(rows)
        per_token_value.append(sum(r["value"] for r in rows))

    grand_total_value = sum(per_token_value)
    total_weight = sum(weight_list)

    flat_rows = []
    for rows, token_value, weight in zip(per_token_rows, per_token_value, weight_list):
        for r in rows:
            share = (r["value"] / grand_total_value) * (weight / total_weight)
            flat_rows.append({**r, "percentage": share})

    columns = ["figi", "quantity", "lot_size", "price", "percentage"]
    if not flat_rows:
        return pd.DataFrame(columns=columns)

    df = pd.DataFrame(flat_rows)
    agg = df.groupby("figi", as_index=False).agg(
        quantity=("quantity", "sum"),
        lot_size=("lot_size", "first"),
        price=("price", "first"),
        percentage=("percentage", "sum"),
    )
    coefficient = 100 / agg["percentage"].sum()
    agg["percentage"] = agg["percentage"] * coefficient
    return agg[columns]
