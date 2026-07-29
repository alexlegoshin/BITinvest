"""A/B `liquidation_mode`: dump a position the master dropped in one order
("full") versus easing out of it over several cycles ("gradual", the default
— see the docstring in `bitinvest.strategy` for why it defaults that way).

Runs the real `strategy.plan_orders` day by day, for many random scenarios —
different position sizes against different average daily volumes ("different
pairs": liquidity varies enormously between instruments, so the comparison is
run across a wide, randomised spread of size-to-volume ratios rather than one
hand-picked example) — and compares the realized exit price under both
settings using a simple square-root market-impact model: temporary impact
scales with sqrt(order size / average daily volume), and a fraction of that
persists as a permanent price shift for the rest of the scenario. Both modes
are replayed against the *same* underlying random price path per scenario, so
the comparison is apples to apples: "full" only ever sees day 0's price and
day 0's impact; "gradual" trades smaller size (less impact per day) but stays
exposed to price drift across the cycles it takes to fully exit.

    python tools/ab_liquidation_policy.py [n_scenarios]

TODO before this is decision-grade, not just a mechanism demo:
  1. IMPACT_K and PERMANENT_FRACTION below are illustrative guesses, not
     calibrated against real order book depth or historical fills for the
     instruments this bot actually trades.
  2. Prices are a random walk, not a real historical series.
  3. Position sizes are drawn from a wide synthetic range; narrow it to what
     the account's actual position sizes look like once there's real data.
  4. `liquidation_step_pct` itself is fixed at the shipped default (25%) here
     — it is a further axis worth sweeping once the above are calibrated.

Meant to run unattended on the server across a large N, not to be trusted from
a single quick local run. Results should be written to documentation/ (see
documentation/ab-tests.md) so they don't get lost between sessions.
"""

from __future__ import annotations

import math
import random
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bitinvest.portfolio import MasterView, Position, Snapshot  # noqa: E402
from bitinvest.settings import Settings  # noqa: E402
from bitinvest.strategy import plan_orders  # noqa: E402

SEED = 20260728
DEFAULT_N = 300
MAX_CYCLES = 60          # trading days; generous enough for a 4000-lot position at 25%/cycle
IMPACT_K = 0.15           # temporary impact ~ IMPACT_K * sqrt(order lots / avg daily volume)
PERMANENT_FRACTION = 0.3  # share of that day's temporary impact that persists afterward
STEP_PCT = 25.0           # matches the shipped config.toml default


@dataclass
class Scenario:
    figi: str
    lot_size: int
    initial_lots: int
    avg_daily_volume: float  # lots/day
    start_price: float
    daily_vol: float
    drift: float

    @property
    def liquidity_ratio(self) -> float:
        """Position size relative to how much of the instrument trades in a
        day. The single number that stands in for "which pair this is"."""
        return self.initial_lots / self.avg_daily_volume


def make_scenarios(n: int, rng: random.Random) -> list[Scenario]:
    scenarios = []
    for i in range(n):
        lots = rng.randint(20, 4000)
        # Log-uniform: a position can be a small fraction of daily volume or
        # many multiples of it, with no regime favoured over another.
        ratio = math.exp(rng.uniform(math.log(0.05), math.log(50)))
        volume = max(1.0, lots / ratio)
        scenarios.append(Scenario(
            figi=f"PAIR-{i}",
            lot_size=1,
            initial_lots=lots,
            avg_daily_volume=volume,
            start_price=rng.uniform(20, 5000),
            daily_vol=rng.uniform(0.01, 0.045),
            drift=rng.uniform(-0.0005, 0.0005),
        ))
    return scenarios


def base_price_path(scenario: Scenario, rng: random.Random, days: int) -> list[float]:
    """Geometric random walk, independent of our own trading — the "true"
    price absent any impact from the position being unwound."""
    price = scenario.start_price
    path = [price]
    for _ in range(days):
        price *= math.exp(
            scenario.drift - scenario.daily_vol ** 2 / 2 + scenario.daily_vol * rng.gauss(0, 1)
        )
        path.append(price)
    return path


def run_scenario(scenario: Scenario, base: list[float], mode: str, step_pct: float = STEP_PCT) -> dict:
    settings = Settings(mode="mirror", min_order_value=0.0,
                        liquidation_mode=mode, liquidation_step_pct=step_pct)
    empty_master = MasterView(positions=(), equity=0.0)

    lots_held = scenario.initial_lots
    fair_value = lots_held * scenario.lot_size * base[0]
    permanent_drag = 1.0
    proceeds = 0.0
    day = 0

    while lots_held > 0 and day < min(MAX_CYCLES, len(base) - 1):
        mid = base[day] * permanent_drag
        snapshot = Snapshot(
            positions=(Position(scenario.figi, scenario.lot_size, mid, lots_held,
                                scenario.figi, f"uid-{scenario.figi}"),),
            cash=0.0,
        )
        orders = plan_orders(empty_master, snapshot, settings)
        if not orders:
            break  # should not happen while lots_held > 0, but don't loop forever
        order = orders[0]
        qty = abs(order.lots)
        impact_frac = min(0.5, IMPACT_K * math.sqrt(qty / scenario.avg_daily_volume))
        exec_price = mid * (1 - impact_frac)
        proceeds += qty * scenario.lot_size * exec_price
        permanent_drag *= (1 - PERMANENT_FRACTION * impact_frac)
        lots_held += order.lots  # order.lots is negative (a sell)
        day += 1

    return {
        "finished": lots_held == 0,
        "days": day,
        "proceeds": proceeds,
        "fair_value": fair_value,
        "cost_bps": (fair_value - proceeds) / fair_value * 1e4 if fair_value else 0.0,
        "liquidity_ratio": scenario.liquidity_ratio,
    }


def run_all(n: int, seed: int = SEED, step_pct: float = STEP_PCT) -> list[dict]:
    rng = random.Random(seed)
    scenarios = make_scenarios(n, rng)
    results = []
    for scenario in scenarios:
        base = base_price_path(scenario, rng, MAX_CYCLES + 1)
        full = run_scenario(scenario, base, "full", step_pct)
        gradual = run_scenario(scenario, base, "gradual", step_pct)
        results.append({"scenario": scenario, "full": full, "gradual": gradual})
    return results


def _bucket(results: list[dict], lo: float, hi: float) -> list[dict]:
    return [r for r in results if lo <= r["scenario"].liquidity_ratio < hi]


def _summarize(label: str, rows: list[dict]) -> None:
    if not rows:
        print(f"{label:<28}  (no scenarios in this bucket)")
        return
    full_bps = [r["full"]["cost_bps"] for r in rows]
    grad_bps = [r["gradual"]["cost_bps"] for r in rows]
    grad_days = [r["gradual"]["days"] for r in rows]
    wins = sum(1 for f, g in zip(full_bps, grad_bps) if g < f)
    print(
        f"{label:<28}  n={len(rows):<5} "
        f"full={statistics.mean(full_bps):>7.1f}bp  "
        f"gradual={statistics.mean(grad_bps):>7.1f}bp  "
        f"gradual-wins={wins}/{len(rows)}  "
        f"avg-days={statistics.mean(grad_days):>4.1f}"
    )


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_N
    results = run_all(n)

    unfinished = [r for r in results if not r["gradual"]["finished"] or not r["full"]["finished"]]
    if unfinished:
        print(f"WARNING: {len(unfinished)} scenario(s) did not fully liquidate within "
              f"{MAX_CYCLES} cycles — increase MAX_CYCLES if this is not a handful of outliers.\n")

    print(f"{n} scenario(s), impact model: IMPACT_K={IMPACT_K}, "
          f"PERMANENT_FRACTION={PERMANENT_FRACTION}, step={STEP_PCT}%\n")
    _summarize("all scenarios", results)
    print()
    print("by position-size / avg-daily-volume ratio (illiquid position for its size = harder to exit):")
    _summarize("  ratio < 1x (liquid)", _bucket(results, 0, 1))
    _summarize("  1x - 5x", _bucket(results, 1, 5))
    _summarize("  5x - 20x", _bucket(results, 5, 20))
    _summarize("  ratio >= 20x (illiquid)", _bucket(results, 20, float("inf")))

    print("\nSynthetic prices and a guessed impact model — mechanism demo, not a verdict.")
    print("See the TODO at the top of this file and documentation/ab-tests.md.")


if __name__ == "__main__":
    main()
