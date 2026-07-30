"""A/B the accumulate mode's cash policy: leave idle cash alone (v0.1) versus
deploying it into whatever lags its target the most.

Runs entirely offline, against the real `strategy.plan_orders` — the point is
to compare policies through the code that will actually trade, not through a
separate model of it. Prices, deposits and dividends come from a fixed seed, so
both policies see byte-identical conditions and the comparison is reproducible.

    python tools/ab_cash_policy.py

`run_all_real()` closes half of the original TODO: it replays the same
reshuffle schedule over a real closing-price series (see tools/ab_runner.py's
shared real-basket slot) instead of a synthetic random walk. What is still
synthetic is the master's *composition changes* — real prices, invented
rebalancing dates — since there is no real master trading history to replay
yet (only market data, via the sandbox token). `operations.get_operations()`
over an actual master account would close the rest, once one exists.
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


def master_weights(day: int, total_days: int = DAYS,
                   instruments: list[str] = INSTRUMENTS) -> dict[str, float]:
    """A buy-and-hold master that reshuffles twice over the run.

    Parametrised on `total_days`/`instruments` (defaulting to the synthetic
    globals) so the same schedule replays over a real price series of whatever
    length actually came back from the API — see `real_series_from_basket`.
    `window` is picked so the default 6-instrument case slices exactly as
    before: [:4], [1:5], [2:6].
    """
    n = len(instruments)
    window = max(1, n - 2)
    if day < total_days // 3:
        start = 0
    elif day < 2 * total_days // 3:
        start = 1
    else:
        start = 2
    start = min(start, n - window)
    held = instruments[start:start + window]
    return {figi: 0.95 / len(held) for figi in held}


def master_view(day: int, prices: dict[str, float], total_days: int = DAYS,
               instruments: list[str] = INSTRUMENTS) -> MasterView:
    return MasterView(
        positions=tuple(
            TargetPosition(figi=figi, lot_size=LOT_SIZE[figi], price=prices[figi],
                           weight=weight, ticker=figi, instrument_uid=f"uid-{figi}")
            for figi, weight in master_weights(day, total_days, instruments).items()
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
    total_days = len(series) - 1
    # Filtered, not re-derived: keeps INSTRUMENTS' order (and the exact
    # reshuffle windows above) for the synthetic 6-ticker case, and degrades
    # to whichever subset a real basket fetch actually returned.
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


def real_series_from_basket(prices_by_ticker: dict[str, list[float]]) -> list[dict[str, float]]:
    """Per-ticker close lists (already truncated to a common length by the
    caller, see tools/ab_runner.py) -> the list-of-day-dicts shape `run()`
    expects. Shared by every *_real() variant across the A/B tools."""
    length = len(next(iter(prices_by_ticker.values())))
    return [{t: prices_by_ticker[t][i] for t in prices_by_ticker} for i in range(length)]


POLICIES = ("never", "underweight_first", "proportional")


def run_all(seed: int = SEED) -> dict[str, Account]:
    """One fresh synthetic price path, all three policies replayed against it.
    Used both by main() below and by tools/ab_runner.py's continuous slot."""
    series = price_series(random.Random(seed))
    return {policy: run(policy, series) for policy in POLICIES}


def run_all_real(prices_by_ticker: dict[str, list[float]]) -> dict[str, Account]:
    """Same schedule as run_all(), replayed over a real closing-price series
    instead of a synthetic random walk (see tools/ab_runner.py's shared
    real-basket slot)."""
    series = real_series_from_basket(prices_by_ticker)
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
