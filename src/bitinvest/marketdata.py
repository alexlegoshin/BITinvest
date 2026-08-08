"""Real candles for the `liquidation_real` A/B slot (see tools/abtests/ab_runner.py).

Thin wrapper over `t_tech.invest`, same style as `broker.py`: no state, no
retries, no rate-limiting logic here — that belongs to the caller, which knows
the shared API budget across all real-data slots.

`find_instrument` is deprecated upstream but still functional and simplest for
a resolve-once-at-startup lookup; not worth chasing its replacement for a call
made a handful of times per process lifetime.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from t_tech.invest import CandleInterval

from bitinvest.broker import money

# Same six liquid MOEX tickers already used in tools/abtests/ab_cash_policy.py.
TICKERS = ("SBER", "GAZP", "LKOH", "MTSS", "PHOR", "ROSN")

# (interval, label, min window, max window, how far back "to" may start from now)
# Kept conservative — real per-interval historical depth limits on the API
# aren't verified from here (no network access while writing this); fetch_close_series
# just returns whatever candles come back, and the caller skips a cycle that
# comes back too thin rather than assuming a specific depth.
_INTERVALS: tuple[tuple[CandleInterval, str, timedelta, timedelta, timedelta], ...] = (
    (CandleInterval.CANDLE_INTERVAL_1_MIN, "1min", timedelta(hours=1), timedelta(hours=8), timedelta(days=2)),
    (CandleInterval.CANDLE_INTERVAL_5_MIN, "5min", timedelta(hours=4), timedelta(days=2), timedelta(days=7)),
    (CandleInterval.CANDLE_INTERVAL_HOUR, "hour", timedelta(days=1), timedelta(days=14), timedelta(days=90)),
    (CandleInterval.CANDLE_INTERVAL_DAY, "day", timedelta(days=30), timedelta(days=365), timedelta(days=1500)),
)

MIN_CANDLES = 20  # fewer than this and the sample isn't worth replaying scenarios on


@dataclass(frozen=True)
class Instrument:
    uid: str
    lot_size: int


@dataclass(frozen=True)
class Sample:
    ticker: str
    interval_label: str
    frm: datetime
    to: datetime


def resolve_instruments(client, tickers: tuple[str, ...] = TICKERS) -> dict[str, Instrument]:
    """Ticker -> (instrument_uid, lot_size), resolved once. Call at process
    startup and cache the result — this is a real API call per ticker."""
    out: dict[str, Instrument] = {}
    for ticker in tickers:
        resp = client.instruments.find_instrument(query=ticker)
        match = next(
            (i for i in resp.instruments if i.ticker == ticker and i.class_code == "TQBR"),
            None,
        )
        if match is None:
            raise RuntimeError(f"could not resolve instrument for ticker {ticker!r} on TQBR")
        out[ticker] = Instrument(uid=match.uid, lot_size=match.lot or 1)
    return out


BASKET_MIN_SPAN = timedelta(days=180)
BASKET_MAX_SPAN = timedelta(days=1100)      # ~3 years, matching the synthetic
                                             # harnesses' DAYS=750 trading days
BASKET_MAX_LOOKBACK = timedelta(days=1500)


def sample_basket_window(rng: random.Random) -> tuple[datetime, datetime]:
    """Real daily window (180 days - 3 years span), for the basket-level A/B
    tests (mode/cash/leverage_policy/accumulate_tuning/margin_safety) that
    each need many aligned real trading days across several tickers, not the
    single instrument at a mixed-interval window `sample_window` produces for
    liquidation_real. Daily-only: fetching several tickers' worth of intraday
    candles in one cycle would multiply the API-call burst for no benefit —
    these tests play out over months, not hours. Separate span constants from
    `_INTERVALS`'s "day" entry on purpose: that one is tuned for a single
    instrument's price path, this one wants runs long enough to be comparable
    to the synthetic harnesses' 750 trading days."""
    span = timedelta(seconds=rng.uniform(BASKET_MIN_SPAN.total_seconds(), BASKET_MAX_SPAN.total_seconds()))

    now = datetime.now(timezone.utc) - timedelta(days=1)  # daily candles need a closed session
    latest_start = now - span
    earliest_start = now - BASKET_MAX_LOOKBACK
    start = datetime.fromtimestamp(
        rng.uniform(earliest_start.timestamp(), latest_start.timestamp()), tz=timezone.utc
    )
    return start, start + span


def sample_window(rng: random.Random) -> Sample:
    """Random (ticker, interval, window) — the "look at Sber for 3 days, then
    Gazprom for 8 hours at another frequency" the user asked for."""
    ticker = rng.choice(TICKERS)
    interval, label, min_window, max_window, max_lookback = rng.choice(_INTERVALS)
    span = timedelta(seconds=rng.uniform(min_window.total_seconds(), max_window.total_seconds()))

    now = datetime.now(timezone.utc) - timedelta(minutes=5)  # candles need to be closed already
    latest_start = now - span
    earliest_start = now - max_lookback
    start = datetime.fromtimestamp(
        rng.uniform(earliest_start.timestamp(), latest_start.timestamp()), tz=timezone.utc
    )
    return Sample(ticker=ticker, interval_label=label, frm=start, to=start + span)


def _interval_for_label(label: str) -> CandleInterval:
    return next(interval for interval, lbl, *_ in _INTERVALS if lbl == label)


def fetch_close_series(client, instrument_uid: str, interval_label: str, frm: datetime, to: datetime) -> list[float]:
    resp = client.market_data.get_candles(
        instrument_id=instrument_uid, from_=frm, to=to, interval=_interval_for_label(interval_label)
    )
    return [money(c.close) for c in resp.candles if c.is_complete]
