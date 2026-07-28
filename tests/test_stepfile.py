from datetime import datetime, timedelta, timezone

import pytest

from bitinvest.portfolio import MasterView, TargetPosition
from bitinvest.stepfile import StaleStepFile, dump, load

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def view():
    return MasterView(
        positions=(
            TargetPosition("A", lot_size=10, price=123.4, weight=0.7, ticker="AAA", instrument_uid="uid-a"),
            TargetPosition("B", lot_size=1, price=50.0, weight=-0.2, ticker="BBB", instrument_uid="uid-b"),
        ),
        equity=12_345.0,
    )


def test_roundtrip_preserves_signed_weights(tmp_path):
    path = tmp_path / "step.json"
    dump(view(), path, now=NOW)

    restored = load(path)

    assert {p.figi: p.weight for p in restored.positions} == pytest.approx({"A": 0.7, "B": -0.2})
    assert restored.equity == pytest.approx(12_345.0)
    assert restored.gross_leverage == pytest.approx(0.9)
    assert restored.cash_weight == pytest.approx(0.5)


def test_stale_snapshot_is_refused(tmp_path):
    path = tmp_path / "step.json"
    dump(view(), path, now=NOW)

    with pytest.raises(StaleStepFile):
        load(path, max_age_sec=900, now=NOW + timedelta(seconds=901))

    load(path, max_age_sec=900, now=NOW + timedelta(seconds=899))


def test_wrong_schema_is_refused(tmp_path):
    path = tmp_path / "step.json"
    path.write_text('{"schema": 1, "positions": [], "master_equity": 0}')

    with pytest.raises(ValueError):
        load(path)
