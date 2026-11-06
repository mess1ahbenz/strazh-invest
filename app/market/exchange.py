"""Искусственная биржа: один общий стакан с приоритетом цены и времени.

Цены:
- `last` — цена последней сделки: и фоновых участников («лента»), и настоящих сделок в этом стакане.
  По ней срабатывают правила.
- заявки покупателей — ограниченный объём по уровням. Только об них исполняется продажа. Цена сделки — цена
  покупателя: и когда продажа приходит в стакан, и когда новый фоновый покупатель забирает стоящую продажу.
  Поэтому продажа не проходит ни по цене, которой в стакане нет, ни дешевле, чем готов заплатить покупатель.

Все цены лежат на шаге инструмента (0,01 ₽) и не ниже одного шага.

Спрос покупателей задан общим запасом `demand_pool`. Сделки его расходуют, он восстанавливается медленно, на заданную
долю в секунду. Перестроение стакана при смене цены не воскрешает уже проданный объём.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Callable

from ..models import BookLevel, Instrument, MarketEvent, MarketSnapshot, Side
from ..money import D

MM = "market"             # фоновые участники


def _clean(value: Decimal) -> Decimal:
    """Одна запись для одной цены: 1000, а не 1000.00; 999.9, а не 999.90."""
    return value.quantize(Decimal(1)) if value == value.to_integral_value() else value.normalize()


@dataclass
class Resting:
    id: int
    owner: str
    side: Side
    price: Decimal
    qty: int
    seq: int
    tag: str = ""         # client_order_id заявки участника

    @property
    def is_mm(self) -> bool:
        return self.owner == MM


@dataclass(frozen=True)
class Fill:
    trade_id: int
    ts: int
    price: Decimal
    qty: int
    seller: str
    seller_tag: str
    buyer: str


@dataclass
class MarketParams:
    level_step: Decimal = Decimal(5)    # шаг уровней стакана, ₽
    levels: int = 6                     # уровней с каждой стороны
    level_qty: int = 20                 # покупателей на уровне при спросе 100%
    ask_qty: int = 50
    spread: Decimal = Decimal(2)        # разница между лучшими ценами, ₽
    demand_scale: Decimal = Decimal(1)  # 1 = 100%
    refill_per_sec: Decimal = Decimal("0.02")   # доля целевого спроса, возвращающаяся за секунду

    @property
    def demand_target(self) -> int:
        return int(self.level_qty * self.levels * self.demand_scale)


class Exchange:
    def __init__(self, instrument: Instrument, price: Decimal, params: MarketParams | None = None) -> None:
        self.instrument, self.params = instrument, params or MarketParams()
        self.tick_size = instrument.tick
        self.center = self.to_tick(price)
        self.last = self.center
        self.halted = False
        self.now = 0
        self.bids: list[Resting] = []        # по убыванию цены, затем по времени
        self.asks: list[Resting] = []        # по возрастанию цены, затем по времени
        self.demand_pool = Decimal(self.params.demand_target)
        self.events: deque[MarketEvent] = deque()
        self.on_fill: Callable[[Fill], None] = lambda fill: None
        self._order_id = self._trade_id = self._seq = self._event_seq = 0
        self._rebuild()

    def to_tick(self, price, rounding: str = ROUND_HALF_UP) -> Decimal:
        """Цена на шаге инструмента и не ниже одного шага."""
        value = max((D(price) / self.tick_size).to_integral_value(rounding) * self.tick_size, self.tick_size)
        return _clean(value)

    # --- наблюдаемое состояние

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    def snapshot(self, depth: int = 12) -> MarketSnapshot:
        return MarketSnapshot(self.instrument.ticker, self.now, self.last, self.best_bid, self.best_ask,
                              self._levels(self.bids, depth), self._levels(self.asks, depth), self.halted)

    @staticmethod
    def _levels(orders: list[Resting], depth: int) -> tuple[BookLevel, ...]:
        out: list[BookLevel] = []
        for order in orders:
            if out and out[-1].price == order.price:
                out[-1] = BookLevel(order.price, out[-1].qty + order.qty)
            elif len(out) == depth:
                break
            else:
                out.append(BookLevel(order.price, order.qty))
        return tuple(out)

    def _emit(self, kind: str) -> None:
        self._event_seq += 1
        self.events.append(MarketEvent(self._event_seq, self.now, kind, self.last, self.best_bid, self.best_ask))

    # --- управление рынком

    def set_price(self, price: Decimal, ts: int) -> None:
        """Рынок переместился. Лента печатает новую цену, стакан перестраивается вокруг неё."""
        self.now = ts
        self.center = self.to_tick(price)
        if self.halted:
            return
        self._rebuild()
        self.last = self.center
        self._emit("tape")

    def set_params(self, ts: int, **changes) -> None:
        self.now = ts
        for key, value in changes.items():
            setattr(self.params, key, value)
        self.demand_pool = min(self.demand_pool, Decimal(self.params.demand_target))
        if not self.halted:
            self._rebuild()
            self._emit("book")

    def halt(self, ts: int) -> None:
        self.now, self.halted = ts, True
        self._emit("halt")

    def resume(self, ts: int) -> None:
        self.now, self.halted = ts, False
        self._rebuild()
        self.last = self.center
        self._emit("resume")

    def tick(self, ts: int, seconds: Decimal) -> None:
        """Восстановление спроса. Покупатели возвращаются постепенно."""
        self.now = ts
        target = Decimal(self.params.demand_target)
        if self.demand_pool >= target or self.halted:
            return
        self.demand_pool = min(target, self.demand_pool + target * self.params.refill_per_sec * seconds)
        self._rebuild()
        self._emit("book")

    def _rebuild(self) -> None:
        self.bids = [o for o in self.bids if not o.is_mm]
        self.asks = [o for o in self.asks if not o.is_mm]
        p = self.params
        # лучший покупатель на шаге цены ниже центра, лучший продавец — ровно на спред выше: спред сохраняется точно
        top_bid = (self.center - p.spread / 2) / self.tick_size
        top_bid = top_bid.to_integral_value(ROUND_FLOOR) * self.tick_size
        for i in range(p.levels):
            price = _clean(max(top_bid + p.spread + p.level_step * i, self.tick_size))
            self._rest(Resting(self._next_order(), MM, Side.SELL, price, p.ask_qty, self._next_seq()))
        pool, levels = int(self.demand_pool), p.levels
        share, extra = (pool // levels, pool % levels) if levels else (0, 0)
        for i in range(levels):
            qty = share + (1 if i < extra else 0)
            price = _clean(top_bid - p.level_step * i)
            if qty > 0 and price >= self.tick_size:
                self._match_buy(price, qty)

    def _match_buy(self, price: Decimal, qty: int) -> None:
        """Фоновый покупатель — пассивная ликвидность: он появился в стакане, а стоящая продажа не дороже его цены
        забирает его по его цене. Продавец получает цену покупателя, а не свою границу."""
        while qty and self.asks and self.asks[0].price <= price and not self.asks[0].is_mm:
            resting = self.asks[0]
            take = min(qty, resting.qty)
            self._trade(resting, take, price=price)
            qty -= take
            self.demand_pool -= take
        if qty:
            self._rest(Resting(self._next_order(), MM, Side.BUY, price, qty, self._next_seq()))

    # --- заявки участников

    def submit_sell(self, owner: str, tag: str, price: Decimal, qty: int, ts: int) -> tuple[int, int]:
        """Вернуть (номер заявки, объём, оставшийся в стакане). Сделки приходят через `on_fill`."""
        self.now = ts
        price = self.to_tick(price, ROUND_CEILING)      # граница вверх: дешевле указанного не продаём
        left = qty
        while left and self.bids and self.bids[0].price >= price:
            resting = self.bids[0]
            take = min(left, resting.qty)
            self._trade(resting, take, seller=owner, seller_tag=tag)
            left -= take
            if resting.is_mm:
                self.demand_pool -= take
        order_id = self._next_order()
        if left:
            self._rest(Resting(order_id, owner, Side.SELL, price, left, self._next_seq(), tag))
        return order_id, left

    def cancel(self, order_id: int) -> int:
        for book in (self.asks, self.bids):
            for i, order in enumerate(book):
                if order.id == order_id:
                    book.pop(i)
                    return order.qty
        return 0

    def _trade(self, resting: Resting, qty: int, seller: str | None = None, seller_tag: str = "",
               price: Decimal | None = None) -> None:
        """Сделка. Цена — цена покупателя: стоящего (`resting`) или пришедшего (`price`)."""
        price = resting.price if price is None else price
        resting.qty -= qty
        if resting.side is Side.SELL:
            seller, seller_tag = resting.owner, resting.tag
        book = self.asks if resting.side is Side.SELL else self.bids
        if resting.qty == 0:
            book.remove(resting)
        self._trade_id += 1
        self.last = price
        self.on_fill(Fill(self._trade_id, self.now, price, qty, seller or MM, seller_tag, MM))
        self._emit("trade")

    def _rest(self, order: Resting) -> None:
        book = self.bids if order.side is Side.BUY else self.asks
        key = (lambda o: (-o.price, o.seq)) if order.side is Side.BUY else (lambda o: (o.price, o.seq))
        book.append(order)
        book.sort(key=key)

    def _next_order(self) -> int:
        self._order_id += 1
        return self._order_id

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq
