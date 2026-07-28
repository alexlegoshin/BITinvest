"""Turning a master snapshot into orders for the slave account.

Two modes, one shared sizing model.

``mirror``
    Symmetric: chase the target weights with both buys and sells.

``accumulate``
    The original dividend idea, made explicit. A position already held is
    never topped up just because its target weight drifted upward; a position
    the master newly opened is bought in full; selling happens only when the
    master actually cut the position, past a threshold so ordinary price drift
    does not trigger churn. Spare cash is deployed by buying into whatever lags
    its target the most — never by selling something else.

    (v0.1 did the "buy new, never add to old" part by accident: its deadband
    `delta < delta*0.75 or delta > delta*1.25` is false for every positive
    delta, so top-ups could not fire. Same behaviour, now on purpose and
    without the accidental 1-lot-per-cycle liquidation drip.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping

from bitinvest import leverage
from bitinvest.portfolio import MasterView, Snapshot
from bitinvest.settings import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Order:
    figi: str
    lots: int  # signed: positive buys, negative sells
    price: float
    lot_size: int
    reason: str
    ticker: str = ""
    instrument_uid: str = ""

    @property
    def value(self) -> float:
        return abs(self.lots) * self.lot_size * self.price

    @property
    def is_buy(self) -> bool:
        return self.lots > 0


@dataclass(frozen=True)
class _Meta:
    lot_size: int
    price: float
    ticker: str
    instrument_uid: str


def _lots_for(value: float, meta: _Meta) -> int:
    """Whole lots fitting into a value, truncated **toward zero**.

    Floor division would be wrong on the short side: -2.4 lots would floor to
    -3, i.e. a bigger short than intended. Truncation always errs toward the
    smaller position, long or short.
    """
    unit = meta.price * meta.lot_size
    if unit <= 0:
        return 0
    return int(value / unit)


def _collect_meta(master: MasterView, slave: Snapshot) -> dict[str, _Meta]:
    meta: dict[str, _Meta] = {}
    for t in master.positions:
        meta[t.figi] = _Meta(t.lot_size, t.price, t.ticker, t.instrument_uid)
    # Prices from our own account win: it is the book we actually trade against.
    for p in slave.positions:
        known = meta.get(p.figi)
        meta[p.figi] = _Meta(
            p.lot_size,
            p.price,
            p.ticker or (known.ticker if known else ""),
            p.instrument_uid or (known.instrument_uid if known else ""),
        )
    return meta


def target_weights(master: MasterView, settings: Settings,
                   margin_factor: float = 1.0) -> tuple[dict[str, float], float]:
    """Master weights, short-filtered and leverage-normalised.

    Returns the scaled weights and the factor applied, so callers can log it.
    Cash is deliberately absent: it is whatever 1 − Σweights leaves over.
    """
    weights = {t.figi: t.weight for t in master.positions}
    if not settings.allow_short:
        dropped = [figi for figi, w in weights.items() if w < 0]
        if dropped:
            logger.info("allow_short is off — skipping short legs: %s", ", ".join(dropped))
        weights = leverage.drop_shorts(weights)

    gross = leverage.gross_leverage(weights)
    factor = leverage.scale_factor(gross, settings.target_leverage, settings.leverage_policy)
    factor *= margin_factor
    logger.info(
        "master gross leverage %.2f -> factor %.4f (target %.2f, policy %s, margin %.4f)",
        gross, factor, settings.target_leverage, settings.leverage_policy, margin_factor,
    )
    return leverage.apply_scale(weights, factor), factor


def target_values(master: MasterView, slave_equity: float, settings: Settings,
                  margin_factor: float = 1.0) -> dict[str, float]:
    weights, _ = target_weights(master, settings, margin_factor)
    return {figi: w * slave_equity for figi, w in weights.items()}


def _emit(figi: str, meta: _Meta, current_lots: int, target_lots: int, reason: str) -> list[Order]:
    """One order, or two when the position flips sign.

    Going from +5 lots to -3 is a close and an open. Splitting keeps the log
    honest about what happened and avoids a single order that has to cross zero
    under the broker's margin checks.
    """
    if current_lots == target_lots:
        return []
    crosses_zero = current_lots * target_lots < 0
    if not crosses_zero:
        return [Order(figi, target_lots - current_lots, meta.price, meta.lot_size, reason,
                      meta.ticker, meta.instrument_uid)]
    return [
        Order(figi, -current_lots, meta.price, meta.lot_size, f"{reason} (close)",
              meta.ticker, meta.instrument_uid),
        Order(figi, target_lots, meta.price, meta.lot_size, f"{reason} (reverse)",
              meta.ticker, meta.instrument_uid),
    ]


def _keep(order: Order, settings: Settings, liquidation: bool) -> bool:
    """Dust filter. Full exits are exempt: leaving a stub of something the
    master no longer holds is worse than paying commission on a small trade."""
    return liquidation or order.value >= settings.min_order_value


def plan_orders(master: MasterView, slave: Snapshot, settings: Settings,
                margin_factor: float = 1.0) -> list[Order]:
    equity = slave.equity
    if equity <= 0:
        raise ValueError("Slave equity is not positive — refusing to trade")

    meta = _collect_meta(master, slave)
    targets = target_values(master, equity, settings, margin_factor)
    held = {p.figi: round(p.lots) for p in slave.positions}

    if settings.mode == "mirror":
        orders = _plan_mirror(targets, held, meta, settings)
    elif settings.mode == "accumulate":
        orders = _plan_accumulate(targets, held, meta, settings, slave)
    else:  # pragma: no cover - Settings validates this
        raise ValueError(f"Unknown mode: {settings.mode!r}")

    for order in orders:
        logger.info("%s %d lot(s) of %s (%s) ≈ %.2f RUB — %s",
                    "BUY " if order.is_buy else "SELL", abs(order.lots),
                    order.ticker or order.figi, order.figi, order.value, order.reason)
    return orders


def _plan_mirror(targets: Mapping[str, float], held: Mapping[str, int],
                 meta: Mapping[str, _Meta], settings: Settings) -> list[Order]:
    orders: list[Order] = []
    for figi in sorted(set(targets) | set(held)):
        target_lots = _lots_for(targets.get(figi, 0.0), meta[figi])
        current_lots = held.get(figi, 0)
        liquidation = target_lots == 0 and current_lots != 0
        reason = "liquidate" if liquidation else "mirror"
        for order in _emit(figi, meta[figi], current_lots, target_lots, reason):
            if _keep(order, settings, liquidation):
                orders.append(order)
    return orders


def _plan_accumulate(targets: Mapping[str, float], held: Mapping[str, int],
                     meta: Mapping[str, _Meta], settings: Settings,
                     slave: Snapshot) -> list[Order]:
    trim_factor = 1 + settings.accumulate.trim_threshold_pct / 100
    orders: list[Order] = []

    for figi in sorted(set(targets) | set(held)):
        target_value = targets.get(figi, 0.0)
        target_lots = _lots_for(target_value, meta[figi])
        current_lots = held.get(figi, 0)
        current_value = current_lots * meta[figi].lot_size * meta[figi].price

        if current_lots == 0:
            reason, wanted = "new position", target_lots
        elif figi not in targets:
            # The master is out of it entirely — the whole position goes, in one
            # order rather than v0.1's one lot per cycle.
            reason, wanted = "liquidate", 0
        elif current_lots * target_lots < 0:
            # The master flipped long/short — a stance change, not drift.
            reason, wanted = "reversal", target_lots
        elif abs(current_value) > abs(target_value) * trim_factor:
            reason, wanted = "trim", target_lots
        else:
            # Held, within tolerance: never topped up on drift alone.
            continue

        liquidation = wanted == 0 and current_lots != 0
        for order in _emit(figi, meta[figi], current_lots, wanted, reason):
            if _keep(order, settings, liquidation):
                orders.append(order)

    orders += _deploy_free_cash(targets, held, meta, settings, slave, orders)
    return orders


def _deploy_free_cash(targets: Mapping[str, float], held: Mapping[str, int],
                      meta: Mapping[str, _Meta], settings: Settings, slave: Snapshot,
                      planned: list[Order]) -> list[Order]:
    """Put idle cash to work without selling anything.

    Without this, `accumulate` parks every deposit and dividend forever
    whenever the master's composition happens to be unchanged — the loose end
    in the v0.1 idea. Only long legs are considered: opening a short raises
    cash rather than spending it, and is not a way to invest a balance.
    """
    policy = settings.accumulate.deploy_free_cash
    if policy == "never":
        return []

    cash = slave.cash - sum(o.lots * o.lot_size * o.price for o in planned)
    investable = cash - slave.equity * settings.accumulate.cash_buffer_pct / 100
    if investable < settings.min_order_value:
        return []

    planned_lots: dict[str, int] = {}
    for o in planned:
        planned_lots[o.figi] = planned_lots.get(o.figi, 0) + o.lots

    gaps: dict[str, float] = {}
    for figi, target_value in targets.items():
        if target_value <= 0:
            continue
        lots_now = held.get(figi, 0) + planned_lots.get(figi, 0)
        gap = target_value - lots_now * meta[figi].lot_size * meta[figi].price
        if gap > 0:
            gaps[figi] = gap
    if not gaps:
        return []

    if policy == "proportional":
        total_gap = sum(gaps.values())
        budgets = {figi: investable * gap / total_gap for figi, gap in gaps.items()}
        order_of_figis = sorted(gaps, key=lambda f: -gaps[f])
    else:  # underweight_first
        budgets = dict(gaps)
        order_of_figis = sorted(gaps, key=lambda f: -gaps[f])

    orders: list[Order] = []
    for figi in order_of_figis:
        budget = min(budgets[figi], gaps[figi], investable)
        lots = _lots_for(budget, meta[figi])
        if lots <= 0:
            continue
        order = Order(figi, lots, meta[figi].price, meta[figi].lot_size, f"deploy cash ({policy})",
                      meta[figi].ticker, meta[figi].instrument_uid)
        if order.value < settings.min_order_value:
            continue
        orders.append(order)
        investable -= order.value
        if investable < settings.min_order_value:
            break
    return orders
