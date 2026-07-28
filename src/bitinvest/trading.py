"""Order execution.

One client per cycle rather than per order (v0.1 opened a fresh gRPC channel
for every single order), a proper unique ``order_id``, and a dry-run switch so
the whole pipeline can be exercised without anything reaching the exchange.
"""

from __future__ import annotations

import logging
from typing import Iterable, Mapping
from uuid import uuid4

from t_tech.invest import Client, InstrumentIdType, OrderDirection, OrderType

from bitinvest.broker import resolve_account_id
from bitinvest.strategy import Order

logger = logging.getLogger(__name__)


def short_enabled(client, instrument_uid: str) -> bool:
    """Whether the broker lets this instrument be shorted at all.

    Unknown instrument or a failed lookup counts as "no": refusing a short we
    cannot verify is cheap, having it rejected mid-cycle is not.
    """
    if not instrument_uid:
        return False
    try:
        response = client.instruments.get_instrument_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=instrument_uid
        )
    except Exception:  # noqa: BLE001
        logger.warning("Could not look up instrument %s; treating short as forbidden", instrument_uid)
        return False
    return bool(response.instrument.short_enabled_flag)


def drop_forbidden_shorts(client, orders: Iterable[Order], held_lots: Mapping[str, int]) -> list[Order]:
    """Filter out sells that would open or deepen a short on an instrument the
    broker does not allow shorting. Sells that merely reduce a long pass."""
    kept: list[Order] = []
    position = dict(held_lots)
    for order in orders:
        resulting = position.get(order.figi, 0) + order.lots
        opens_short = resulting < 0
        if opens_short and not short_enabled(client, order.instrument_uid):
            logger.warning("Short not permitted for %s (%s) — skipping %d lot(s)",
                           order.ticker or order.figi, order.figi, order.lots)
            continue
        position[order.figi] = resulting
        kept.append(order)
    return kept


def post_order(client, account_id: str, order: Order, confirm_margin: bool = False):
    """``confirm_margin`` is the API's explicit consent flag for an order that
    needs borrowed money or borrowed stock; without it such an order is
    rejected. Harmless on orders that turn out not to need margin."""
    direction = OrderDirection.ORDER_DIRECTION_BUY if order.is_buy else OrderDirection.ORDER_DIRECTION_SELL
    return client.orders.post_order(
        order_id=uuid4().hex,
        instrument_id=order.instrument_uid or order.figi,
        quantity=abs(int(order.lots)),
        account_id=account_id,
        direction=direction,
        order_type=OrderType.ORDER_TYPE_MARKET,
        confirm_margin_trade=confirm_margin,
    )


def execute(token: str, orders: Iterable[Order], held_lots: Mapping[str, int],
            dry_run: bool = False, confirm_margin: bool = False) -> int:
    """Place every order, sells first so freed cash is available for the buys.

    Returns how many orders actually went through. A single failure is logged
    and skipped rather than aborting the cycle — the next cycle recomputes from
    the real portfolio anyway, so a missed order self-heals.
    """
    orders = list(orders)
    if not orders:
        return 0

    with Client(token) as client:
        account_id = resolve_account_id(client)
        orders = drop_forbidden_shorts(client, orders, held_lots)
        sells = [o for o in orders if not o.is_buy]
        buys = [o for o in orders if o.is_buy]

        placed = 0
        for order in sells + buys:
            if dry_run:
                logger.info("[dry-run] %s %d lot(s) of %s",
                            "BUY" if order.is_buy else "SELL", abs(order.lots), order.figi)
                placed += 1
                continue
            try:
                post_order(client, account_id, order, confirm_margin)
                placed += 1
            except Exception:
                logger.exception("Unable to %s %s", "buy" if order.is_buy else "sell", order.figi)
        return placed
