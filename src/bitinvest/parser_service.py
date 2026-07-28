from __future__ import annotations

import logging

from bitinvest import config
from bitinvest.portfolio import parse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    master = config.load_master_config()
    df = parse(master.tokens, master.weights)
    step_df = df[["figi", "lot_size", "percentage", "price"]]

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    step_df.to_csv(config.STEP_CSV, index=False)
    logger.info("Parsed master portfolio: %d instruments -> %s", len(step_df), config.STEP_CSV)


if __name__ == "__main__":
    main()
