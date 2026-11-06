"""Торговый бот. Детерминированный код без сети и без языковой модели.

Цепочка «новая цена → условие → заявка» выполняется синхронно внутри обработки рыночного события. Бот не читает
расписание сценария: он получает только уже случившиеся события и текущее состояние через интерфейс брокера.

Условие начала и исполнение разделены:
- срабатывание смотрит только на цену последней сделки;
- оценка исполнения смотрит на заявки покупателей и на продажу не влияет: исчезновение спроса не запускает её и не отменяет.

Алгоритм продажи — одна лимитная заявка по минимальной цене клиента. Она сразу забирает всех покупателей от лучшей цены
до границы, а остаток стоит до конца срока. Деления на части во времени нет: ожидание стоит дороже давления на цену,
и остаток всё равно виден рынку только как заявка по цене клиента.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import Callable, Protocol

from ..broker.base import Broker
from ..models import (
    FINAL_STATUSES, ConditionKind, Execution, MarketEvent, OrderRequest, OrderStatus, OrderView, QtyMode, Reference,
    Rule, Side,
)
from ..money import fmt_pct, fmt_rub

ACTIVE = ("armed", "selling", "waiting_market", "suspended")
STATE_TITLES = {"armed": "следит за ценой", "selling": "продаёт", "waiting_market": "ждёт возобновления торгов",
                "suspended": "приостановлено", "done": "продажа завершена",
                "expired": "заявка снята по сроку, правило больше не продаёт", "failed": "ошибка"}


class Claims(Protocol):
    def claim(self, key: str) -> bool:
        """Первый вызов с ключом — True. Защита от повторного торгового действия."""
        ...


class MemoryClaims:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    def claim(self, key: str) -> bool:
        if key in self.keys:
            return False
        self.keys.add(key)
        return True


@dataclass
class RuleRuntime:
    rule: Rule
    state: str = "armed"
    peak: Decimal | None = None
    trigger_no: int = 0
    attempt: int = 0
    target_qty: int = 0
    sold_qty: int = 0
    gross: Decimal = Decimal(0)
    fee: Decimal = Decimal(0)
    open_cid: str | None = None
    trigger_price: Decimal | None = None
    threshold: Decimal | None = None
    stop_reason: str = ""
    rule_paused: bool = False
    base_position: int | None = None   # позиция при первом срабатывании: от неё считается согласованный объём
    decision_us: int | None = None
    order_ids: list[str] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return max(self.target_qty - self.sold_qty, 0)


class TradingBot:
    def __init__(self, account: str, broker: Broker, ticker: str, cost_basis: Decimal, *, run_id: str = "run",
                 claims: Claims | None = None, emit: Callable[..., None] | None = None) -> None:
        self.account, self.broker, self.ticker, self.cost_basis = account, broker, ticker, cost_basis
        self.run_id = run_id
        self.claims = claims or MemoryClaims()
        self.emit = emit or (lambda kind, runtime, **facts: None)
        self.runtimes: dict[int, RuleRuntime] = {}
        self.paused = False
        self.last_seq = 0

    # --- управление правилами

    def set_rule(self, rule: Rule) -> None:
        """Новая версия применяется сразу. Стоящая заявка старой версии снимается: у неё другие ограничения.

        После смены версии правило снова следит за ценой и продаёт, только если условие новой версии выполнено
        на текущей цене. Объём считается от позиции при первом срабатывании, поэтому правка границы не увеличивает
        согласованный объём. На паузе бота или правила новых заявок нет."""
        if rule.status == "deleted":
            self.remove_rule(rule.id)
            return
        runtime = self.runtimes.get(rule.id)
        last = self.broker.get_market_data(self.ticker).last
        if runtime is None:
            runtime = self.runtimes[rule.id] = RuleRuntime(rule, peak=last, rule_paused=rule.status == "paused")
            if not self.paused and not runtime.rule_paused:
                self._check(runtime, last)     # условие уже выполнено: продажа начинается сразу после включения
            return
        changed = runtime.rule.version != rule.version
        runtime.rule = rule
        runtime.rule_paused = rule.status == "paused"
        if not changed:
            return
        if runtime.open_cid:
            self.broker.cancel_order(self.account, runtime.open_cid)
            runtime.open_cid = None
        if runtime.state != "armed":
            runtime.state, runtime.stop_reason, runtime.attempt = "armed", "", 0
        if not self.paused and not runtime.rule_paused:
            self._check(runtime, last, f"Применена версия {rule.version} правила")

    def remove_rule(self, rule_id: int) -> None:
        runtime = self.runtimes.pop(rule_id, None)
        if runtime and runtime.open_cid:
            self.broker.cancel_order(self.account, runtime.open_cid)

    def pause_rule(self, rule_id: int) -> None:
        runtime = self.runtimes.get(rule_id)
        if runtime is None:
            return
        runtime.rule_paused = True
        self._suspend(runtime)

    def resume_rule(self, rule_id: int) -> None:
        runtime = self.runtimes.get(rule_id)
        if runtime is None:
            return
        runtime.rule_paused = False
        if not self.paused:
            self._revive(runtime)

    def pause(self) -> list[str]:
        """Новых действий нет, неисполненные заявки сняты. Сделанные сделки остаются."""
        self.paused = True
        cancelled = []
        for runtime in self.runtimes.values():
            if runtime.open_cid:
                cancelled.append(runtime.open_cid)
            self._suspend(runtime)
        return cancelled

    def resume(self) -> None:
        self.paused = False
        for runtime in self.runtimes.values():
            if not runtime.rule_paused:
                self._revive(runtime)

    def _suspend(self, runtime: RuleRuntime) -> None:
        if runtime.open_cid:
            self.broker.cancel_order(self.account, runtime.open_cid)
        if runtime.state in ("selling", "waiting_market"):
            runtime.state = "suspended"

    def _revive(self, runtime: RuleRuntime) -> None:
        if runtime.state == "suspended":
            self._send(runtime, "Возобновление продажи")
        elif runtime.state == "armed":
            self._check(runtime, self.broker.get_market_data(self.ticker).last)

    # --- рыночные события

    def on_market_event(self, event: MarketEvent) -> None:
        if event.seq <= self.last_seq:        # повторная доставка не должна запускать ничего второй раз
            return
        self.last_seq = event.seq
        if event.kind not in ("tape", "trade", "resume"):
            return
        for runtime in list(self.runtimes.values()):
            if runtime.peak is None or event.last > runtime.peak:
                runtime.peak = event.last
            if self.paused or runtime.rule_paused:
                continue
            if runtime.state == "armed":
                self._check(runtime, event.last)
            elif runtime.state == "waiting_market" and event.kind == "resume":
                self._send(runtime, "Торги возобновились")

    def threshold(self, runtime: RuleRuntime) -> Decimal:
        return threshold_of(runtime.rule.spec, self.cost_basis, runtime.peak)

    def _check(self, runtime: RuleRuntime, last: Decimal, why: str = "Сработало условие") -> None:
        started = time.perf_counter_ns()
        threshold = self.threshold(runtime)
        if last > threshold:
            return
        runtime.state, runtime.trigger_price, runtime.threshold = "selling", last, threshold
        runtime.trigger_no += 1
        spec = runtime.rule.spec
        if runtime.base_position is None:
            position = self.broker.get_positions(self.account).positions.get(self.ticker, 0)
            runtime.base_position = position + runtime.sold_qty
        runtime.target_qty = self._target(spec, runtime.base_position)
        self.emit("triggered", runtime, last=last, threshold=threshold, target=runtime.target_qty)
        if runtime.remaining == 0:
            self._finish(runtime, "done", "Продан весь согласованный объём")
        else:
            self._send(runtime, why)
        runtime.decision_us = (time.perf_counter_ns() - started) // 1000

    @staticmethod
    def _target(spec, position: int) -> int:
        if spec.qty_mode is QtyMode.ALL:
            return position
        if spec.qty_mode is QtyMode.SHARES:
            return min(int(spec.qty_value), position)
        return int((Decimal(position) * spec.qty_value / 100).to_integral_value(ROUND_FLOOR))

    def _send(self, runtime: RuleRuntime, why: str) -> None:
        spec = runtime.rule.spec
        portfolio = self.broker.get_positions(self.account)
        qty = min(runtime.remaining, portfolio.available(self.ticker))
        if qty <= 0:
            self._finish(runtime, "done" if runtime.sold_qty else "failed", "Нет бумаг для продажи")
            return
        market = self.broker.get_market_data(self.ticker)
        runtime.state = "selling"
        if market.halted:
            runtime.state = "waiting_market"
            self.emit("waiting_market", runtime)
            return
        floor = spec.floor
        limit = floor if floor is not None else Decimal("0.01")
        demand = market.demand_at_or_above(floor)
        self.emit("assessed", runtime, best_bid=market.best_bid, demand=demand, qty=qty)
        runtime.attempt += 1
        rule = runtime.rule
        cid = f"{self.run_id}:{self.account}:r{rule.id}v{rule.version}:t{runtime.trigger_no}:a{runtime.attempt}"
        if not self.claims.claim(cid):
            return                            # это торговое действие уже выполнялось
        view = self.broker.post_order(OrderRequest(cid, self.account, self.ticker, Side.SELL, qty, limit,
                                                   spec.order_ttl_sec * 1000))
        runtime.order_ids.append(cid)
        if view.status is OrderStatus.REJECTED:
            self.emit("error", runtime, reason=view.reason)
            self._finish(runtime, "failed", view.reason)
            return
        runtime.open_cid = cid
        self.emit("order_sent", runtime, qty=qty, limit=limit, why=why, cid=cid)
        if floor is not None and (market.best_bid is None or market.best_bid < floor):
            self.emit("blocked", runtime, floor=floor, best_bid=market.best_bid, qty=qty)

    # --- заявки

    def on_order_update(self, view: OrderView, execution: Execution | None) -> None:
        runtime = next((r for r in self.runtimes.values() if view.client_order_id in r.order_ids), None)
        if runtime is None:
            return
        if execution is not None:
            runtime.sold_qty += execution.qty
            runtime.gross += execution.gross
            runtime.fee += execution.fee
            self.emit("fill", runtime, execution=execution)
        if view.status not in FINAL_STATUSES or runtime.open_cid != view.client_order_id:
            return
        runtime.open_cid = None
        if runtime.remaining == 0:
            self._finish(runtime, "done", "Продан весь согласованный объём")
        elif view.status is OrderStatus.EXPIRED:
            self._finish(runtime, "expired", "Нет покупателей по разрешённой цене")
        elif view.status is OrderStatus.REJECTED:
            if "Торги остановлены" in view.reason:
                runtime.state = "waiting_market"
                self.emit("waiting_market", runtime)
            else:
                self.emit("error", runtime, reason=view.reason)
                self._finish(runtime, "failed", view.reason)

    def _finish(self, runtime: RuleRuntime, state: str, reason: str) -> None:
        runtime.state, runtime.stop_reason = state, reason
        self.emit("finished", runtime, reason=reason)

    def describe(self, runtime: RuleRuntime) -> str:
        spec = runtime.rule.spec
        if spec.condition is ConditionKind.PRICE_BELOW:
            return f"цена дошла до {fmt_rub(spec.value)}"
        base = "цены покупки" if spec.reference is Reference.BUY_PRICE else "максимума после включения"
        amount = fmt_pct(spec.value) if spec.condition is ConditionKind.DROP_PCT else fmt_rub(spec.value)
        return f"снижение на {amount} от {base}"

    def state_title(self, runtime: RuleRuntime) -> str:
        """Состояние словами, согласованное с паузой бота, паузой правила и стаканом."""
        if self.paused and runtime.state in ACTIVE:
            return "бот на паузе: новых заявок нет"
        if runtime.rule_paused and runtime.state in ACTIVE:
            return "правило приостановлено"
        if runtime.state == "armed":
            return f"ждёт падения до {fmt_rub(self.threshold(runtime))}"
        if runtime.state == "selling":
            floor = runtime.rule.spec.floor
            best = self.broker.get_market_data(self.ticker).best_bid
            if floor is not None and (best is None or best < floor):
                return f"ждёт покупателей от {fmt_rub(floor)}"
        return STATE_TITLES.get(runtime.state, runtime.state)


def threshold_of(spec, cost_basis: Decimal, peak: Decimal | None) -> Decimal:
    """Порог условия: цена последней сделки не выше него — правило срабатывает."""
    if spec.condition is ConditionKind.PRICE_BELOW:
        return spec.value
    base = cost_basis if spec.reference is Reference.BUY_PRICE else (peak if peak is not None else cost_basis)
    if spec.condition is ConditionKind.DROP_ABS:
        return base - spec.value
    return base * (Decimal(100) - spec.value) / Decimal(100)
