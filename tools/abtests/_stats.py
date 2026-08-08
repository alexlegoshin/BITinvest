"""Welford running mean/std — bounded-memory aggregation for tools/abtests/ab_runner.py.

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

    @classmethod
    def from_saved(cls, n: int, mean: float, std: float) -> "RunningStat":
        """Rebuild exact Welford state from a previously written (n, mean,
        std) triple, so a process restart can resume accumulating instead of
        losing everything back to zero. M2 = std^2 * (n - 1) inverts
        RunningStat.std exactly — this is not an approximation."""
        m2 = std ** 2 * (n - 1) if n > 1 else 0.0
        return cls(n=n, _mean=mean, _m2=m2)


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

    @classmethod
    def from_dict(cls, data: dict) -> "MetricBag":
        """Inverse of to_dict() — resume a bag from what was last written to
        data/ab_results.json instead of starting back at n=0 on every service
        restart. Only valid when the metric being resumed is computed by the
        exact same code as before (see tools/abtests/ab_runner.py's RESUMABLE_SLOTS)."""
        bag = cls()
        n = data.get("n", 0)
        if not n:
            return bag
        names = {k[:-5] for k in data if k.endswith("_mean")}
        for name in names:
            mean = data.get(f"{name}_mean", 0.0)
            std = data.get(f"{name}_std", 0.0)
            bag._stats[name] = RunningStat.from_saved(n, mean, std)
        return bag
