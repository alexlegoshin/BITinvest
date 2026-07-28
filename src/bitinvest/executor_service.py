"""Entrypoint for the executor host: read step.json, trade the slave account."""

from __future__ import annotations

import logging

from t_tech.invest import Client

from bitinvest import config, margin, strategy, trading
from bitinvest.broker import resolve_account_id
from bitinvest.portfolio import snapshot_from_portfolio
from bitinvest.settings import load_settings
from bitinvest.stepfile import StaleStepFile, load

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _margin_factor(client, account_id, master, slave, settings) -> float:
    """How much further to shrink the target so the projected starting margin
    stays inside the liquid portfolio. 1.0 means the margin rules do not bind."""
    weights, factor = strategy.target_weights(master, settings)
    if not margin.needs_margin_check(weights, 1.0):
        return 1.0

    state = margin.fetch_margin_state(client, account_id)
    if state is None:
        # No margin regime on this account: shorts and borrowing are impossible
        # regardless of what the config asks for.
        logger.warning("Account has no margin regime — margin-requiring targets will be rejected")
        return 1.0
    if state.margin_call:
        logger.error("Account is in margin call — only risk-reducing orders should be placed")
    elif not state.can_open_new:
        logger.warning("Starting margin already exceeds the liquid portfolio — no new positions")

    uid_by_figi = {t.figi: t.instrument_uid for t in master.positions}
    rates = margin.fetch_risk_rates(client, uid_by_figi.values())
    values = {
        uid_by_figi[figi]: w * slave.equity
        for figi, w in weights.items()
        if uid_by_figi.get(figi)
    }
    cap = margin.margin_cap(values, rates, state.liquid_portfolio, settings.margin_safety)
    if cap >= 1.0:
        return 1.0
    logger.warning("Margin requirements cap the target at %.1f%% of the requested size", cap * 100)
    return cap


def main() -> None:
    settings = load_settings()
    slave = config.load_slave_config()
    if len(slave.tokens) > 1:
        raise ValueError(
            f"{len(slave.tokens)} slave tokens configured, but the executor trades exactly one "
            "account: orders are computed against the combined portfolio and would be replayed "
            "per token, doubling every trade. Use one slave token per deployment."
        )
    token = slave.tokens[0]

    try:
        master = load(config.STEP_FILE, max_age_sec=settings.max_step_age_sec)
    except StaleStepFile as exc:
        logger.error("%s", exc)
        return

    with Client(token) as client:
        account_id = resolve_account_id(client)
        snapshot = snapshot_from_portfolio(client.operations.get_portfolio(account_id=account_id))
        factor = _margin_factor(client, account_id, master, snapshot, settings)

    logger.info("slave: equity %.2f RUB, cash %.2f RUB, %d position(s)",
                snapshot.equity, snapshot.cash, len(snapshot.positions))

    orders = strategy.plan_orders(master, snapshot, settings, margin_factor=factor)
    if not orders:
        logger.info("nothing to do")
        return

    held_lots = {p.figi: round(p.lots) for p in snapshot.positions}
    confirm_margin = settings.allow_short or settings.target_leverage > 1.0
    placed = trading.execute(token, orders, held_lots,
                             dry_run=settings.dry_run, confirm_margin=confirm_margin)
    logger.info("%d of %d order(s) placed%s", placed, len(orders), " (dry run)" if settings.dry_run else "")


if __name__ == "__main__":
    main()
