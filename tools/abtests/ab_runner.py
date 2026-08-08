"""Continuous A/B runner: crunches every tools/abtests/ab_*.py harness forever,
keeping running (bounded-size) aggregates in data/ab_results.json instead of
one-shot stdout printouts.

Runs several independent "slots" on their own timers (see SLOTS at the
bottom):

  * cash_policy_synthetic          — no network, large-N over time.
  * leverage_policy_synthetic      — no network; "cap" vs "normalize" against
    a master whose own gross exposure drifts over time (see the docstring of
    tools/abtests/ab_leverage_policy.py for why that's the only regime where the two
    policies differ).
  * mode_policy_synthetic          — no network; "mirror" vs "accumulate",
    the largest untested fork in strategy.py before this file existed.
  * accumulate_tuning_synthetic    — no network; three independent grid
    sweeps (trim_threshold_pct, cash_buffer_pct, min_order_value).
  * liquidation_synthetic_step*    — no network; three step_pct values
    (10/25/40) run in parallel, closing the "sweep liquidation_step_pct"
    TODO in tools/abtests/ab-tests-documentation.md.
  * liquidation_real (x3, phased)  — needs secrets/sandbox_token.txt. Each
    cycle fetches ONE real (ticker, interval, window) from the T-Invest API
    and replays a batch of synthetic position sizes against that one real
    price series (one API call buys many data points). The three trackers
    are phased 5 minutes apart on a 15-minute period each, so the *combined*
    call rate is one every ~5 minutes.
  * real_basket                    — needs secrets/sandbox_token.txt. Every
    30 minutes, fetches ONE real daily-candle window across the whole
    6-ticker pool (one burst of up to 6 calls, then quiet for 30 minutes —
    same order of magnitude combined rate as liquidation_real) and replays
    it through cash_policy, leverage_policy, mode_policy, accumulate_tuning
    and margin_safety all at once — one fetch feeds five comparisons, so
    leaning on real data here doesn't multiply the API budget.

Every *_synthetic bag except the liquidation ones resumes from whatever was
last written to data/ab_results.json on process start, instead of losing
accumulated statistics on every restart (service redeploys, crashes). The
liquidation bags deliberately do NOT resume: an impact-model fix (see
tools/abtests/ab_liquidation_policy.py) changed the underlying cost formula, and
averaging pre-fix and post-fix numbers into the same running stat would be
silently wrong. See tools/abtests/ab-tests-documentation.md for that reset's date and the
archived pre-fix snapshot.

Usage:
    python tools/abtests/ab_runner.py            # runs forever
    python tools/abtests/ab_runner.py --once      # one cycle per slot, then exit
"""

from __future__ import annotations

import argparse
import json
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
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import ab_accumulate_tuning as ab_tune  # noqa: E402
import ab_cash_policy  # noqa: E402
import ab_leverage_policy as ab_lev  # noqa: E402
import ab_liquidation_policy as ab_liq  # noqa: E402
import ab_margin_safety as ab_margin  # noqa: E402
import ab_mode_policy as ab_mode  # noqa: E402
from _stats import MetricBag  # noqa: E402

from bitinvest import config  # noqa: E402

# bitinvest.marketdata imports t_tech.invest, which the synthetic-only slots
# don't need at all — deferred so `python tools/abtests/ab_runner.py --once` works
# with zero extra deps when there's no sandbox_token.txt (see run()).

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ab_runner")

# plan_orders() logs one INFO line per order; this harness calls it hundreds of
# thousands of times per day, which would flood journald on a 956Mi server.
# The executor service (a separate process) still gets those lines at their
# normal level.
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

REAL_BASKET_PERIOD_SEC = 30 * 60   # one burst of up to 6 calls per period
REAL_BASKET_PHASE_SEC = 150        # stagger away from the liquidation_real trackers
REAL_BASKET_MIN_DAYS = 60          # fewer aligned days than this and the fetch is too thin to replay


def _bucket_of(ratio: float) -> str:
    for name, lo, hi in BUCKETS:
        if lo <= ratio < hi:
            return name
    return BUCKETS[-1][0]


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def _load_previous(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("could not read previous %s — starting every bag fresh", path)
        return {}


def _dig(node: dict, *path: str) -> dict:
    """Descend nested dict keys, returning {} the moment anything is missing
    or not a dict — the same missing-safe shape MetricBag.from_dict()
    tolerates (n=0 -> empty bag). Lets every resume call assume the previous
    JSON simply might not have this section yet."""
    for key in path:
        if not isinstance(node, dict):
            return {}
        node = node.get(key, {})
    return node if isinstance(node, dict) else {}


class Runner:
    def __init__(self, sandbox_token: str | None, resume: dict | None = None):
        self._sandbox_token = sandbox_token
        self._instruments: dict[str, "marketdata.Instrument"] | None = None
        resume = resume or {}

        # -- resumable synthetic bags (mechanism unchanged across restarts) --

        self.cash_bags: dict[str, MetricBag] = {
            p: MetricBag.from_dict(_dig(resume, "cash_policy_synthetic", "policies", p))
            for p in ab_cash_policy.POLICIES
        }
        self.cash_cycles = _dig(resume, "cash_policy_synthetic").get("cycles", 0)

        self.lev_bags: dict[str, MetricBag] = {
            p: MetricBag.from_dict(_dig(resume, "leverage_policy_synthetic", "policies", p))
            for p in ab_lev.POLICIES
        }
        self.lev_cycles = _dig(resume, "leverage_policy_synthetic").get("cycles", 0)

        self.mode_bags: dict[str, MetricBag] = {
            m: MetricBag.from_dict(_dig(resume, "mode_policy_synthetic", "modes", m))
            for m in ab_mode.MODES
        }
        self.mode_cycles = _dig(resume, "mode_policy_synthetic").get("cycles", 0)

        tuning_saved = _dig(resume, "accumulate_tuning_synthetic", "axes")
        self.tuning_bags: dict[str, dict[str, MetricBag]] = {
            axis: {label: MetricBag.from_dict(data) for label, data in _dig(tuning_saved, axis).items()}
            for axis in ab_tune.AXES
        }
        self.tuning_cycles = _dig(resume, "accumulate_tuning_synthetic").get("cycles", 0)

        # -- liquidation bags: resumable like everything else in general, but
        # see the *contents* of data/ab_results.json at deploy time — an
        # impact-model fix changed the underlying cost formula (see
        # tools/abtests/ab_liquidation_policy.py), so the liquidation_synthetic/
        # liquidation_real keys were deliberately stripped from the live file
        # on the server before this code shipped (archived first, see
        # tools/abtests/ab-tests-documentation.md). Resuming from a post-fix file is exactly
        # as safe as any other section; it's only the one-time pre/post-fix
        # boundary that must never be averaged together.

        liq_syn_saved = _dig(resume, "liquidation_synthetic")
        self.liq_syn_bags: dict[str, dict[str, MetricBag]] = {
            f"step_{int(p)}": {
                name: MetricBag.from_dict(_dig(liq_syn_saved, f"step_{int(p)}", "buckets", name))
                for name, _, _ in BUCKETS
            }
            for p in STEP_PCTS
        }
        self.liq_syn_cycles: dict[str, int] = {
            key: _dig(liq_syn_saved, key).get("cycles", 0) for key in self.liq_syn_bags
        }

        liq_real_saved = _dig(resume, "liquidation_real")
        self.real_overall = MetricBag.from_dict(_dig(liq_real_saved, "overall"))
        self.real_by_ticker: dict[str, MetricBag] = {
            t: MetricBag.from_dict(data) for t, data in _dig(liq_real_saved, "by_ticker").items()
        }
        self.real_by_interval: dict[str, MetricBag] = {
            i: MetricBag.from_dict(data) for i, data in _dig(liq_real_saved, "by_interval").items()
        }
        self.real_cycles = liq_real_saved.get("cycles", 0)
        self.real_last_sample: dict | None = liq_real_saved.get("last_sample")

        # -- real-basket-fed bags (also resumable) --

        self.cash_real_bags: dict[str, MetricBag] = {
            p: MetricBag.from_dict(_dig(resume, "real_basket", "cash_policy_real", "policies", p))
            for p in ab_cash_policy.POLICIES
        }
        self.lev_real_bags: dict[str, MetricBag] = {
            p: MetricBag.from_dict(_dig(resume, "real_basket", "leverage_policy_real", "policies", p))
            for p in ab_lev.POLICIES
        }
        self.mode_real_bags: dict[str, MetricBag] = {
            m: MetricBag.from_dict(_dig(resume, "real_basket", "mode_policy_real", "modes", m))
            for m in ab_mode.MODES
        }
        tuning_real_saved = _dig(resume, "real_basket", "accumulate_tuning_real", "axes")
        self.tuning_real_bags: dict[str, dict[str, MetricBag]] = {
            axis: {label: MetricBag.from_dict(data) for label, data in _dig(tuning_real_saved, axis).items()}
            for axis in ab_tune.AXES
        }
        margin_saved = _dig(resume, "real_basket", "margin_safety_real", "safety_levels")
        self.margin_real_bags: dict[str, MetricBag] = {
            label: MetricBag.from_dict(data) for label, data in margin_saved.items()
        }
        self.real_basket_cycles = _dig(resume, "real_basket").get("cycles", 0)
        self.real_basket_last_sample = _dig(resume, "real_basket").get("last_sample")

        self._rng = {
            "cash": random.Random(),
            "leverage": random.Random(),
            "mode": random.Random(),
            "tuning": random.Random(),
            "liq_syn": random.Random(),
            "liq_real": random.Random(),
            "real_basket": random.Random(),
            "margin": random.Random(),
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

    # -- mode_policy_synthetic --------------------------------------------

    def cycle_mode_policy(self) -> None:
        seed = self._seed("mode")
        accounts = ab_mode.run_all(seed)
        series = ab_cash_policy.price_series(random.Random(seed))
        final = series[-1]
        for mode, account in accounts.items():
            equity = account.snapshot(final).equity
            idle_pct = account.cash / equity * 100 if equity else 0.0
            self.mode_bags[mode].add(
                equity=equity, idle_pct=idle_pct, trades=account.trades,
                turnover=account.turnover, fees=account.commission,
            )
        self.mode_cycles += 1

    # -- accumulate_tuning_synthetic ---------------------------------------

    def cycle_accumulate_tuning(self) -> None:
        seed = self._seed("tuning")
        grids = ab_tune.run_all(seed)
        series = ab_cash_policy.price_series(random.Random(seed))
        final = series[-1]
        for axis, results in grids.items():
            for label, account in results.items():
                equity = account.snapshot(final).equity
                idle_pct = account.cash / equity * 100 if equity else 0.0
                self.tuning_bags[axis].setdefault(label, MetricBag()).add(
                    equity=equity, idle_pct=idle_pct, trades=account.trades,
                    turnover=account.turnover, fees=account.commission,
                )
        self.tuning_cycles += 1

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

    # -- shared real-data fetch helpers ------------------------------------

    def _client_instruments(self, client, marketdata) -> dict:
        if self._instruments is None:
            self._instruments = marketdata.resolve_instruments(client)
            logger.info("resolved %d instrument(s) for real-data slots", len(self._instruments))
        return self._instruments

    # -- liquidation_real --------------------------------------------------

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

    # -- real_basket: one fetch feeds cash/leverage/mode/tuning/margin -----

    def cycle_real_basket(self) -> None:
        if not self._sandbox_token:
            logger.info("real_basket: no secrets/sandbox_token.txt, skipping")
            return

        from t_tech.invest import Client

        from bitinvest import marketdata

        rng = self._rng["real_basket"]
        frm, to = marketdata.sample_basket_window(rng)
        with Client(self._sandbox_token) as client:
            instruments = self._client_instruments(client, marketdata)
            raw: dict[str, list[float]] = {}
            for ticker, instrument in instruments.items():
                series = marketdata.fetch_close_series(client, instrument.uid, "day", frm, to)
                if series:
                    raw[ticker] = series

        if not raw:
            logger.warning("real_basket: no candles returned for window %s..%s, skipping cycle", frm, to)
            return

        min_len = min(len(v) for v in raw.values())
        if min_len < REAL_BASKET_MIN_DAYS:
            logger.warning(
                "real_basket: only %d aligned day(s) across %d ticker(s), skipping cycle",
                min_len, len(raw),
            )
            return
        prices_by_ticker = {t: v[:min_len] for t, v in raw.items()}
        series = ab_cash_policy.real_series_from_basket(prices_by_ticker)
        final = series[-1]

        for policy, account in ab_cash_policy.run_all_real(prices_by_ticker).items():
            equity = account.snapshot(final).equity
            idle_pct = account.cash / equity * 100 if equity else 0.0
            self.cash_real_bags[policy].add(
                equity=equity, idle_pct=idle_pct, trades=account.trades,
                turnover=account.turnover, fees=account.commission,
            )

        for policy, account in ab_lev.run_all_real(prices_by_ticker, seed=self._seed("leverage")).items():
            equity = account.snapshot(final).equity
            idle_pct = account.cash / equity * 100 if equity else 0.0
            self.lev_real_bags[policy].add(
                equity=equity, idle_pct=idle_pct, trades=account.trades,
                turnover=account.turnover, fees=account.commission,
            )

        for mode, account in ab_mode.run_all_real(prices_by_ticker).items():
            equity = account.snapshot(final).equity
            idle_pct = account.cash / equity * 100 if equity else 0.0
            self.mode_real_bags[mode].add(
                equity=equity, idle_pct=idle_pct, trades=account.trades,
                turnover=account.turnover, fees=account.commission,
            )

        for axis, results in ab_tune.run_all_real(prices_by_ticker).items():
            for label, account in results.items():
                equity = account.snapshot(final).equity
                idle_pct = account.cash / equity * 100 if equity else 0.0
                self.tuning_real_bags[axis].setdefault(label, MetricBag()).add(
                    equity=equity, idle_pct=idle_pct, trades=account.trades,
                    turnover=account.turnover, fees=account.commission,
                )

        for safety_label, rows in ab_margin.run_all_real(prices_by_ticker, self._rng["margin"]).items():
            bag = self.margin_real_bags.setdefault(safety_label, MetricBag())
            for row in rows:
                bag.add(
                    capital_used_pct=row["capital_used_pct"],
                    worst_margin_ratio=row["worst_margin_ratio"],
                    margin_call=row["margin_call"],
                )

        self.real_basket_cycles += 1
        self.real_basket_last_sample = {
            "tickers": sorted(prices_by_ticker), "from": frm.isoformat(), "to": to.isoformat(),
            "days": min_len,
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
            "mode_policy_synthetic": {
                "cycles": self.mode_cycles, "days": ab_cash_policy.DAYS,
                "modes": {m: bag.to_dict() for m, bag in self.mode_bags.items()},
            },
            "accumulate_tuning_synthetic": {
                "cycles": self.tuning_cycles, "days": ab_cash_policy.DAYS,
                "axes": {
                    axis: {label: bag.to_dict() for label, bag in bags.items()}
                    for axis, bags in self.tuning_bags.items()
                },
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
            "real_basket": {
                "cycles": self.real_basket_cycles,
                "last_sample": self.real_basket_last_sample,
                "cash_policy_real": {
                    "policies": {p: bag.to_dict() for p, bag in self.cash_real_bags.items()},
                },
                "leverage_policy_real": {
                    "policies": {p: bag.to_dict() for p, bag in self.lev_real_bags.items()},
                },
                "mode_policy_real": {
                    "modes": {m: bag.to_dict() for m, bag in self.mode_real_bags.items()},
                },
                "accumulate_tuning_real": {
                    "axes": {
                        axis: {label: bag.to_dict() for label, bag in bags.items()}
                        for axis, bags in self.tuning_real_bags.items()
                    },
                },
                "margin_safety_real": {
                    "safety_levels": {label: bag.to_dict() for label, bag in self.margin_real_bags.items()},
                    "target_gross": ab_margin.TARGET_GROSS,
                    "assumed_risk_rate": ab_margin.ASSUMED_RISK_RATE,
                    "hold_days": ab_margin.HOLD_DAYS,
                },
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
    now = time.monotonic()
    slots = [
        Slot("cash_policy_synthetic", CASH_PERIOD_SEC, runner.cycle_cash_policy),
        Slot("leverage_policy_synthetic", CASH_PERIOD_SEC, runner.cycle_leverage_policy),
        Slot("mode_policy_synthetic", CASH_PERIOD_SEC, runner.cycle_mode_policy),
        Slot("accumulate_tuning_synthetic", CASH_PERIOD_SEC, runner.cycle_accumulate_tuning),
    ]
    for step_pct in STEP_PCTS:
        slots.append(Slot(
            f"liquidation_synthetic_step{int(step_pct)}", LIQ_SYN_PERIOD_SEC,
            lambda p=step_pct: runner.cycle_liquidation_synthetic(p),
        ))
    for i in range(LIQ_REAL_TRACKERS):
        slots.append(Slot(
            f"liquidation_real_{i}", LIQ_REAL_PERIOD_SEC, runner.cycle_liquidation_real,
            next_due=now + i * LIQ_REAL_PHASE_SEC,
        ))
    slots.append(Slot(
        "real_basket", REAL_BASKET_PERIOD_SEC, runner.cycle_real_basket,
        next_due=now + REAL_BASKET_PHASE_SEC,
    ))
    return slots


def run(once: bool = False) -> None:
    token = None
    try:
        token = config.load_sandbox_token()
    except FileNotFoundError:
        logger.info("secrets/sandbox_token.txt not found — real-data slots will be skipped")

    resume = _load_previous(RESULTS_FILE)
    if resume:
        logger.info("resuming synthetic bags from %s", RESULTS_FILE)
    runner = Runner(sandbox_token=token, resume=resume)
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
