"""A/B `leverage_policy`: "cap" (mirror the master's own leverage exactly,
only ever scale down) versus "normalize" (always land exactly on
`target_leverage`, deploying whatever cash the master chose to leave idle).

The two policies are identical whenever the master's own gross exposure is at
or above `target_leverage` — see the docstring of `bitinvest.leverage.
scale_factor`. They only diverge when the master sits partly in cash, so the
harness below drives a master whose gross exposure drifts over time (a slow
random walk in [0.35, 1.0]) instead of always being fully invested — that's
the one regime where this question has an answer to give.

Runs the real `strategy.plan_orders` (mirror mode) day by day, reusing
tools/ab_cash_policy.py's price series and account bookkeeping — the
comparison depends on how leverage.scale_factor reacts to master exposure,
not on anything specific to the cash-policy harness.

    python tools/ab_leverage_policy.py

TODO before this is decision-grade: the master's own gross exposure here is a
synthetic random walk, not a real master account's history. If a real master
is almost always fully invested, cap/normalize are moot in practice — that's
worth checking before spending more effort calibrating this.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bitinvest.portfolio import MasterView, Snapshot, TargetPosition  # noqa: E402
from bitinvest.settings import Settings  # noqa: E402
from bitinvest.strategy import plan_orders  # noqa: E402

from ab_cash_policy import (  # noqa: E402
    COMMISSION, DAYS, INSTRUMENTS, LOT_SIZE, START_CASH, Account, price_series,
)

SEED = 20260730
TARGET_LEVERAGE = 1.0
POLICIES = ("cap", "normalize")


def master_gross_series(rng: random.Random, days: int) -> list[float]:
    """The master's own chosen gross exposure per day, in [0.35, 1.0] — the
    only axis "cap" and "normalize" disagree on. A slow walk, not noise: a
    real master doesn't flip between fully invested and half-cash daily."""
    gross = 0.85
    series = [gross]
    for _ in range(days):
        gross = min(1.0, max(0.35, gross + rng.uniform(-0.03, 0.03)))
        series.append(gross)
    return series


def master_view(day: int, prices: dict[str, float], gross_series: list[float]) -> MasterView:
    """A buy-and-hold master (same reshuffle as ab_cash_policy) whose total
    exposure that day is gross_series[day] instead of always ~0.95."""
    held = INSTRUMENTS[:4] if day < DAYS // 2 else INSTRUMENTS[2:]
    weight = gross_series[day] / len(held)
    return MasterView(
        positions=tuple(
            TargetPosition(figi=figi, lot_size=LOT_SIZE[figi], price=prices[figi],
                           weight=weight, ticker=figi, instrument_uid=f"uid-{figi}")
            for figi in held
        ),
        equity=1_000_000.0,
    )


def run(policy: str, series: list[dict[str, float]], gross_series: list[float]) -> Account:
    settings = Settings(mode="mirror", min_order_value=1000.0,
                        target_leverage=TARGET_LEVERAGE, leverage_policy=policy)
    account = Account(cash=START_CASH)

    for day, prices in enumerate(series):
        snapshot = account.snapshot(prices)
        if snapshot.equity <= 0:
            continue
        orders = plan_orders(master_view(day, prices, gross_series), snapshot, settings)
        account.apply(orders, prices)

    return account


def run_all(seed: int = SEED) -> dict[str, Account]:
    """One fresh (price, master-exposure) path, both policies replayed
    against it. Used both by main() below and tools/ab_runner.py's slot."""
    series = price_series(random.Random(seed))
    gross_series = master_gross_series(random.Random(seed + 1), DAYS)
    return {policy: run(policy, series, gross_series) for policy in POLICIES}


def main() -> None:
    accounts = run_all(SEED)
    series = price_series(random.Random(SEED))
    final = series[-1]

    print(f"{DAYS} trading days, target_leverage={TARGET_LEVERAGE}, "
          f"master gross exposure drifting in [0.35, 1.0], commission {COMMISSION:.2%}\n")
    header = f"{'policy':<14}{'equity':>14}{'cash idle':>12}{'trades':>8}{'turnover':>14}{'fees':>10}"
    print(header)
    print("-" * len(header))

    for policy in POLICIES:
        account = accounts[policy]
        equity = account.snapshot(final).equity
        idle = account.cash / equity * 100 if equity else 0.0
        print(f"{policy:<14}{equity:>14,.0f}{idle:>11.1f}%{account.trades:>8}"
              f"{account.turnover:>14,.0f}{account.commission:>10,.0f}")

    print("\nSynthetic prices and a synthetic master exposure walk — this shows the")
    print("mechanism, not the verdict. See the TODO at the top of this file.")


if __name__ == "__main__":
    main()
