from __future__ import annotations

import logging

import pandas as pd

from bitinvest import config, trading
from bitinvest.portfolio import parse
from bitinvest.rebalance import check_deltas

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    slave = config.load_slave_config()

    master_df = pd.read_csv(config.STEP_CSV)
    slave_df = parse(slave.tokens, slave.weights)

    orders = check_deltas(master_df, slave_df)
    logger.info("%d order(s) to place", len(orders))

    # Designed for a single slave token/account: check_deltas() above compares
    # the *combined* slave portfolio to the target. With more than one slave
    # token, the same order set gets replayed once per token below, which
    # over-executes. Use exactly one slave token per deployment.
    for token in slave.tokens:
        for _, order in orders[orders.quantity < 0].iterrows():
            try:
                trading.sell(token, order.figi, abs(order.quantity))
            except Exception:
                logger.exception("Unable to sell %s", order.figi)
        for _, order in orders[orders.quantity > 0].iterrows():
            try:
                trading.buy(token, order.figi, order.quantity)
            except Exception:
                logger.exception("Unable to buy %s", order.figi)


if __name__ == "__main__":
    main()
