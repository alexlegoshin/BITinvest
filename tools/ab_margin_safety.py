"""A/B `margin_safety`: how much headroom to leave below the projected
starting margin requirement before letting a leveraged target through in full.

Untested since the setting was added. Every other A/B tool here compares a
strategy knob against a synthetic portfolio-value model; this is the first to
actually exercise `bitinvest.margin`'s arithmetic — through its pure functions
(`required_margin`, `margin_cap`), not live account margin attributes, since a
sandbox token has no funded margin account to read those from.

The question margin_safety answers: enter a leveraged position sized as close
to `target_leverage` as the margin rules allow — higher margin_safety means
closer to the edge, more capital actually deployed — then, if the master
isn't rebalanced for a while (a slow parser host, a quiet weekend), how much
cushion is left before a real adverse price move forces a margin call?

Method: for each `margin_safety` value, size a long-only, gross=TARGET_GROSS
synthetic position the same way `executor_service._margin_factor` does
(`bitinvest.margin.margin_cap`), then hold it unrebalanced across HOLD_DAYS of
a REAL historical single-ticker price path (see tools/ab_runner.py's shared
real-basket slot) and track the worst equity / minimal-margin ratio reached —
below 1.0 is a margin call. Real-data only: there is no synthetic random walk
to fall back to here, because the entire point is measuring against genuine
historical drawdowns, not an invented volatility assumption. Single-ticker
per scenario on purpose — it's the sharpest stress case (no cross-instrument
diversification cushion) and keeps a scenario independent of which subset of
tickers happened to resolve on a given real-basket cycle.

TARGET_GROSS and ASSUMED_RISK_RATE are chosen so that, at margin=1.0 exactly,
demand == equity: required_margin = TARGET_GROSS * equity * ASSUMED_RISK_RATE
= 4.0 * equity * 0.25 = equity. That makes `factor == safety` exactly for
every grid point below 1.0 — a deliberately transparent way to demonstrate
the capital-utilization side of the tradeoff, while the shock-survival side
(worst_margin_ratio / margin_call) is what genuinely depends on the sampled
real price history. ASSUMED_RISK_RATE is a guessed illustrative constant
(MOEX blue-chip long risk rates commonly sit in this ballpark), not fetched
from the API — same caveat as IMPACT_K in tools/ab_liquidation_policy.py.

    python tools/ab_margin_safety.py   (needs secrets/sandbox_token.txt)
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bitinvest.margin import RiskRate, margin_cap, required_margin  # noqa: E402

SAFETY_GRID = (0.7, 0.8, 0.9, 0.95, 1.0)   # config.toml default: 0.9
TARGET_GROSS = 4.0        # aggressive on purpose, see module docstring
ASSUMED_RISK_RATE = 0.25  # illustrative guess, not fetched from the API
HOLD_DAYS = 20            # real trading days held unrebalanced before the next check
START_EQUITY = 1_000_000.0
WINDOWS_PER_TICKER = 40   # random (day0) draws per ticker, per real-basket fetch


def simulate(safety: float, prices: list[float], day0: int) -> dict:
    """One entry (at day0) held unrebalanced for HOLD_DAYS along one real
    price path."""
    price0 = prices[day0]
    value0 = TARGET_GROSS * START_EQUITY
    rate = RiskRate(long=ASSUMED_RISK_RATE, short=ASSUMED_RISK_RATE)

    cap = margin_cap({"uid": value0}, {"uid": rate}, liquid_portfolio=START_EQUITY, safety=safety)
    factor = min(1.0, cap)
    units = (value0 * factor) / price0
    cash0 = START_EQUITY - units * price0

    worst_ratio = float("inf")
    breached = False
    for d in range(1, HOLD_DAYS + 1):
        position_value_t = units * prices[day0 + d]
        equity_t = cash0 + position_value_t
        if equity_t <= 0:
            worst_ratio = 0.0
            breached = True
            break
        req_t = required_margin({"uid": position_value_t}, {"uid": rate})
        minimal_t = req_t / 2  # see bitinvest.margin module docstring
        ratio = equity_t / minimal_t if minimal_t > 0 else float("inf")
        worst_ratio = min(worst_ratio, ratio)
        if equity_t < minimal_t:
            breached = True

    return {
        "factor": factor,
        "capital_used_pct": factor * 100,
        "worst_margin_ratio": worst_ratio,
        "margin_call": 1.0 if breached else 0.0,
    }


def label(safety: float) -> str:
    return f"{safety:g}"


def run_all_real(prices_by_ticker: dict[str, list[float]], rng: random.Random) -> dict[str, list[dict]]:
    """One margin_safety grid's worth of simulations, drawn from a real
    multi-ticker basket (see tools/ab_runner.py). Same (ticker, day0) draw is
    replayed across every safety value — paired comparison, same pattern as
    the other A/B tools here. Returns {safety_label: [scenario_dict, ...]}."""
    out: dict[str, list[dict]] = {label(s): [] for s in SAFETY_GRID}
    for ticker in sorted(prices_by_ticker):
        prices = prices_by_ticker[ticker]
        if len(prices) <= HOLD_DAYS + 1:
            continue
        for _ in range(WINDOWS_PER_TICKER):
            day0 = rng.randrange(0, len(prices) - HOLD_DAYS - 1)
            for safety in SAFETY_GRID:
                out[label(safety)].append(simulate(safety, prices, day0))
    return out


def _summarize(rows: list[dict]) -> dict[str, float]:
    n = len(rows)
    if not n:
        return {"n": 0}
    return {
        "n": n,
        "capital_used_pct_mean": round(sum(r["capital_used_pct"] for r in rows) / n, 2),
        "worst_margin_ratio_mean": round(sum(r["worst_margin_ratio"] for r in rows) / n, 4),
        "margin_call_rate": round(sum(r["margin_call"] for r in rows) / n, 4),
    }


def main() -> None:
    from t_tech.invest import Client

    from bitinvest import config, marketdata

    token = config.load_sandbox_token()
    rng = random.Random()
    frm, to = marketdata.sample_basket_window(rng)

    with Client(token) as client:
        instruments = marketdata.resolve_instruments(client)
        prices_by_ticker = {}
        for ticker, instrument in instruments.items():
            series = marketdata.fetch_close_series(client, instrument.uid, "day", frm, to)
            if len(series) > HOLD_DAYS + 1:
                prices_by_ticker[ticker] = series

    if not prices_by_ticker:
        print("Not enough real candles in the sampled window — try again.")
        return

    grids = run_all_real(prices_by_ticker, rng)

    print(f"window {frm.date()} .. {to.date()}, tickers={sorted(prices_by_ticker)}, "
          f"target_gross={TARGET_GROSS}, assumed_risk_rate={ASSUMED_RISK_RATE}, "
          f"hold_days={HOLD_DAYS}\n")
    header = f"{'safety':<10}{'n':>8}{'capital used':>14}{'worst ratio':>14}{'margin call rate':>18}"
    print(header)
    print("-" * len(header))
    for safety in SAFETY_GRID:
        s = _summarize(grids[label(safety)])
        if s["n"] == 0:
            print(f"{label(safety):<10}  (no scenarios)")
            continue
        print(f"{label(safety):<10}{s['n']:>8}{s['capital_used_pct_mean']:>13.1f}%"
              f"{s['worst_margin_ratio_mean']:>14.2f}{s['margin_call_rate']:>17.1%}")

    print("\nReal prices, guessed risk rate — see the module docstring and documentation/ab-tests.md.")


if __name__ == "__main__":
    main()
