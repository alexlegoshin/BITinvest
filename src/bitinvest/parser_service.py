"""Entrypoint for the parser host: read the master accounts, publish step.json.

This host holds only master tokens and never places an order — which is the
whole point of keeping the two roles on physically separate machines.
"""

from __future__ import annotations

import logging

from bitinvest import config
from bitinvest.portfolio import fetch_master_view
from bitinvest.stepfile import dump

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    master = config.load_master_config()
    view = fetch_master_view(master.tokens, master.weights)

    logger.info(
        "master: %d position(s), equity %.2f RUB, gross leverage %.2f, cash weight %.1f%%",
        len(view.positions), view.equity, view.gross_leverage, view.cash_weight * 100,
    )
    dump(view, config.STEP_FILE)
    logger.info("wrote %s", config.STEP_FILE)


if __name__ == "__main__":
    main()
