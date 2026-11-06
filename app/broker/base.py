"""Единый интерфейс брокера. Торговый бот знает только его: искусственный рынок и песочница взаимозаменяемы.

Методы синхронные: у искусственного брокера нет сети, а сетевой адаптер запускается в своём потоке.
"""
from __future__ import annotations

from typing import Protocol

from ..models import Execution, MarketSnapshot, OrderRequest, OrderView, Portfolio


class Broker(Protocol):
    def get_positions(self, account: str) -> Portfolio: ...

    def get_market_data(self, ticker: str) -> MarketSnapshot: ...

    def post_order(self, request: OrderRequest) -> OrderView:
        """Идемпотентно по `client_order_id`: повтор возвращает ту же заявку и не создаёт новую."""
        ...

    def cancel_order(self, account: str, client_order_id: str) -> OrderView: ...

    def get_order_state(self, account: str, client_order_id: str) -> OrderView: ...

    def get_executions(self, account: str, client_order_id: str | None = None) -> list[Execution]: ...
