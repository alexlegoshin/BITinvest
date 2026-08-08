"""RunningStat/MetricBag resume rebuilds exact Welford state from whatever was
last written to JSON — tools/abtests/ab_runner.py relies on this to survive service
restarts without losing accumulated A/B statistics (see its module
docstring). "Exact" is relative to the saved (mean, std) pair: to_dict()
itself rounds to 4 decimals before writing, so a resume-then-continue run
matches a never-restarted run up to that rounding, not to the last bit."""

import random

import pytest

from _stats import MetricBag, RunningStat


def test_resume_then_continue_matches_never_restarting():
    rng = random.Random(0)
    samples = [(rng.gauss(100, 15), rng.randint(1, 50)) for _ in range(300)]
    more = [(rng.gauss(100, 15), rng.randint(1, 50)) for _ in range(150)]

    never_restarted = MetricBag()
    for eq, tr in samples:
        never_restarted.add(equity=eq, trades=tr)
    for eq, tr in more:
        never_restarted.add(equity=eq, trades=tr)

    restarted = MetricBag()
    for eq, tr in samples:
        restarted.add(equity=eq, trades=tr)
    resumed = MetricBag.from_dict(restarted.to_dict())
    for eq, tr in more:
        resumed.add(equity=eq, trades=tr)

    got, want = resumed.to_dict(), never_restarted.to_dict()
    assert got.keys() == want.keys()
    for key in want:
        assert got[key] == pytest.approx(want[key], abs=1e-2)


def test_resume_of_empty_bag_is_empty():
    assert MetricBag.from_dict({"n": 0}).to_dict() == {"n": 0}
    assert MetricBag.from_dict({}).to_dict() == {"n": 0}


def test_single_sample_std_is_zero_and_resumes_as_zero():
    bag = MetricBag()
    bag.add(x=42.0)
    resumed = RunningStat.from_saved(**{
        "n": bag.to_dict()["n"], "mean": bag.to_dict()["x_mean"], "std": bag.to_dict()["x_std"],
    })
    assert resumed.n == 1
    assert resumed.mean == 42.0
    assert resumed.std == 0.0
