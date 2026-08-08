"""Mechanical smoke tests for the liquidation A/B harness — this only checks
the tool runs correctly and stays deterministic. It deliberately does not run
the full campaign or draw any conclusion about which policy wins; that verdict
comes from the continuous server runs, see tools/abtests/ab-tests-documentation.md."""

import ab_liquidation_policy as ab


def test_deterministic_for_a_fixed_seed():
    a = ab.run_all(n=15, seed=1)
    b = ab.run_all(n=15, seed=1)
    assert [r["full"]["proceeds"] for r in a] == [r["full"]["proceeds"] for r in b]
    assert [r["gradual"]["proceeds"] for r in a] == [r["gradual"]["proceeds"] for r in b]


def test_full_mode_always_exits_in_a_single_day():
    for r in ab.run_all(n=15, seed=2):
        assert r["full"]["days"] == 1
        assert r["full"]["finished"]


def test_gradual_mode_takes_more_than_one_day_and_finishes():
    results = ab.run_all(n=15, seed=3)
    assert any(r["gradual"]["days"] > 1 for r in results)
    assert all(r["gradual"]["finished"] for r in results)


def test_cost_bps_is_relative_to_the_pre_liquidation_mark():
    for r in ab.run_all(n=5, seed=4):
        for mode in ("full", "gradual"):
            outcome = r[mode]
            expected = (outcome["fair_value"] - outcome["proceeds"]) / outcome["fair_value"] * 1e4
            assert outcome["cost_bps"] == expected
