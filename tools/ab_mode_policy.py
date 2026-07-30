"""A/B `mode`: "mirror" (symmetric — chase target weights with both buys and
sells) versus "accumulate" (never trim on drift alone, buy new positions in
full, sell only on a real cut past `trim_threshold_pct`, deploy idle cash
separately).

This is the single largest fork in `strategy.py` — a different algorithm
entirely, not a tuning knob — and unlike `leverage_policy`/`deploy_free_cash`
it had no A/B coverage at all before this file: `deploy_free_cash` only
matters under `accumulate` in the first place, so comparing cash policies
never told us whether `accumulate` itself beats `mirror`, or vice versa.

Reuses ab_cash_policy's account bookkeeping and reshuffling master — the
comparison is about the rebalancing algorithm, not about a different market
scenario. `accumulate` runs with `deploy_free_cash="underweight_first"`, the
policy `tools/ab_cash_policy.py`'s own continuous run already favours, so this
is "accumulate at its best" versus "mirror", not accumulate crippled by a
worse cash policy.

    python tools/ab_mode_policy.py

`run_all_real()` replays the same schedule over a real closing-price series
(see tools/ab_runner.py's shared real-basket slot); the master's composition
changes are still invented, same caveat as the other *_real() variants here.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bitinvest.settings import AccumulateSettings, Settings  # noqa: E402
from bitinvest.strategy import plan_orders  # noqa: E402

from ab_cash_policy import (  # noqa: E402
    COMMISSION, DAYS, DEPOSIT, DEPOSIT_EVERY, DIVIDEND_EVERY, DIVIDEND_YIELD,
    INSTRUMENTS, LOT_SIZE, START_CASH, Account, master_view, price_series,
    real_series_from_basket,
)

SEED = 20260730
MODES = ("mirror", "accumulate")


def run(mode: str, series: list[dict[str, float]]) -> Account:
    settings = Settings(
        mode=mode,
        min_order_value=1000.0,
        accumulate=AccumulateSettings(deploy_free_cash="underweight_first"),
    )
    account = Account(cash=START_CASH)
    total_days = len(series) - 1
    instruments = [t for t in INSTRUMENTS if t in series[0]]

    for day, prices in enumerate(series):
        if day and day % DEPOSIT_EVERY == 0:
            account.cash += DEPOSIT
        if day and day % DIVIDEND_EVERY == 0:
            account.cash += sum(
                lots * LOT_SIZE[figi] * prices[figi] * DIVIDEND_YIELD
                for figi, lots in account.lots.items()
            )
        snapshot = account.snapshot(prices)
        if snapshot.equity <= 0:
            continue
        orders = plan_orders(master_view(day, prices, total_days, instruments), snapshot, settings)
        account.apply(orders, prices)

    return account


def run_all(seed: int = SEED) -> dict[str, Account]:
    """One fresh synthetic price path, both modes replayed against it. Used
    both by main() below and by tools/ab_runner.py's continuous slot."""
    series = price_series(random.Random(seed))
    return {mode: run(mode, series) for mode in MODES}


def run_all_real(prices_by_ticker: dict[str, list[float]]) -> dict[str, Account]:
    """Same schedule as run_all(), replayed over a real closing-price series."""
    series = real_series_from_basket(prices_by_ticker)
    return {mode: run(mode, series) for mode in MODES}


def main() -> None:
    accounts = run_all(SEED)
    series = price_series(random.Random(SEED))
    final = series[-1]
    deposited = START_CASH + DEPOSIT * (DAYS // DEPOSIT_EVERY)

    print(f"{DAYS} trading days, {deposited:,.0f} RUB paid in, commission {COMMISSION:.2%}, "
          f"accumulate.deploy_free_cash=underweight_first\n")
    header = f"{'mode':<14}{'equity':>14}{'cash idle':>12}{'trades':>8}{'turnover':>14}{'fees':>10}"
    print(header)
    print("-" * len(header))

    for mode in MODES:
        account = accounts[mode]
        equity = account.snapshot(final).equity
        idle = account.cash / equity * 100 if equity else 0.0
        print(f"{mode:<14}{equity:>14,.0f}{idle:>11.1f}%{account.trades:>8}"
              f"{account.turnover:>14,.0f}{account.commission:>10,.0f}")

    print("\nSynthetic prices — this shows the mechanism, not the verdict. See documentation/ab-tests.md.")


if __name__ == "__main__":
    main()
