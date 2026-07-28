"""The one thing the two hosts exchange: a snapshot of the master's weights.

JSON rather than v0.1's CSV because the model now needs signed weights, a
negative cash weight and, above all, a timestamp — without one the executor
happily replays the last snapshot forever after the parser host dies.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from bitinvest.portfolio import MasterView, TargetPosition

SCHEMA = 2

logger = logging.getLogger(__name__)


class StaleStepFile(RuntimeError):
    pass


def dump(view: MasterView, path: Path, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    payload = {
        "schema": SCHEMA,
        "generated_at": now.isoformat(),
        "master_equity": view.equity,
        "gross_leverage": view.gross_leverage,
        "cash_weight": view.cash_weight,
        "positions": [
            {
                "figi": p.figi,
                "ticker": p.ticker,
                "instrument_uid": p.instrument_uid,
                "lot_size": p.lot_size,
                "price": p.price,
                "weight": p.weight,
            }
            for p in sorted(view.positions, key=lambda p: p.figi)
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: the executor must never read a half-written snapshot.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    tmp.replace(path)


def load(path: Path, max_age_sec: float | None = None, now: datetime | None = None) -> MasterView:
    payload = json.loads(path.read_text())
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"{path}: unsupported schema {payload.get('schema')!r}, expected {SCHEMA}")

    if max_age_sec is not None:
        now = now or datetime.now(timezone.utc)
        generated = datetime.fromisoformat(payload["generated_at"])
        age = (now - generated).total_seconds()
        if age > max_age_sec:
            raise StaleStepFile(
                f"Master snapshot is {age:.0f}s old (limit {max_age_sec:.0f}s) — refusing to trade on it"
            )

    positions = tuple(
        TargetPosition(
            figi=p["figi"],
            lot_size=int(p["lot_size"]),
            price=float(p["price"]),
            weight=float(p["weight"]),
            ticker=p.get("ticker", ""),
            instrument_uid=p.get("instrument_uid", ""),
        )
        for p in payload["positions"]
    )
    return MasterView(positions=positions, equity=float(payload["master_equity"]))
