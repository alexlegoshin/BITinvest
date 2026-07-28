"""Runtime settings for the executor side, loaded from config.toml.

Everything that decides *how* the slave account tracks the master lives here:
rebalance mode, leverage normalisation, short handling, margin headroom.
Tokens do not — those stay in secrets/, see config.py.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from bitinvest.config import REPO_ROOT

MODES = ("mirror", "accumulate")
LEVERAGE_POLICIES = ("cap", "normalize")
CASH_POLICIES = ("underweight_first", "proportional", "never")


@dataclass(frozen=True)
class AccumulateSettings:
    deploy_free_cash: str = "underweight_first"
    cash_buffer_pct: float = 0.0
    trim_threshold_pct: float = 25.0


@dataclass(frozen=True)
class Settings:
    mode: str = "mirror"
    target_leverage: float = 1.0
    leverage_policy: str = "cap"
    allow_short: bool = True
    margin_safety: float = 0.9
    min_order_value: float = 1000.0
    max_step_age_sec: float = 900.0
    dry_run: bool = False
    accumulate: AccumulateSettings = field(default_factory=AccumulateSettings)

    def __post_init__(self) -> None:
        _one_of("mode", self.mode, MODES)
        _one_of("leverage_policy", self.leverage_policy, LEVERAGE_POLICIES)
        _one_of("accumulate.deploy_free_cash", self.accumulate.deploy_free_cash, CASH_POLICIES)
        _positive("target_leverage", self.target_leverage)
        _positive("margin_safety", self.margin_safety)
        _positive("max_step_age_sec", self.max_step_age_sec)
        if self.min_order_value < 0:
            raise ValueError("min_order_value must not be negative")
        if not 0 <= self.accumulate.cash_buffer_pct < 100:
            raise ValueError("accumulate.cash_buffer_pct must be in [0, 100)")
        if self.accumulate.trim_threshold_pct < 0:
            raise ValueError("accumulate.trim_threshold_pct must not be negative")


def _one_of(name: str, value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}, got {value!r}")


def _positive(name: str, value: float) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")


def config_path() -> Path:
    return Path(os.environ.get("BITINVEST_CONFIG", REPO_ROOT / "config.toml"))


def load_settings(path: Path | None = None) -> Settings:
    """Read config.toml. A missing file means "all defaults" — the defaults in
    Settings and config.toml are kept identical on purpose."""
    path = path or config_path()
    if not path.exists():
        return Settings()
    with path.open("rb") as f:
        raw = tomllib.load(f)

    accumulate_raw = raw.pop("accumulate", {})
    unknown = set(raw) - {f for f in Settings.__dataclass_fields__ if f != "accumulate"}
    if unknown:
        raise ValueError(f"Unknown keys in {path}: {', '.join(sorted(unknown))}")
    unknown_acc = set(accumulate_raw) - set(AccumulateSettings.__dataclass_fields__)
    if unknown_acc:
        raise ValueError(f"Unknown [accumulate] keys in {path}: {', '.join(sorted(unknown_acc))}")

    return Settings(**raw, accumulate=AccumulateSettings(**accumulate_raw))
