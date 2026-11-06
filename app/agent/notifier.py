"""Уведомления. Движок только кладёт запись в базу, отправка идёт отдельно: сбой доставки не касается торговли."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..db import Database
from ..money import fmt_int, fmt_rub
from ..report import Report
from . import texts

TEST_CHAT = "test"
FILL_FLUSH_MS = 1500


class Notifier:
    def __init__(self, db: Database, telegram_enabled: bool) -> None:
        self.db, self.telegram_enabled = db, telegram_enabled
        self.sim: Any = None
        self._pending_fill: dict[str, Any] = {}

    def send(self, key: str, text: str, buttons: list | None = None, chat: str = TEST_CHAT, broadcast: bool = False) -> None:
        """Один ключ — одно сообщение, сколько бы раз событие ни повторилось.

        Ответ уходит в чат, откуда пришёл вопрос. Событие движка видно и во встроенном чате, и в настроенном Telegram.
        """
        if not self.db.claim(f"notify:{key}"):
            return
        self.db.add_chat(chat, "out", text, buttons, "notify" if broadcast else "reply")
        if self.telegram_enabled and (broadcast or chat != TEST_CHAT):
            self.db.enqueue(key, text, buttons)

    def event(self, key: str, text: str) -> None:
        self.send(key, text, broadcast=True)

    def on_event(self, kind: str, **e: Any) -> None:
        if kind == "scenario_finished":
            self.event(f"{e['run_id']}:scenario", texts.report_text(e["report"], f"Сценарий завершён: {e['title']}"))
            return
        if kind in ("market_halt", "market_resume", "run_failed", "run_interrupted"):
            self._run_event(kind, e)
            return
        runtime, facts, run = e["runtime"], e["facts"], e["run_id"]
        rule = runtime.rule
        prefix = f"{run}:r{rule.id}v{rule.version}:t{runtime.trigger_no}"
        if kind == "triggered":
            left = facts["target"] - runtime.sold_qty
            self.event(f"{prefix}:triggered",
                      f"Сработало условие по {texts.NAME}: цена {fmt_rub(facts['last'])}, порог {fmt_rub(facts['threshold'])}. "
                      f"Начинаю продажу до {texts.shares(left)}.")
        elif kind == "order_sent":
            self.event(f"{facts['cid']}:sent", f"Заявка отправлена: {texts.shares(facts['qty'])} не дешевле "
                                              f"{fmt_rub(facts['limit'])}. Заявка ещё не сделка.")
        elif kind == "blocked":
            best = (f"лучшая цена покупателя {fmt_rub(facts['best_bid'])}" if facts["best_bid"]
                    else "покупателей сейчас нет совсем")
            self.event(f"{prefix}:blocked:{runtime.attempt}",
                      f"Продажа сейчас ограничена: минимальная цена {fmt_rub(facts['floor'])}, {best}. "
                      "Дешевле заявка не исполнится, она ждёт покупателей в стакане.")
        elif kind == "fill":
            self._schedule_fill(prefix, runtime)
        elif kind == "finished":
            avg = runtime.gross / runtime.sold_qty if runtime.sold_qty else None
            self._pending_fill.pop(prefix, None)
            self.event(f"{prefix}:finished:{runtime.sold_qty}",
                      texts.sale_summary(runtime.sold_qty, runtime.target_qty, avg, runtime.remaining, facts["reason"]))
        elif kind == "error":
            self.event(f"{prefix}:error:{runtime.attempt}", f"Ошибка торгового бота: {facts['reason']}. Новых заявок по правилу нет.")
        elif kind == "waiting_market":
            self.event(f"{prefix}:halt:{runtime.attempt}", "Торги остановлены. Заявка не принята, бот ждёт возобновления.")

    def _run_event(self, kind: str, e: dict[str, Any]) -> None:
        run = e["run_id"]
        if kind == "run_failed":
            self.event(f"{run}:failed", "Учебный прогон остановлен из-за внутренней ошибки. Сделки до сбоя сохранены, "
                                       "новых заявок нет. Нажмите «Сброс», чтобы начать заново.")
        elif kind == "run_interrupted":
            self.event(f"{run}:interrupted", "Сервер перезапускался: прошлый учебный прогон прерван, его незакрытые заявки "
                                            "закрыты. Правила и пауза сохранены, начат новый прогон.")
        elif not e.get("has_rules"):
            return                            # без правил остановка торгов клиента не касается
        elif kind == "market_halt":
            self.event(f"{run}:halt:{e['ts']}", f"Торги по {texts.NAME} остановлены: заявки сейчас не исполняются. "
                                               "Бот ждёт возобновления.")
        else:
            floor, last = e.get("floor"), e["last"]
            gap = (f" Это ниже вашей минимальной цены {fmt_rub(floor)}: дешевле бот не продаст." if floor is not None
                   and last < floor else "")
            self.event(f"{run}:resume:{e['ts']}", f"Торги по {texts.NAME} возобновлены, цена открылась на {fmt_rub(last)}.{gap}")

    def _schedule_fill(self, prefix: str, runtime: Any) -> None:
        """Сделки идут пачкой: клиенту нужен итог пачки, а не сообщение на каждую."""
        if prefix in self._pending_fill or self.sim is None:
            return
        self._pending_fill[prefix] = runtime

        def flush() -> None:
            current = self._pending_fill.pop(prefix, None)
            if current is None or not current.remaining:
                return
            avg = current.gross / current.sold_qty
            self.event(f"{prefix}:fill:{current.sold_qty}",
                      f"Исполнено частично: продано {fmt_int(current.sold_qty)} из {texts.shares(current.target_qty)}, "
                      f"средняя цена {fmt_rub(avg)}. Осталось {texts.shares(current.remaining)}.")

        self.sim.schedule(FILL_FLUSH_MS, flush)
