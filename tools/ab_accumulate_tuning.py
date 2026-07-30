"""A/B three tuning knobs inside `accumulate` mode that shipped with a guessed
default and were never swept against alternatives:

`trim_threshold_pct` — how far a position must overweight its target before a
trim fires at all (the deadband that stops ordinary price drift from causing
churn). Shipped default: 25%.

`cash_buffer_pct` — share of equity walled off from `deploy_free_cash`.
Shipped default: 0% (deploy everything).

`min_order_value` — the dust filter: orders below this value are skipped.
Shared with `mirror` mode too, but exercised here since `accumulate` is this
file's baseline scenario. Shipped default: 1000 RUB.

Each grid is swept independently — the other two knobs held at their shipped
default — against the same reshuffling master and price path as
tools/ab_cash_policy.py, with `deploy_free_cash` fixed at "underweight_first".

    python tools/ab_accumulate_tuning.py

`run_all_real()` replays the same three sweeps over a real closing-price
series (see tools/ab_runner.py's shared real-basket slot); the master's
composition changes are still invented, same caveat as this file's siblings.
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

# Shipped defaults are always included in their own grid, so "no change from
# config.toml" is one of the compared points, not just a baseline off to the side.
TRIM_GRID = (10.0, 25.0, 50.0)                    # config.toml default: 25.0
BUFFER_GRID = (0.0, 5.0, 15.0)                    # config.toml default: 0.0
MIN_ORDER_GRID = (0.0, 1000.0, 3000.0, 8000.0)    # config.toml default: 1000.0

AXES = ("trim_threshold_pct", "cash_buffer_pct", "min_order_value")


def _settings(trim: float = 25.0, buffer: float = 0.0, min_order: float = 1000.0) -> Settings:
    return Settings(
        mode="accumulate",
        min_order_value=min_order,
        accumulate=AccumulateSettings(
            deploy_free_cash="underweight_first",
            trim_threshold_pct=trim,
            cash_buffer_pct=buffer,
        ),
    )


def run(settings: Settings, series: list[dict[str, float]]) -> Account:
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


def _label(value: float) -> str:
    return f"{value:g}"


def _sweep(series: list[dict[str, float]]) -> dict[str, dict[str, Account]]:
    return {
        "trim_threshold_pct": {_label(v): run(_settings(trim=v), series) for v in TRIM_GRID},
        "cash_buffer_pct": {_label(v): run(_settings(buffer=v), series) for v in BUFFER_GRID},
        "min_order_value": {_label(v): run(_settings(min_order=v), series) for v in MIN_ORDER_GRID},
    }


def run_all(seed: int = SEED) -> dict[str, dict[str, Account]]:
    """One fresh synthetic price path, all three grids replayed against it.
    Used both by main() below and by tools/ab_runner.py's continuous slot."""
    series = price_series(random.Random(seed))
    return _sweep(series)


def run_all_real(prices_by_ticker: dict[str, list[float]]) -> dict[str, dict[str, Account]]:
    """Same three grids, replayed over a real closing-price series."""
    series = real_series_from_basket(prices_by_ticker)
    return _sweep(series)


def main() -> None:
    grids = run_all(SEED)
    series = price_series(random.Random(SEED))
    final = series[-1]
    deposited = START_CASH + DEPOSIT * (DAYS // DEPOSIT_EVERY)

    print(f"{DAYS} trading days, {deposited:,.0f} RUB paid in, commission {COMMISSION:.2%}\n")
    header = f"{'value':<10}{'equity':>14}{'cash idle':>12}{'trades':>8}{'turnover':>14}{'fees':>10}"

    for axis in AXES:
        print(f"-- {axis} --")
        print(header)
        print("-" * len(header))
        for label, account in grids[axis].items():
            equity = account.snapshot(final).equity
            idle = account.cash / equity * 100 if equity else 0.0
            print(f"{label:<10}{equity:>14,.0f}{idle:>11.1f}%{account.trades:>8}"
                  f"{account.turnover:>14,.0f}{account.commission:>10,.0f}")
        print()

    print("Synthetic prices — this shows the mechanism, not the verdict. See documentation/ab-tests.md.")


if __name__ == "__main__":
    main()
