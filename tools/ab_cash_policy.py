"""A/B the accumulate mode's cash policy: leave idle cash alone (v0.1) versus
deploying it into whatever lags its target the most.

Runs entirely offline, against the real `strategy.plan_orders` — the point is
to compare policies through the code that will actually trade, not through a
separate model of it. Prices, deposits and dividends come from a fixed seed, so
both policies see byte-identical conditions and the comparison is reproducible.

    python tools/ab_cash_policy.py

TODO: synthetic prices only show the mechanism, not which policy is better on
the real thing. Before treating this as an answer, replay an actual series:
`operations.get_operations()` over the master account for its composition
changes, plus historical candles for the instruments it holds. The harness
below takes prices as an injected sequence precisely so that swap is small.
"""

from __future__ import annotations

import math
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bitinvest.portfolio import MasterView, Position, Snapshot, TargetPosition  # noqa: E402
from bitinvest.settings import AccumulateSettings, Settings  # noqa: E402
from bitinvest.strategy import plan_orders  # noqa: E402

COMMISSION = 0.003  # ~0.3%, "Инвестор" tariff order of magnitude
DAYS = 750
DEPOSIT_EVERY = 21
DEPOSIT = 20_000.0
DIVIDEND_EVERY = 63
DIVIDEND_YIELD = 0.02  # per payout, on the value held
START_CASH = 300_000.0
SEED = 20260728

INSTRUMENTS = ["SBER", "LKOH", "GAZP", "MTSS", "PHOR", "ROSN"]
LOT_SIZE = {"SBER": 10, "LKOH": 1, "GAZP": 10, "MTSS": 10, "PHOR": 1, "ROSN": 10}


@dataclass
class Account:
    cash: float
    lots: dict[str, int] = field(default_factory=dict)
    trades: int = 0
    turnover: float = 0.0
    commission: float = 0.0

    def snapshot(self, prices: dict[str, float]) -> Snapshot:
        positions = tuple(
            Position(figi=figi, lot_size=LOT_SIZE[figi], price=prices[figi],
                     quantity=lots * LOT_SIZE[figi], ticker=figi, instrument_uid=f"uid-{figi}")
            for figi, lots in sorted(self.lots.items())
            if lots
        )
        return Snapshot(positions=positions, cash=self.cash)

    def apply(self, orders, prices: dict[str, float]) -> None:
        for order in orders:
            value = order.lots * order.lot_size * prices[order.figi]
            self.cash -= value
            self.cash -= abs(value) * COMMISSION
            self.commission += abs(value) * COMMISSION
            self.turnover += abs(value)
            self.trades += 1
            self.lots[order.figi] = self.lots.get(order.figi, 0) + order.lots


def price_series(rng: random.Random) -> list[dict[str, float]]:
    """Daily geometric random walk, mild drift, per instrument."""
    prices = {figi: 100.0 + 40 * rng.random() for figi in INSTRUMENTS}
    series = [dict(prices)]
    for _ in range(DAYS):
        for figi in INSTRUMENTS:
            drift, vol = 0.0003, 0.018
            prices[figi] *= math.exp(drift - vol * vol / 2 + vol * rng.gauss(0, 1))
        series.append(dict(prices))
    return series


def master_weights(day: int) -> dict[str, float]:
    """A buy-and-hold master that reshuffles twice over three years."""
    if day < DAYS // 3:
        held = INSTRUMENTS[:4]
    elif day < 2 * DAYS // 3:
        held = INSTRUMENTS[1:5]
    else:
        held = INSTRUMENTS[2:]
    return {figi: 0.95 / len(held) for figi in held}


def master_view(day: int, prices: dict[str, float]) -> MasterView:
    return MasterView(
        positions=tuple(
            TargetPosition(figi=figi, lot_size=LOT_SIZE[figi], price=prices[figi],
                           weight=weight, ticker=figi, instrument_uid=f"uid-{figi}")
            for figi, weight in master_weights(day).items()
        ),
        equity=1_000_000.0,
    )


def run(policy: str, series: list[dict[str, float]]) -> Account:
    settings = Settings(
        mode="accumulate",
        min_order_value=1000.0,
        accumulate=AccumulateSettings(deploy_free_cash=policy),
    )
    account = Account(cash=START_CASH)

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
        orders = plan_orders(master_view(day, prices), snapshot, settings)
        account.apply(orders, prices)

    return account


POLICIES = ("never", "underweight_first", "proportional")


def run_all(seed: int = SEED) -> dict[str, Account]:
    """One fresh synthetic price path, all three policies replayed against it.
    Used both by main() below and by tools/ab_runner.py's continuous slot."""
    series = price_series(random.Random(seed))
    return {policy: run(policy, series) for policy in POLICIES}


def main() -> None:
    accounts = run_all(SEED)
    series = price_series(random.Random(SEED))
    final = series[-1]
    deposited = START_CASH + DEPOSIT * (DAYS // DEPOSIT_EVERY)

    print(f"{DAYS} trading days, {deposited:,.0f} RUB paid in, commission {COMMISSION:.2%}\n")
    header = f"{'policy':<20}{'equity':>14}{'cash idle':>12}{'trades':>8}{'turnover':>14}{'fees':>10}"
    print(header)
    print("-" * len(header))

    for policy in POLICIES:
        account = accounts[policy]
        equity = account.snapshot(final).equity
        idle = account.cash / equity * 100 if equity else 0.0
        print(f"{policy:<20}{equity:>14,.0f}{idle:>11.1f}%{account.trades:>8}"
              f"{account.turnover:>14,.0f}{account.commission:>10,.0f}")

    print("\nSynthetic prices — this shows the mechanism, not the verdict. See the TODO at the top.")


if __name__ == "__main__":
    main()
