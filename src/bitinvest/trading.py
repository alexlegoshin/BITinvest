from __future__ import annotations

import logging
import time

from t_tech.invest import Client, OrderDirection, OrderType

logger = logging.getLogger(__name__)


def _single_account_id(client: Client) -> str:
    accounts = client.users.get_accounts().accounts
    if not accounts:
        raise RuntimeError("Token has no accounts")
    if len(accounts) > 1:
        logger.warning(
            "Token has %d accounts; using the first (%s) and ignoring the rest. "
            "Create account-scoped tokens (one account per token) to avoid this.",
            len(accounts),
            accounts[0].id,
        )
    return accounts[0].id


def _post_market_order(token: str, figi: str, quantity: int, direction: OrderDirection.ValueType, label: str):
    with Client(token) as client:
        account_id = _single_account_id(client)
        return client.orders.post_order(
            order_id=f"{label} {time.time()}",
            figi=figi,
            quantity=int(quantity),
            account_id=account_id,
            direction=direction,
            order_type=OrderType.ORDER_TYPE_MARKET,
        )


def sell(token: str, figi: str, quantity: int):
    return _post_market_order(token, figi, quantity, OrderDirection.ORDER_DIRECTION_SELL, "sell")


def buy(token: str, figi: str, quantity: int):
    return _post_market_order(token, figi, quantity, OrderDirection.ORDER_DIRECTION_BUY, "buy")
