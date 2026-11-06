"""Искусственный брокер. Заявка поступает в рынок с задержкой и исполняется только об общий стакан."""
from __future__ import annotations

from decimal import Decimal
from typing import Callable

from ..market.exchange import Exchange, Fill
from ..models import (
    FINAL_STATUSES, Execution, MarketSnapshot, OrderRequest, OrderStatus, OrderView, Portfolio, Side,
)
from ..money import commission, rub

Listener = Callable[[OrderView, Execution | None], None]


class SimulatedBroker:
    def __init__(self, exchange: Exchange, schedule: Callable[[int, Callable[[], None]], None],
                 now: Callable[[], int], commission_bps: Decimal, delay_ms: Callable[[str], int],
                 recorder: object | None = None) -> None:
        self.exchange, self.schedule, self.now = exchange, schedule, now
        self.commission_bps, self.delay_ms, self.recorder = commission_bps, delay_ms, recorder
        self.accounts: dict[str, Portfolio] = {}
        self.orders: dict[str, OrderView] = {}
        self.executions: list[Execution] = []
        self.arrival_log: list[tuple[int, str, str]] = []     # (время, счёт, заявка) в порядке поступления
        self._book_ids: dict[str, int] = {}
        self._ttl: dict[str, int] = {}
        self._listeners: dict[str, Listener] = {}
        exchange.on_fill = self._on_fill

    def open_account(self, account: str, ticker: str, qty: int, cash: Decimal) -> None:
        self.accounts[account] = Portfolio(account, cash, {ticker: qty}, {ticker: 0})

    def subscribe(self, account: str, listener: Listener) -> None:
        self._listeners[account] = listener

    # --- интерфейс брокера

    def get_positions(self, account: str) -> Portfolio:
        return self.accounts[account]

    def get_market_data(self, ticker: str) -> MarketSnapshot:
        return self.exchange.snapshot()

    def post_order(self, request: OrderRequest) -> OrderView:
        known = self.orders.get(request.client_order_id)
        if known is not None:
            return known
        view = OrderView(request.client_order_id, request.account, request.ticker, request.side, request.qty,
                         request.limit_price, OrderStatus.PENDING, sent_at=self.now())
        self.orders[view.client_order_id] = view
        portfolio = self.accounts[request.account]
        if request.side is not Side.SELL:
            return self._reject(view, "Страж только продаёт")
        if request.qty <= 0:
            return self._reject(view, "Объём заявки должен быть положительным")
        if request.qty > portfolio.available(request.ticker):
            return self._reject(view, f"Доступно меньше: {portfolio.available(request.ticker)} шт.")
        portfolio.reserved[request.ticker] = portfolio.reserved.get(request.ticker, 0) + request.qty
        self._ttl[view.client_order_id] = request.ttl_ms
        self._save(view)
        self.schedule(self.delay_ms(request.account), lambda: self._arrive(view.client_order_id))
        return view

    def cancel_order(self, account: str, client_order_id: str) -> OrderView:
        view = self.orders[client_order_id]
        if view.status in FINAL_STATUSES:
            return view
        if client_order_id in self._book_ids:
            self.exchange.cancel(self._book_ids.pop(client_order_id))
        self._close(view, OrderStatus.CANCELLED, "Снята")
        return view

    def get_order_state(self, account: str, client_order_id: str) -> OrderView:
        return self.orders[client_order_id]

    def get_executions(self, account: str, client_order_id: str | None = None) -> list[Execution]:
        return [e for e in self.executions if e.account == account
                and (client_order_id is None or e.client_order_id == client_order_id)]

    # --- внутреннее

    def _arrive(self, cid: str) -> None:
        view = self.orders[cid]
        if view.status is not OrderStatus.PENDING:
            return                                # сняли, пока заявка шла к бирже
        view.arrival_at = self.now()
        self.arrival_log.append((view.arrival_at, view.account, cid))
        if self.exchange.halted:
            self._close(view, OrderStatus.REJECTED, "Торги остановлены")
            return
        view.status = OrderStatus.NEW
        self._notify(view, None)
        order_id, left = self.exchange.submit_sell(view.account, cid, view.limit_price, view.qty, self.now())
        if left:
            self._book_ids[cid] = order_id
            self.schedule(self._ttl[cid], lambda: self._expire(cid))
        self._save(view)

    def _expire(self, cid: str) -> None:
        view = self.orders[cid]
        if view.status in FINAL_STATUSES:
            return
        self.exchange.cancel(self._book_ids.pop(cid))
        self._close(view, OrderStatus.EXPIRED, "Истёк срок заявки")

    def _on_fill(self, fill: Fill) -> None:
        view = self.orders.get(fill.seller_tag)
        if view is None:
            return
        gross = rub(fill.price * fill.qty)
        fee = commission(gross, self.commission_bps)
        execution = Execution(f"{view.client_order_id}#{fill.trade_id}", view.client_order_id, view.account, view.ticker,
                              fill.qty, fill.price, gross, fee, fill.ts)
        portfolio = self.accounts[view.account]
        portfolio.positions[view.ticker] -= fill.qty
        portfolio.reserved[view.ticker] -= fill.qty
        portfolio.cash += gross - fee
        view.filled_qty += fill.qty
        view.gross += gross
        view.fee += fee
        self.executions.append(execution)
        if self.recorder:
            self.recorder.save_execution(execution)
        if view.filled_qty == view.qty:
            view.status, view.closed_at = OrderStatus.FILLED, self.now()
            self._book_ids.pop(view.client_order_id, None)
        else:
            view.status = OrderStatus.PARTIAL
        self._save(view)
        self._notify(view, execution)

    def _reject(self, view: OrderView, reason: str) -> OrderView:
        view.status, view.reason, view.closed_at = OrderStatus.REJECTED, reason, self.now()
        self._save(view)
        return view

    def _close(self, view: OrderView, status: OrderStatus, reason: str) -> None:
        portfolio = self.accounts[view.account]
        portfolio.reserved[view.ticker] -= view.qty - view.filled_qty
        view.status, view.reason, view.closed_at = status, reason, self.now()
        self._save(view)
        self._notify(view, None)

    def _save(self, view: OrderView) -> None:
        if self.recorder:
            self.recorder.save_order(view)

    def _notify(self, view: OrderView, execution: Execution | None) -> None:
        listener = self._listeners.get(view.account)
        if listener:
            listener(view, execution)
