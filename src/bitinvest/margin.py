"""Margin arithmetic and safety rails.

T-Bank's margin regime, in the only terms that matter here:

    начальная маржа  = Σ(|position value| × risk rate)
    минимальная маржа = начальная / 2
    ликвидный портфель ≥ начальная  -> new positions may be opened
    ликвидный портфель < минимальная -> margin call, positions get force-closed

Risk rates differ for long and short legs and depend on the client's risk
level (КНУР/КСУР/КПУР), so they are read from the API rather than assumed.

Since the projected starting margin is linear in the scale factor, the largest
safe factor has a closed form and needs no search:

    k ≤ margin_safety × ликвидный портфель / Σ(|value at k=1| × rate)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Mapping

from t_tech.invest.schemas import RiskRatesRequest

from bitinvest.broker import money

logger = logging.getLogger(__name__)

# Used when the API gives us no rate for an instrument: demand full collateral.
DEFAULT_RISK_RATE = 1.0


@dataclass(frozen=True)
class MarginState:
    liquid_portfolio: float
    starting_margin: float
    minimal_margin: float

    @property
    def can_open_new(self) -> bool:
        return self.liquid_portfolio >= self.starting_margin

    @property
    def margin_call(self) -> bool:
        return self.liquid_portfolio < self.minimal_margin


@dataclass(frozen=True)
class RiskRate:
    long: float
    short: float

    def rate_for(self, value: float) -> float:
        return self.short if value < 0 else self.long


def _as_fraction(value) -> float:
    """Risk rates come as Quotation. A rate is a fraction (0.4 = 40% collateral);
    anything above 1.5 can only be a percentage, so scale it down.

    Defensive on purpose: this session has no live token to confirm the unit
    against, and misreading 40 as 4000% would silently forbid all trading.
    """
    rate = money(value)
    if rate > 1.5:
        rate /= 100
    return rate


def fetch_margin_state(client, account_id: str) -> MarginState | None:
    """None when the account has no margin regime (or the call is unavailable)."""
    try:
        attrs = client.users.get_margin_attributes(account_id=account_id)
    except Exception:  # noqa: BLE001 - non-margin accounts legitimately fail here
        logger.info("No margin attributes for account %s — treating as cash-only", account_id)
        return None
    return MarginState(
        liquid_portfolio=money(attrs.liquid_portfolio),
        starting_margin=money(attrs.starting_margin),
        minimal_margin=money(attrs.minimal_margin),
    )


def fetch_risk_rates(client, instrument_uids: Iterable[str]) -> dict[str, RiskRate]:
    uids = sorted({uid for uid in instrument_uids if uid})
    if not uids:
        return {}
    try:
        response = client.instruments.get_risk_rates(RiskRatesRequest(instrument_id=uids))
    except Exception:  # noqa: BLE001
        logger.warning("Could not fetch risk rates; falling back to full collateral", exc_info=True)
        return {}
    rates: dict[str, RiskRate] = {}
    for item in response.instrument_risk_rates:
        if getattr(item, "error", ""):
            continue
        rates[item.instrument_uid] = RiskRate(
            long=_as_fraction(item.long_risk_rate),
            short=_as_fraction(item.short_risk_rate),
        )
    return rates


def required_margin(values: Mapping[str, float], rates: Mapping[str, RiskRate]) -> float:
    """Projected starting margin for a set of positions keyed by instrument_uid."""
    total = 0.0
    for uid, value in values.items():
        rate = rates.get(uid)
        total += abs(value) * (rate.rate_for(value) if rate else DEFAULT_RISK_RATE)
    return total


def margin_cap(values_at_full: Mapping[str, float], rates: Mapping[str, RiskRate],
               liquid_portfolio: float, safety: float) -> float:
    """Largest scale factor keeping the projected starting margin within budget."""
    demand = required_margin(values_at_full, rates)
    if demand <= 0:
        return float("inf")
    return safety * liquid_portfolio / demand


def needs_margin_check(weights: Mapping[str, float], factor: float) -> bool:
    """Skip the API round-trip when the result cannot possibly bind: with only
    long legs and gross ≤ 1, the margin demand is at most equity (rates ≤ 1)."""
    if any(w < 0 for w in weights.values()):
        return True
    return sum(abs(w) for w in weights.values()) * factor > 1.0
