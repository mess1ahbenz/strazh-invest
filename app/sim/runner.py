"""Симуляция: учебное время, очередь событий, сценарии и виртуальные боты.

Учебное время идёт только через `advance`: внутри него по порядку выполняются запланированные действия. Реальная задержка
приложения сюда не входит и замеряется отдельно. Сброс очищает очередь, поэтому старые задачи не сработают,
а `generation` останавливает шаг времени, внутри которого случился сброс.
"""
from __future__ import annotations

import heapq
import logging
import random
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from ..bot.engine import RuleRuntime, TradingBot
from ..broker.simulated import SimulatedBroker
from ..config import Settings
from ..db import Database
from ..market.exchange import Exchange, MarketParams
from ..models import ConditionKind, Execution, Instrument, OrderView, QtyMode, Reference, Rule, RuleSpec
from ..money import D, fmt_int, fmt_rub
from ..report import Report, compute
from .scenarios import SCENARIOS

log = logging.getLogger(__name__)

CLIENT = "client"
INSTRUMENT = Instrument("PRMT", "Пример-Тех")
TICK_MS = 1000
FLEET_DEFAULT_RULE = Rule(0, 1, RuleSpec(INSTRUMENT.ticker, ConditionKind.DROP_PCT, Decimal(10), Reference.BUY_PRICE, QtyMode.ALL,
                                         None, Decimal(850), 60))
HISTORY_LIMIT = 1500
MARKERS_LIMIT = 500

# Допустимые значения ручек рынка и прогона. Всё, что вне, — ValueError с понятным текстом.
LIMITS: dict[str, tuple[Decimal, Decimal, str]] = {
    "price": (Decimal("0.01"), Decimal(1_000_000), "Цена"),
    "seconds": (Decimal(1), Decimal(3600), "Длительность снижения, с"),
    "percent": (Decimal("0.01"), Decimal("99.99"), "Обвал, %"),
    "spread": (Decimal(0), Decimal(1000), "Спред, ₽"),
    "scale": (Decimal(0), Decimal(5), "Объём покупателей (1 = 100%)"),
    "size": (Decimal(0), Decimal(200), "Число виртуальных ботов"),
    "jitter_ms": (Decimal(0), Decimal(5000), "Случайная задержка ботов, мс"),
    "seed": (Decimal(0), Decimal(2 ** 31), "Seed"),
    "exec_delay_ms": (Decimal(0), Decimal(60_000), "Задержка исполнения, мс"),
}


def bounded(key: str, value: Any, integer: bool = False) -> Decimal:
    """Проверить число по LIMITS. Сообщение об ошибке пригодно для показа клиенту."""
    low, high, title = LIMITS[key]
    try:
        number = D(value)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{title}: нужно число") from None
    if not number.is_finite() or not low <= number <= high:
        raise ValueError(f"{title}: допустимо от {_plain_number(low)} до {_plain_number(high)}")
    if integer and number != number.to_integral_value():
        raise ValueError(f"{title}: нужно целое число")
    return number


def _plain_number(x: Decimal) -> str:
    whole, _, frac = f"{x.normalize():f}".partition(".")
    return f"{int(whole):,}".replace(",", " ") + (f",{frac}" if frac else "")


@dataclass
class FleetConfig:
    """Разброс задержки и seed виртуальных ботов. Число ботов задаётся при запуске сценария (`fleet_size`),
    `size` оставлен для совместимости и ни на что не влияет."""
    size: int = 0
    jitter_ms: int = 0
    seed: int = 42

    def __setattr__(self, key: str, value: Any) -> None:
        if key in ("size", "jitter_ms", "seed"):
            value = int(bounded(key, value, integer=True))
        super().__setattr__(key, value)


class Recorder:
    """Пишет заявки и сделки прогона в базу."""

    def __init__(self, sim: "Simulation") -> None:
        self.sim = sim

    def save_order(self, view: OrderView) -> None:
        self.sim.db.save_order(view, self.sim.run_id)

    def save_execution(self, execution: Execution) -> None:
        self.sim.db.save_execution(execution, self.sim.run_id)


class Simulation:
    def __init__(self, settings: Settings, db: Database, on_event: Callable[..., None] | None = None) -> None:
        self.settings, self.db = settings, db
        self.on_event = on_event or (lambda kind, **facts: None)   # значимые события для уведомлений
        self.generation = 0
        self.speed = 1.0
        self.exec_delay_ms = settings.exec_delay_ms
        self.fleet_config = FleetConfig()
        self.notify_failures = 0
        self.failure = ""                 # причина остановки прогона из-за внутренней ошибки
        self.reset()

    @property
    def exec_delay_ms(self) -> int:
        return self._exec_delay_ms

    @exec_delay_ms.setter
    def exec_delay_ms(self, value: Any) -> None:
        self._exec_delay_ms = int(bounded("exec_delay_ms", value, integer=True))

    # --- жизненный цикл прогона

    def reset(self, scenario: str | None = None) -> None:
        """Полностью новый прогон. Правила сохраняются, всё состояние рынка и заявки старого прогона — нет."""
        self.generation += 1
        self.run_id = uuid.uuid4().hex[:10]
        self.now = 0
        self._heap: list[tuple[int, int, Callable[[], None]]] = []
        self._order = 0
        self.ramp_id = 0
        self.status = "idle"
        self.failure = ""
        self.scenario_key = None
        self.scenario_end: int | None = None
        self.paused = False
        self.history: list[dict[str, Any]] = []
        self.markers: list[dict[str, Any]] = []
        self.exchange = Exchange(INSTRUMENT, self.settings.buy_price, MarketParams())
        self.broker = SimulatedBroker(self.exchange, self.schedule, lambda: self.now, self.settings.commission_bps,
                                      self._delay_for, Recorder(self))
        self.broker.open_account(CLIENT, INSTRUMENT.ticker, self.settings.start_qty, self.settings.start_cash)
        self.bots: dict[str, TradingBot] = {}
        self.jitter: dict[str, int] = {}
        self.bot_order: list[str] = []
        self.fleet_uses_default_rule = False
        client = self._add_bot(CLIENT)
        interrupted = self.db.start_run(self.run_id, scenario, {"exec_delay_ms": self.exec_delay_ms, "seed": self.fleet_config.seed})
        if self.db.flag("bots_paused"):       # пауза клиента переживает сброс и перезапуск; ставится до правил
            client.pause()
        for rule in self.db.rules():
            client.set_rule(rule)
        self._snapshot("start")
        self.schedule(TICK_MS, self._tick)
        self.log(CLIENT, "run", "Новый прогон: 100 акций Пример-Тех, цена 1 000 ₽" if self.settings.start_qty == 100
                 else "Новый прогон")
        if interrupted and self.generation == 1:
            self.log(CLIENT, "run", "Предыдущий прогон прерван перезапуском сервера. Его незакрытые заявки закрыты.")
            self._publish("run_interrupted", run_id=self.run_id)

    def _add_bot(self, account: str) -> TradingBot:
        bot = TradingBot(account, self.broker, INSTRUMENT.ticker, self.settings.buy_price, run_id=self.run_id,
                         claims=self.db, emit=lambda kind, runtime, **facts: self._bot_event(account, kind, runtime, facts))
        self.bots[account] = bot
        self.bot_order.append(account)
        self.broker.subscribe(account, bot.on_order_update)
        return bot

    def start_scenario(self, key: str, fleet_size: int | None = None) -> None:
        """`fleet_size=None` — число ботов по сценарию. Прошлый запуск на новый не влияет."""
        scenario = SCENARIOS[key]
        size = scenario.fleet if fleet_size is None else int(bounded("size", fleet_size, integer=True))
        self.reset(key)
        self.scenario_key = key
        self.status = "running"
        self.exchange.set_params(self.now, **scenario.market)
        self.exchange.demand_pool = Decimal(self.exchange.params.demand_target)
        self.exchange.set_params(self.now)
        self._spawn_fleet(size)
        for at, action, params in scenario.steps:
            self.schedule(at, lambda a=action, p=params: self._apply(a, p))
        self.scenario_end = scenario.duration_ms
        self.schedule(scenario.duration_ms, self._finish_scenario)
        self.log(CLIENT, "run", f"Сценарий запущен: {scenario.title}" + (f", виртуальных ботов {size}" if size else ""))
        self._pump()

    def _spawn_fleet(self, size: int) -> None:
        """Виртуальные боты получают копию действующего правила и свой счёт, но стакан один на всех."""
        config = self.fleet_config
        if not size:
            return
        rng = random.Random(config.seed)
        accounts = [f"bot-{i + 1:03d}" for i in range(size)]
        rules = [r for r in self.db.rules() if r.status == "active"]
        if not rules:                         # у клиента правила нет, но участникам рынка оно нужно
            rules = [FLEET_DEFAULT_RULE]
            self.fleet_uses_default_rule = True
        for account in accounts:
            self.broker.open_account(account, INSTRUMENT.ticker, self.settings.start_qty, self.settings.start_cash)
            bot = self._add_bot(account)
            for rule in rules:
                bot.set_rule(rule)
            self.jitter[account] = rng.randint(0, config.jitter_ms) if config.jitter_ms else 0
        self.jitter[CLIENT] = rng.randint(0, config.jitter_ms) if config.jitter_ms else 0
        rng.shuffle(self.bot_order)       # порядок при равной задержке фиксирован seed

    def _delay_for(self, account: str) -> int:
        return self.exec_delay_ms + self.jitter.get(account, 0)

    def _finish_scenario(self) -> None:
        self.status = "finished"
        self.db.finish_run(self.run_id)
        report = self.report()
        self.log(CLIENT, "run", "Сценарий завершён")
        self._publish("scenario_finished", run_id=self.run_id, title=SCENARIOS[self.scenario_key].title, report=report)

    def _publish(self, kind: str, **facts: Any) -> None:
        """Сбой уведомлений не должен дойти до торгового пути."""
        try:
            self.on_event(kind, **facts)
        except Exception as exc:  # noqa: BLE001
            self.notify_failures += 1
            log.warning("Уведомление %s не записано: %s", kind, type(exc).__name__)

    # --- время

    def schedule(self, delay_ms: int, action: Callable[[], None]) -> None:
        self._order += 1
        heapq.heappush(self._heap, (self.now + max(delay_ms, 0), self._order, action))

    def advance(self, ms: int) -> None:
        """Продвинуть учебное время. Всё происходит по порядку, за одну транзакцию базы.

        Внутренняя ошибка не откатывает базу: торговые записи до сбоя уже совпадают с памятью. Прогон
        останавливается со статусом `failed`, дальше время не идёт до «Сброса»: состояние посреди обхода стакана
        могло остаться незаконченным, и продолжать торговать на нём нечестно."""
        if self.status == "failed":
            return
        target = self.now + ms
        generation = self.generation
        with self.db.transaction():
            try:
                while self._heap and self._heap[0][0] <= target:
                    at, _, action = heapq.heappop(self._heap)
                    self.now = max(self.now, at)
                    self.exchange.now = self.now
                    action()
                    self._pump()
                    if self.generation != generation:
                        return
            except Exception as exc:  # noqa: BLE001
                self._fail(exc)
                return
            self.now = target
            self.exchange.now = target

    def _fail(self, exc: Exception) -> None:
        log.exception("Сбой шага симуляции")
        self.status = "failed"
        self.failure = f"{type(exc).__name__}: {exc}"[:300]
        self._heap.clear()
        for bot in self.bots.values():        # в памяти новых заявок нет, даже если кто-то вызовет бота напрямую
            bot.paused = True
        self.marker("error", "Прогон остановлен из-за ошибки")
        try:
            self.db.finish_run(self.run_id, "failed")
            self.log(CLIENT, "error", "Прогон остановлен из-за внутренней ошибки. Нажмите «Сброс».", reason=self.failure)
        except Exception:  # noqa: BLE001 — сама запись могла быть причиной сбоя
            log.warning("Запись о сбое прогона не сохранена")
        self._publish("run_failed", run_id=self.run_id, reason=self.failure)

    def _tick(self) -> None:
        self.exchange.tick(self.now, Decimal(TICK_MS) / 1000)
        self._snapshot("tick")
        self.schedule(TICK_MS, self._tick)

    def _pump(self) -> None:
        """Доставить рыночные события ботам. Бот реагирует сразу, а его заявка придёт в рынок с задержкой."""
        events = self.exchange.events
        while events:
            event = events.popleft()
            for account in list(self.bot_order):
                self.bots[account].on_market_event(event)
            if event.kind in ("tape", "trade", "resume"):
                self._snapshot(event.kind)

    def _snapshot(self, kind: str) -> None:
        ex = self.exchange
        point = {"t": self.now, "last": str(ex.last), "bid": str(ex.best_bid) if ex.best_bid else None,
                 "ask": str(ex.best_ask) if ex.best_ask else None}
        if self.history and self.history[-1]["t"] == self.now:
            self.history[-1] = point
        else:
            self.history.append(point)
            del self.history[:-HISTORY_LIMIT]

    # --- управление рынком: панель и сценарии идут через один вход

    def command(self, action: str, **params: Any) -> None:
        """Ручка панели. Неверные параметры — ValueError до любых изменений."""
        params = self.validate(action, params)
        if self.status == "failed":
            raise ValueError("Прогон остановлен из-за ошибки. Нажмите «Сброс».")
        with self.db.transaction():
            try:
                self._apply(action, params, manual=True)
                self._pump()
            except Exception as exc:  # noqa: BLE001
                self._fail(exc)

    @staticmethod
    def validate(action: str, params: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if action in ("price", "gradual"):
            out["price"] = bounded("price", params.get("price")).quantize(INSTRUMENT.tick)
            if action == "gradual":
                out["seconds"] = int(bounded("seconds", params.get("seconds", 20), integer=True))
        elif action == "crash":
            out["percent"] = bounded("percent", params.get("percent", 50))
        elif action == "demand":
            out["scale"] = bounded("scale", params.get("scale"))
        elif action == "spread":
            out["spread"] = bounded("spread", params.get("spread")).quantize(INSTRUMENT.tick)
        elif action not in ("halt", "resume"):
            raise ValueError(f"Неизвестное действие рынка: {action}")
        return out

    def _apply(self, action: str, params: dict[str, Any], manual: bool = False) -> None:
        if manual and action in ("price", "crash", "gradual"):
            self.ramp_id += 1                 # ручная команда отменяет незаконченное постепенное снижение
        if action == "price":
            self.exchange.set_price(D(params["price"]), self.now)
        elif action == "crash":
            price = self.exchange.to_tick(self.exchange.center * (100 - D(params.get("percent", 50))) / 100)
            self.exchange.set_price(price, self.now)
            self.marker("crash", f"Обвал до {fmt_rub(price)}")
        elif action == "gradual":
            self._gradual(D(params["price"]), int(params.get("seconds", 20)))
        elif action == "demand":
            self.exchange.set_params(self.now, demand_scale=D(params["scale"]))
            self.marker("market", f"Покупатели: {int(D(params['scale']) * 100)}%")
        elif action == "spread":
            self.exchange.set_params(self.now, spread=D(params["spread"]))
            self.marker("market", f"Спред {fmt_rub(D(params['spread']))}")
        elif action == "halt":
            self.exchange.halt(self.now)
            self.marker("halt", "Торги остановлены")
            self._publish("market_halt", run_id=self.run_id, ts=self.now, **self._client_limits())
        elif action == "resume":
            self.exchange.resume(self.now)
            self.marker("halt", "Торги возобновлены")
            self._publish("market_resume", run_id=self.run_id, ts=self.now, last=self.exchange.last, **self._client_limits())
        else:
            raise ValueError(f"Неизвестное действие рынка: {action}")
        if manual:
            self.log(CLIENT, "market", f"Панель рынка: {self._action_title(action, params)}")

    def _client_limits(self) -> dict[str, Any]:
        """Минимальные цены действующих правил клиента: для понятного сообщения о разрыве цены."""
        runtimes = self.bots[CLIENT].runtimes.values()
        floors = [r.rule.spec.floor for r in runtimes if r.rule.spec.floor is not None]
        return {"has_rules": bool(runtimes), "floor": max(floors) if floors else None}

    @staticmethod
    def _action_title(action: str, p: dict[str, Any]) -> str:
        return {"price": lambda: f"цена {fmt_rub(p['price'])}", "crash": lambda: f"обвал на {p.get('percent', 50)}%",
                "gradual": lambda: f"снижение до {fmt_rub(p['price'])} за {p.get('seconds', 20)} с",
                "demand": lambda: f"покупатели {int(D(p['scale']) * 100)}%", "spread": lambda: f"спред {fmt_rub(p['spread'])}",
                "halt": lambda: "торги остановлены", "resume": lambda: "торги возобновлены"}[action]()

    def _gradual(self, target: Decimal, seconds: int) -> None:
        """Одна повторяющаяся задача с состоянием, а не задача на каждую секунду."""
        self.ramp_id += 1
        ramp = self.ramp_id
        start = self.exchange.center
        steps = max(seconds, 1)

        def step(i: int = 1) -> None:
            if self.ramp_id != ramp:
                return
            self.exchange.set_price(self.exchange.to_tick(start + (target - start) * i / steps), self.now)
            if i < steps:
                self.schedule(1000, lambda: step(i + 1))

        self.schedule(1000, step)

    # --- боты и правила

    def apply_rule(self, rule: Rule) -> None:
        """Удалённое правило боту не передаётся: оно снимается."""
        if rule.status == "deleted":
            self.remove_rule(rule.id)
            return
        self.bots[CLIENT].set_rule(rule)
        self._pump()

    def remove_rule(self, rule_id: int) -> None:
        self.bots[CLIENT].remove_rule(rule_id)
        self._pump()

    def pause_bots(self) -> list[str]:
        cancelled: list[str] = []
        with self.db.transaction():
            self.db.set_flag("bots_paused", True)
            for bot in self.bots.values():
                cancelled += bot.pause()
        return cancelled

    def resume_bots(self) -> None:
        with self.db.transaction():
            self.db.set_flag("bots_paused", False)
            if self.status == "failed":       # остановленный прогон не торгует; пауза снимется в новом прогоне
                return
            for account in list(self.bot_order):
                self.bots[account].resume()

    def _bot_event(self, account: str, kind: str, runtime: RuleRuntime, facts: dict[str, Any]) -> None:
        rule = runtime.rule
        text = self._describe(kind, runtime, facts)
        if text:
            self.log(account, kind, text, rule_id=rule.id, version=rule.version, **{k: v for k, v in facts.items() if k != "execution"})
        if account == CLIENT:
            if kind in ("triggered", "fill", "order_sent", "blocked", "finished"):
                self._mark(kind, runtime, facts)
            self._publish(kind, run_id=self.run_id, account=account, runtime=runtime, facts=facts, ts=self.now)

    def _describe(self, kind: str, runtime: RuleRuntime, facts: dict[str, Any]) -> str:
        if kind == "triggered":
            return (f"Сработало условие: цена {fmt_rub(facts['last'])}, порог {fmt_rub(facts['threshold'])}. "
                    f"К продаже {fmt_int(facts['target'])} шт.")
        if kind == "assessed":
            best = f"лучшая цена покупателя {fmt_rub(facts['best_bid'])}" if facts["best_bid"] else "покупателей сейчас нет совсем"
            return f"Оценка рынка: {best}, спрос по разрешённой цене {fmt_int(facts['demand'])} шт."
        if kind == "order_sent":
            return f"Заявка отправлена: {fmt_int(facts['qty'])} шт. не дешевле {fmt_rub(facts['limit'])}"
        if kind == "blocked":
            best = f"лучшая цена покупателя {fmt_rub(facts['best_bid'])}" if facts["best_bid"] else "покупателей сейчас нет совсем"
            return f"Сейчас продажа невозможна: ограничение {fmt_rub(facts['floor'])}, {best}. Заявка ждёт в стакане."
        if kind == "fill":
            e = facts["execution"]
            return f"Сделка: {fmt_int(e.qty)} шт. по {fmt_rub(e.price)}, комиссия {fmt_rub(e.fee)}"
        if kind == "waiting_market":
            return "Торги остановлены: заявка не принята, бот ждёт возобновления"
        if kind == "error":
            return f"Ошибка: {facts['reason']}"
        if kind == "finished":
            return f"Продажа остановлена: {facts['reason']}. Продано {fmt_int(runtime.sold_qty)} из {fmt_int(runtime.target_qty)}"
        return ""

    def _mark(self, kind: str, runtime: RuleRuntime, facts: dict[str, Any]) -> None:
        if kind == "triggered":
            self.marker("trigger", f"Условие {fmt_rub(facts['last'])}", price=facts["last"])
        elif kind == "fill":
            e = facts["execution"]
            self.marker("fill", f"Продано {e.qty} по {fmt_rub(e.price)}", price=e.price)
        elif kind == "order_sent":
            self.marker("order", f"Заявка {facts['qty']} шт. от {fmt_rub(facts['limit'])}", price=facts["limit"])
        elif kind == "blocked":
            self.marker("blocked", "Нет покупателей по разрешённой цене", price=facts["floor"])

    def marker(self, kind: str, text: str, price: Decimal | None = None) -> None:
        self.markers.append({"t": self.now, "kind": kind, "text": text, "price": str(price) if price is not None else None})
        del self.markers[:-MARKERS_LIMIT]

    def log(self, account: str, kind: str, text: str, **data: Any) -> None:
        self.db.log(self.run_id, self.now, account, kind, text, data)

    # --- результаты

    def report(self, account: str = CLIENT) -> Report:
        return compute(self.broker, account, INSTRUMENT.ticker, self.settings.start_qty, self.settings.buy_price,
                       self.settings.start_cash)

    def fleet_report(self) -> dict[str, Any]:
        """Общий итог всех участников одного стакана. Спрос считается один раз.

        Строка клиента («Вы») всегда первая. Порядок поступления заявок дан номером и сдвигом от первой заявки:
        при равной задержке заявки поступают в одну миллисекунду, и очередь между ними задаёт seed."""
        rows = []
        for account in self.bots:
            report = self.report(account)
            rows.append({"account": account, "label": account_label(account), "is_you": account == CLIENT,
                         "sold": report.sold_qty, "remaining": report.remaining_qty,
                         "avg_price": str(report.avg_price) if report.avg_price else None, "revenue": str(report.revenue),
                         "difference": str(report.difference), "jitter_ms": self.jitter.get(account, 0)})
        rows.sort(key=lambda r: (not r["is_you"], -r["sold"], r["account"]))
        log_ = self.broker.arrival_log
        first = log_[0][0] if log_ else 0
        arrivals = [{"t": t, "account": a, "label": account_label(a), "position": i + 1, "offset_ms": t - first}
                    for i, (t, a, _) in enumerate(log_)]
        simultaneous = len({t for t, _, _ in log_}) == 1 and len(log_) > 1
        wanted = sum(r.target_qty for bot in self.bots.values() for r in bot.runtimes.values())
        return {"participants": len(rows), "total_sold": sum(r["sold"] for r in rows), "total_wanted": wanted,
                "demand_left": int(self.exchange.demand_pool), "demand_target": self.exchange.params.demand_target,
                "rows": rows, "arrivals": arrivals[:300], "simultaneous": simultaneous,
                "order_note": (f"Заявки поступили одновременно, очередь задана seed {self.fleet_config.seed}"
                               if simultaneous else "Заявки поступали в разное время: порядок по задержке, при равенстве — по seed")}


def account_label(account: str) -> str:
    if account == CLIENT:
        return "Вы"
    if account.startswith("bot-") and account[4:].isdigit():
        return f"Бот {int(account[4:])}"
    return account
