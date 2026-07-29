"""Welford running mean/std — bounded-memory aggregation for tools/ab_runner.py.

The point: a daemon that runs for weeks must not grow its output file by
keeping raw samples. Each MetricBag holds only a few floats per named metric,
regardless of how many times .add() is called.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RunningStat:
    n: int = 0
    _mean: float = 0.0
    _m2: float = 0.0

    def add(self, x: float) -> None:
        self.n += 1
        delta = x - self._mean
        self._mean += delta / self.n
        self._m2 += delta * (x - self._mean)

    @property
    def mean(self) -> float:
        return self._mean

    @property
    def std(self) -> float:
        return (self._m2 / (self.n - 1)) ** 0.5 if self.n > 1 else 0.0


@dataclass
class MetricBag:
    """Several named running stats that are always .add()-ed together, e.g.
    one sample = one (equity, idle_pct, trades, turnover, fees) tuple."""

    _stats: dict[str, RunningStat] = field(default_factory=dict)

    def add(self, **values: float) -> None:
        for name, value in values.items():
            self._stats.setdefault(name, RunningStat()).add(value)

    def to_dict(self) -> dict:
        if not self._stats:
            return {"n": 0}
        n = next(iter(self._stats.values())).n
        out: dict = {"n": n}
        for name, stat in self._stats.items():
            out[f"{name}_mean"] = round(stat.mean, 4)
            out[f"{name}_std"] = round(stat.std, 4)
        return out
