"""Общие модели. Время здесь всегда учебное: миллисекунды от начала прогона, а не часы компьютера."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from .money import D


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    PENDING = "pending"        # отправлена, в рынок ещё не поступила
    NEW = "new"                # стоит в стакане, сделки нет
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"


FINAL_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.REJECTED})


@dataclass(frozen=True)
class Instrument:
    ticker: str
    name: str
    kind: str = "share"        # облигации с НКД и купонами в этой версии не моделируются
    lot: int = 1
    tick: Decimal = Decimal("0.01")


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    qty: int


@dataclass(frozen=True)
class MarketSnapshot:
    ticker: str
    ts: int
    last: Decimal
    best_bid: Decimal | None
    best_ask: Decimal | None
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    halted: bool

    def demand_at_or_above(self, price: Decimal | None) -> int:
        return sum(l.qty for l in self.bids if price is None or l.price >= price)


@dataclass(frozen=True)
class MarketEvent:
    """Только то, что уже произошло. Будущего расписания сценария здесь нет."""
    seq: int
    ts: int
    kind: str                  # tape, trade, book, halt, resume
    last: Decimal
    best_bid: Decimal | None
    best_ask: Decimal | None


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str       # ключ идемпотентности
    account: str
    ticker: str
    side: Side
    qty: int
    limit_price: Decimal
    ttl_ms: int


@dataclass(frozen=True)
class Execution:
    exec_id: str
    client_order_id: str
    account: str
    ticker: str
    qty: int
    price: Decimal
    gross: Decimal
    fee: Decimal
    ts: int


@dataclass
class OrderView:
    client_order_id: str
    account: str
    ticker: str
    side: Side
    qty: int
    limit_price: Decimal
    status: OrderStatus
    filled_qty: int = 0
    gross: Decimal = Decimal(0)
    fee: Decimal = Decimal(0)
    sent_at: int = 0
    arrival_at: int | None = None
    closed_at: int | None = None
    reason: str = ""

    @property
    def open_qty(self) -> int:
        return 0 if self.status in FINAL_STATUSES else self.qty - self.filled_qty

    @property
    def avg_price(self) -> Decimal | None:
        return self.gross / self.filled_qty if self.filled_qty else None


@dataclass
class Portfolio:
    account: str
    cash: Decimal
    positions: dict[str, int]
    reserved: dict[str, int]

    def available(self, ticker: str) -> int:
        return self.positions.get(ticker, 0) - self.reserved.get(ticker, 0)


class Reference(StrEnum):
    BUY_PRICE = "buy_price"
    PEAK = "peak_since_enable"


class ConditionKind(StrEnum):
    DROP_PCT = "drop_pct"       # снижение на X% от точки отсчёта
    DROP_ABS = "drop_abs"       # снижение на X ₽ от точки отсчёта
    PRICE_BELOW = "price_below" # цена дошла до уровня


class QtyMode(StrEnum):
    ALL = "all"
    SHARES = "shares"
    PERCENT = "percent"


@dataclass(frozen=True)
class RuleSpec:
    """Согласованные условия. Неизменяемы: любое изменение — новая версия с новым подтверждением."""
    ticker: str
    condition: ConditionKind
    value: Decimal                     # проценты или рубли, по `condition`
    reference: Reference | None        # только для снижения (в процентах или рублях)
    qty_mode: QtyMode
    qty_value: Decimal | None          # штуки или проценты позиции
    floor: Decimal | None              # None только если клиент явно отказался от ограничения
    order_ttl_sec: int

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, Decimal):
                d[k] = str(v)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RuleSpec":
        return cls(ticker=d["ticker"], condition=ConditionKind(d["condition"]), value=D(d["value"]),
                   reference=Reference(d["reference"]) if d.get("reference") else None,
                   qty_mode=QtyMode(d["qty_mode"]), qty_value=D(d["qty_value"]) if d.get("qty_value") is not None else None,
                   floor=D(d["floor"]) if d.get("floor") is not None else None, order_ttl_sec=int(d["order_ttl_sec"]))


@dataclass
class Rule:
    id: int
    version: int
    spec: RuleSpec
    status: str = "active"      # active, paused, deleted
    meta: dict[str, Any] = field(default_factory=dict)
