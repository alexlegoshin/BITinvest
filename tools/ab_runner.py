"""Continuous A/B runner: crunches tools/ab_cash_policy.py,
tools/ab_leverage_policy.py and tools/ab_liquidation_policy.py forever,
keeping running (bounded-size) aggregates in data/ab_results.json instead of
one-shot stdout printouts.

Runs several independent "slots" on their own timers (see SLOTS at the
bottom):

  * cash_policy_synthetic          — no network, large-N over time.
  * leverage_policy_synthetic      — no network; "cap" vs "normalize" against
    a master whose own gross exposure drifts over time (see the docstring of
    tools/ab_leverage_policy.py for why that's the only regime where the two
    policies differ).
  * liquidation_synthetic_step*    — no network; three step_pct values
    (10/25/40) run in parallel, closing the "sweep liquidation_step_pct"
    TODO in documentation/ab-tests.md.
  * liquidation_real (x3, phased)  — needs secrets/sandbox_token.txt. Each
    cycle fetches ONE real (ticker, interval, window) from the T-Invest API
    and replays a batch of synthetic position sizes against that one real
    price series (one API call buys many data points). The three trackers
    are phased 5 minutes apart on a 15-minute period each, so the *combined*
    call rate is one every ~5 minutes — inside the 1-15 min budget the
    account owner asked for, not per-tracker.

Usage:
    python tools/ab_runner.py            # runs forever
    python tools/ab_runner.py --once      # one cycle per slot, then exit
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ab_cash_policy  # noqa: E402
import ab_leverage_policy as ab_lev  # noqa: E402
import ab_liquidation_policy as ab_liq  # noqa: E402
from _stats import MetricBag  # noqa: E402

from bitinvest import config  # noqa: E402

# bitinvest.marketdata imports t_tech.invest, which the synthetic-only slots
# don't need at all — deferred so `python tools/ab_runner.py --once` works
# with zero extra deps when there's no sandbox_token.txt (see run()).

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ab_runner")

# plan_orders() logs one INFO line per order; this harness calls it hundreds of
# thousands of times per day (LIQ_SYN_N * 2 modes * 3 step slots every minute),
# which would flood journald on a 956Mi server. The executor service (a separate
# process) still gets those lines at their normal level.
logging.getLogger("bitinvest.strategy").setLevel(logging.WARNING)
logging.getLogger("bitinvest.margin").setLevel(logging.WARNING)

RESULTS_FILE = config.DATA_DIR / "ab_results.json"

STEP_PCTS = (10.0, 25.0, 40.0)
LIQ_SYN_N = 200           # scenarios per cycle, per step_pct slot
LIQ_REAL_N = 60           # synthetic position sizes replayed per real price series
BUCKETS = (("liquid_<1x", 0, 1), ("1x_5x", 1, 5), ("5x_20x", 5, 20), ("20x_plus", 20, float("inf")))

CASH_PERIOD_SEC = 60
LIQ_SYN_PERIOD_SEC = 60
LIQ_REAL_PERIOD_SEC = 15 * 60      # per tracker
LIQ_REAL_PHASE_SEC = 5 * 60        # offset between trackers -> combined rate = 1 call/5min
LIQ_REAL_TRACKERS = 3


def _bucket_of(ratio: float) -> str:
    for name, lo, hi in BUCKETS:
        if lo <= ratio < hi:
            return name
    return BUCKETS[-1][0]


def _atomic_write_json(path: Path, data: dict) -> None:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


class Runner:
    def __init__(self, sandbox_token: str | None):
        self._sandbox_token = sandbox_token
        self._instruments: dict[str, marketdata.Instrument] | None = None

        self.cash_bags: dict[str, MetricBag] = {p: MetricBag() for p in ab_cash_policy.POLICIES}
        self.cash_cycles = 0

        self.lev_bags: dict[str, MetricBag] = {p: MetricBag() for p in ab_lev.POLICIES}
        self.lev_cycles = 0

        self.liq_syn_bags: dict[str, dict[str, MetricBag]] = {
            f"step_{int(p)}": {name: MetricBag() for name, _, _ in BUCKETS} for p in STEP_PCTS
        }
        self.liq_syn_cycles: dict[str, int] = {k: 0 for k in self.liq_syn_bags}

        self.real_overall = MetricBag()
        self.real_by_ticker: dict[str, MetricBag] = {}
        self.real_by_interval: dict[str, MetricBag] = {}
        self.real_cycles = 0
        self.real_last_sample: dict | None = None

        self._rng = {
            "cash": random.Random(),
            "leverage": random.Random(),
            "liq_syn": random.Random(),
            "liq_real": random.Random(),
        }

    def _seed(self, key: str) -> int:
        return self._rng[key].randrange(2**31)

    # -- cash_policy_synthetic ------------------------------------------

    def cycle_cash_policy(self) -> None:
        seed = self._seed("cash")
        accounts = ab_cash_policy.run_all(seed)
        series = ab_cash_policy.price_series(random.Random(seed))
        final = series[-1]
        for policy, account in accounts.items():
            equity = account.snapshot(final).equity
            idle_pct = account.cash / equity * 100 if equity else 0.0
            self.cash_bags[policy].add(
                equity=equity, idle_pct=idle_pct, trades=account.trades,
                turnover=account.turnover, fees=account.commission,
            )
        self.cash_cycles += 1

    # -- leverage_policy_synthetic ---------------------------------------

    def cycle_leverage_policy(self) -> None:
        seed = self._seed("leverage")
        accounts = ab_lev.run_all(seed)
        series = ab_cash_policy.price_series(random.Random(seed))
        final = series[-1]
        for policy, account in accounts.items():
            equity = account.snapshot(final).equity
            idle_pct = account.cash / equity * 100 if equity else 0.0
            self.lev_bags[policy].add(
                equity=equity, idle_pct=idle_pct, trades=account.trades,
                turnover=account.turnover, fees=account.commission,
            )
        self.lev_cycles += 1

    # -- liquidation_synthetic_step* -------------------------------------

    def cycle_liquidation_synthetic(self, step_pct: float) -> None:
        key = f"step_{int(step_pct)}"
        seed = self._seed("liq_syn")
        results = ab_liq.run_all(LIQ_SYN_N, seed=seed, step_pct=step_pct)
        for row in results:
            bucket = _bucket_of(row["scenario"].liquidity_ratio)
            self.liq_syn_bags[key][bucket].add(
                full_bps=row["full"]["cost_bps"], gradual_bps=row["gradual"]["cost_bps"],
                gradual_win=1.0 if row["gradual"]["cost_bps"] < row["full"]["cost_bps"] else 0.0,
                gradual_days=row["gradual"]["days"],
            )
        self.liq_syn_cycles[key] += 1

    # -- liquidation_real --------------------------------------------------

    def _client_instruments(self, client, marketdata) -> dict:
        if self._instruments is None:
            self._instruments = marketdata.resolve_instruments(client)
            logger.info("resolved %d instrument(s) for real-data slots", len(self._instruments))
        return self._instruments

    def cycle_liquidation_real(self) -> None:
        if not self._sandbox_token:
            logger.info("liquidation_real: no secrets/sandbox_token.txt, skipping")
            return

        from t_tech.invest import Client

        from bitinvest import marketdata

        rng = self._rng["liq_real"]
        sample = marketdata.sample_window(rng)
        with Client(self._sandbox_token) as client:
            instruments = self._client_instruments(client, marketdata)
            instrument = instruments[sample.ticker]
            prices = marketdata.fetch_close_series(
                client, instrument.uid, sample.interval_label, sample.frm, sample.to,
            )

        if len(prices) < marketdata.MIN_CANDLES:
            logger.warning(
                "liquidation_real: only %d candle(s) for %s/%s, skipping cycle",
                len(prices), sample.ticker, sample.interval_label,
            )
            return

        scenarios = ab_liq.make_scenarios(LIQ_REAL_N, rng)
        for scenario in scenarios:
            full = ab_liq.run_scenario(scenario, prices, "full")
            gradual = ab_liq.run_scenario(scenario, prices, "gradual")
            values = dict(
                full_bps=full["cost_bps"], gradual_bps=gradual["cost_bps"],
                gradual_win=1.0 if gradual["cost_bps"] < full["cost_bps"] else 0.0,
            )
            self.real_overall.add(**values)
            self.real_by_ticker.setdefault(sample.ticker, MetricBag()).add(**values)
            self.real_by_interval.setdefault(sample.interval_label, MetricBag()).add(**values)

        self.real_cycles += 1
        self.real_last_sample = {
            "ticker": sample.ticker, "interval": sample.interval_label,
            "from": sample.frm.isoformat(), "to": sample.to.isoformat(), "candles": len(prices),
        }

    # -- output -------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "cash_policy_synthetic": {
                "cycles": self.cash_cycles, "days": ab_cash_policy.DAYS,
                "policies": {p: bag.to_dict() for p, bag in self.cash_bags.items()},
            },
            "leverage_policy_synthetic": {
                "cycles": self.lev_cycles, "days": ab_lev.DAYS,
                "policies": {p: bag.to_dict() for p, bag in self.lev_bags.items()},
            },
            "liquidation_synthetic": {
                key: {"cycles": self.liq_syn_cycles[key],
                      "buckets": {name: bag.to_dict() for name, bag in bags.items()}}
                for key, bags in self.liq_syn_bags.items()
            },
            "liquidation_real": {
                "cycles": self.real_cycles, "last_sample": self.real_last_sample,
                "overall": self.real_overall.to_dict(),
                "by_ticker": {t: b.to_dict() for t, b in self.real_by_ticker.items()},
                "by_interval": {i: b.to_dict() for i, b in self.real_by_interval.items()},
            },
        }

    def write(self) -> None:
        _atomic_write_json(RESULTS_FILE, self.to_dict())


@dataclass
class Slot:
    name: str
    period_sec: float
    handler: Callable[[], None]
    next_due: float = field(default_factory=time.monotonic)


def make_slots(runner: Runner) -> list[Slot]:
    slots = [
        Slot("cash_policy_synthetic", CASH_PERIOD_SEC, runner.cycle_cash_policy),
        Slot("leverage_policy_synthetic", CASH_PERIOD_SEC, runner.cycle_leverage_policy),
    ]
    for step_pct in STEP_PCTS:
        slots.append(Slot(
            f"liquidation_synthetic_step{int(step_pct)}", LIQ_SYN_PERIOD_SEC,
            lambda p=step_pct: runner.cycle_liquidation_synthetic(p),
        ))
    now = time.monotonic()
    for i in range(LIQ_REAL_TRACKERS):
        slots.append(Slot(
            f"liquidation_real_{i}", LIQ_REAL_PERIOD_SEC, runner.cycle_liquidation_real,
            next_due=now + i * LIQ_REAL_PHASE_SEC,
        ))
    return slots


def run(once: bool = False) -> None:
    token = None
    try:
        token = config.load_sandbox_token()
    except FileNotFoundError:
        logger.info("secrets/sandbox_token.txt not found — real-data slots will be skipped")

    runner = Runner(sandbox_token=token)
    slots = make_slots(runner)

    while True:
        now = time.monotonic()
        due = [s for s in slots if s.next_due <= now] if not once else slots
        if not due:
            time.sleep(max(0.5, min(s.next_due for s in slots) - now))
            continue

        for slot in due:
            t0 = time.monotonic()
            try:
                slot.handler()
            except Exception:
                logger.exception("slot=%s failed", slot.name)
            slot.next_due = time.monotonic() + slot.period_sec
            logger.info("slot=%s done in %.2fs", slot.name, time.monotonic() - t0)

        runner.write()
        if once:
            return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one cycle per slot, then exit")
    args = parser.parse_args()
    run(once=args.once)


if __name__ == "__main__":
    main()
