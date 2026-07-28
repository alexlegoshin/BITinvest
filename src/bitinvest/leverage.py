"""Leverage normalisation — pure arithmetic, no API calls.

T-Bank has no leverage setting. You buy more than your cash allows, the broker
lends you the difference, and your leverage is whatever falls out:

    gross exposure / equity   (equity = your own money, "ликвидный портфель")

So copying a master means copying *proportions*, then rescaling the whole
thing by a single factor until the slave's gross leverage hits its own target.
One factor for the entire portfolio, never one per position: a master who is
5x in A and 3x in B gets reproduced as 0.625 / 0.375 at target 1.0, keeping the
5:3 ratio intact.

Cash never appears here. It is whatever is left after the scaled positions —
which is why a short can exist at leverage 1.0 with no borrowed money at all:
long 0.625 + short 0.375 is gross 1.0, and the short's proceeds sit in cash.
"""

from __future__ import annotations

from typing import Mapping


def gross_leverage(weights: Mapping[str, float]) -> float:
    return sum(abs(w) for w in weights.values())


def scale_factor(leverage: float, target: float, policy: str = "cap") -> float:
    """How much to shrink (or stretch) the master's weights.

    ``cap``       — only ever shrink. A master sitting 50% in cash keeps sitting
                    50% in cash; the target is read as a ceiling.
    ``normalize`` — always land exactly on the target, deploying cash the master
                    chose to hold.
    """
    if target <= 0:
        raise ValueError("target leverage must be positive")
    if leverage <= 0:
        # Master holds nothing — nothing to scale, everything goes to cash.
        return 0.0
    ratio = target / leverage
    if policy == "cap":
        return min(1.0, ratio)
    if policy == "normalize":
        return ratio
    raise ValueError(f"Unknown leverage policy: {policy!r}")


def apply_scale(weights: Mapping[str, float], factor: float) -> dict[str, float]:
    return {figi: w * factor for figi, w in weights.items()}


def drop_shorts(weights: Mapping[str, float]) -> dict[str, float]:
    """Remove short legs without scaling the longs up.

    Deliberately not renormalised: if the master is long 0.6 / short 0.4 and we
    cannot short, we hold 0.6 and leave 0.4 in cash. Stretching the longs to
    1.0 would be a different bet than the master's, taken on our own initiative.
    """
    return {figi: w for figi, w in weights.items() if w > 0}
