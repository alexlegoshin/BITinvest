"""Signed portfolio model.

Everything downstream is built on three ideas:

* a position's ``quantity`` is **signed** — negative means a short;
* ``cash`` is **signed** too — negative means money borrowed from the broker,
  which is the only form leverage takes at T-Bank (there is no leverage dial,
  you simply buy more than you have and the broker lends the difference);
* ``equity`` is cash + the signed value of every position, i.e. what is
  actually yours. Weights are always taken against equity, so a portfolio
  with borrowed money has weights summing above 1 and a negative cash weight.

``gross_leverage`` = sum of the absolute weights. That, not any per-position
number, is what gets normalised on the slave side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from t_tech.invest import Client

from bitinvest.broker import money, resolve_account_id

RUB_FIGI = "RUB000UTSTOM"


@dataclass(frozen=True)
class Position:
    """A security holding. Cash is not a Position — see Snapshot.cash."""

    figi: str
    lot_size: int
    price: float
    quantity: float  # signed, in raw units (not lots)
    ticker: str = ""
    instrument_uid: str = ""

    @property
    def value(self) -> float:
        return self.quantity * self.price

    @property
    def lots(self) -> float:
        return self.quantity / self.lot_size


@dataclass(frozen=True)
class Snapshot:
    """One account at one moment."""

    positions: tuple[Position, ...]
    cash: float

    @property
    def equity(self) -> float:
        return self.cash + sum(p.value for p in self.positions)

    @property
    def gross_exposure(self) -> float:
        return sum(abs(p.value) for p in self.positions)

    @property
    def gross_leverage(self) -> float:
        equity = self.equity
        if equity <= 0:
            raise ValueError("Non-positive equity — nothing sane to normalise against")
        return self.gross_exposure / equity

    def weights(self) -> dict[str, float]:
        equity = self.equity
        if equity <= 0:
            raise ValueError("Non-positive equity — cannot compute weights")
        return {p.figi: p.value / equity for p in self.positions}

    def by_figi(self) -> dict[str, Position]:
        return {p.figi: p for p in self.positions}


@dataclass(frozen=True)
class TargetPosition:
    """One line of the master snapshot published to the executor."""

    figi: str
    lot_size: int
    price: float
    weight: float  # signed, share of master equity
    ticker: str = ""
    instrument_uid: str = ""


@dataclass(frozen=True)
class MasterView:
    """Weight-blended view of one or more master accounts."""

    positions: tuple[TargetPosition, ...]
    equity: float  # combined, informational only — sizing uses slave equity

    @property
    def gross_leverage(self) -> float:
        return sum(abs(p.weight) for p in self.positions)

    @property
    def cash_weight(self) -> float:
        return 1.0 - sum(p.weight for p in self.positions)

    def by_figi(self) -> dict[str, TargetPosition]:
        return {p.figi: p for p in self.positions}


def _lot_size(position) -> int:
    """Derived from quantity/quantity_lots — a holding is always a whole
    number of lots, so the ratio is the lot size. Signs cancel, so shorts work."""
    lots = money(position.quantity_lots)
    if not lots:
        return 1
    return max(1, round(abs(money(position.quantity) / lots)))


def snapshot_from_portfolio(portfolio) -> Snapshot:
    """PortfolioResponse -> Snapshot. RUB becomes cash; everything else,
    including other currencies, stays a tradeable position."""
    positions: list[Position] = []
    cash = 0.0
    for p in portfolio.positions:
        quantity = money(p.quantity)
        price = money(p.current_price)
        if p.figi == RUB_FIGI:
            cash += quantity * price
            continue
        positions.append(
            Position(
                figi=p.figi,
                lot_size=_lot_size(p),
                price=price,
                quantity=quantity,
                ticker=getattr(p, "ticker", ""),
                instrument_uid=getattr(p, "instrument_uid", ""),
            )
        )
    return Snapshot(positions=tuple(positions), cash=cash)


def fetch_snapshot(token: str) -> Snapshot:
    with Client(token) as client:
        account_id = resolve_account_id(client)
        return snapshot_from_portfolio(client.operations.get_portfolio(account_id=account_id))


def blend(snapshots: Sequence[Snapshot], weights: Sequence[float]) -> MasterView:
    """Combine master accounts into one set of target weights.

    Each account is first normalised against *its own* equity, then accounts
    are mixed by their declared weights. So a declared weight is a share of
    influence, full stop — an account holding ten times more money does not
    get ten times the say on top of it. (v0.1 multiplied the two together,
    which made the declared weights nearly meaningless.)
    """
    if len(snapshots) != len(weights):
        raise ValueError("snapshots and weights must be the same length")
    if not snapshots:
        raise ValueError("Need at least one master snapshot")
    total_weight = sum(weights)
    if total_weight <= 0:
        raise ValueError("Master weights must sum to something positive")

    blended: dict[str, float] = {}
    meta: dict[str, Position] = {}
    for snapshot, weight in zip(snapshots, weights):
        share = weight / total_weight
        for figi, w in snapshot.weights().items():
            blended[figi] = blended.get(figi, 0.0) + share * w
        for position in snapshot.positions:
            meta.setdefault(position.figi, position)

    positions = tuple(
        TargetPosition(
            figi=figi,
            lot_size=meta[figi].lot_size,
            price=meta[figi].price,
            weight=weight,
            ticker=meta[figi].ticker,
            instrument_uid=meta[figi].instrument_uid,
        )
        for figi, weight in blended.items()
        # long and short of the same figi across masters can cancel out
        if abs(weight) > 1e-9
    )
    return MasterView(positions=positions, equity=sum(s.equity for s in snapshots))


def fetch_master_view(tokens: Iterable[str], weights: Sequence[float]) -> MasterView:
    snapshots = [fetch_snapshot(token) for token in tokens]
    return blend(snapshots, weights)
