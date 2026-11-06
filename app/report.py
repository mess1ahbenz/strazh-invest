"""Результат сценария. Все числа считаются здесь из состояния движка; модель их не считает и не меняет."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .broker.simulated import SimulatedBroker
from .money import rub


@dataclass(frozen=True)
class Report:
    start_qty: int
    buy_price: Decimal
    initial_value: Decimal
    sold_qty: int
    avg_price: Decimal | None
    revenue: Decimal
    fees: Decimal
    cash: Decimal
    remaining_qty: int
    mark_price: Decimal
    remaining_value: Decimal
    liquidation_value: Decimal
    with_bot: Decimal
    hold_value: Decimal
    difference: Decimal

    def to_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.__dict__.items()}


def liquidation_estimate(snapshot, qty: int) -> Decimal:
    """Сколько дал бы стакан прямо сейчас, если продать остаток рыночно. Только оценка."""
    left, value = qty, Decimal(0)
    for level in snapshot.bids:
        take = min(left, level.qty)
        value += take * level.price
        left -= take
        if not left:
            break
    return rub(value)


def compute(broker: SimulatedBroker, account: str, ticker: str, start_qty: int, buy_price: Decimal,
            start_cash: Decimal = Decimal(0)) -> Report:
    """Оба сценария оцениваются в один момент по одной цене: цене последней сделки."""
    portfolio = broker.get_positions(account)
    executions = broker.get_executions(account)
    snapshot = broker.get_market_data(ticker)
    sold = sum(e.qty for e in executions)
    revenue = sum((e.gross for e in executions), Decimal(0))
    fees = sum((e.fee for e in executions), Decimal(0))
    remaining = portfolio.positions.get(ticker, 0)
    mark = snapshot.last
    remaining_value = rub(mark * remaining)
    with_bot = rub(portfolio.cash + remaining_value)
    hold_value = rub(start_cash + mark * start_qty)
    return Report(start_qty, buy_price, rub(buy_price * start_qty), sold, rub(revenue / sold) if sold else None, rub(revenue),
                  rub(fees), rub(portfolio.cash), remaining, mark, remaining_value, liquidation_estimate(snapshot, remaining),
                  with_bot, hold_value, rub(with_bot - hold_value))
