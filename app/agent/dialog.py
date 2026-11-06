"""Диалог: сообщение клиента → черновик → уточнения → подтверждение → версия правила.

Один обработчик для встроенного чата и Telegram. Правило попадает к торговому боту только после нажатия «Включить»,
и только в той версии, которая была показана: кнопка привязана к номеру черновика, а любое изменение черновика
(текстом или кнопкой) выдаёт новый номер. Удаление, пауза и возобновление, которые предложила модель, выполняются
только по отдельной кнопке.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from ..bot.engine import threshold_of
from ..db import Database
from ..models import ConditionKind, QtyMode, Reference, RuleSpec
from ..money import D, fmt_int, fmt_pct, fmt_rub
from ..sim.runner import CLIENT, INSTRUMENT, Simulation
from ..sim.scenarios import SCENARIOS
from . import parser, texts
from .llm import LLMClient, LLMUnavailable
from .notifier import Notifier

log = logging.getLogger(__name__)

MAX_PRICE = Decimal(1_000_000)
MAX_SHARES = Decimal(1_000_000)
MAX_TTL_SEC = 24 * 3600
RULE_VERBS = {"pause": "pause_rule", "resume": "resume_rule", "edit": "edit_rule", "delete": "delete_rule"}
ACTION_TITLES = {"pause_rule": ("приостановить", "Приостановить"), "resume_rule": ("возобновить", "Возобновить"),
                 "delete_rule": ("удалить", "Удалить")}
FAILED = "Не получилось обработать сообщение. Правила не изменились. Попробуйте сформулировать иначе."


def reference_buttons(draft_id: str) -> list:
    return [[{"text": "От цены покупки", "data": f"ref:{draft_id}:buy_price"}],
            [{"text": "От максимума после включения", "data": f"ref:{draft_id}:peak_since_enable"}]]


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


class ChatService:
    def __init__(self, sim: Simulation, db: Database, notifier: Notifier, llm: LLMClient | None = None,
                 default_ttl: int = 60) -> None:
        self.sim, self.db, self.notifier, self.llm, self.default_ttl = sim, db, notifier, llm, default_ttl

    # --- вход

    async def handle_text(self, chat: str, text: str, user: str = "test") -> None:
        text = text.strip()
        self.db.add_chat(chat, "in", text, kind="user")
        try:
            await self._text(chat, text, user)
        except Exception:  # noqa: BLE001 — сообщение не теряется молча: клиент получает ответ, ошибка в журнале
            log.exception("Сбой обработки сообщения")
            self._say(chat, FAILED)

    async def handle_callback(self, chat: str, data: str, user: str = "test") -> None:
        try:
            self._callback(chat, data, user)
        except Exception:  # noqa: BLE001
            log.exception("Сбой обработки кнопки")
            self._say(chat, FAILED)

    async def _text(self, chat: str, text: str, user: str) -> None:
        if text.startswith("/"):
            await self._command(chat, text.split()[0].split("@")[0].lower())
            return
        draft = self.db.draft(chat)
        if parser.CANCEL.search(text):
            self._cancel(chat, draft)
            return
        parsed = await self._parse(chat, text, draft)
        if parsed.intent in ("pause_rule", "resume_rule", "delete_rule"):
            if parsed.source == "llm":
                self._ask_rule_action(chat, parsed.intent)      # модель только предлагает, решает кнопка
            else:
                self._rule_action(chat, parsed.intent)
            return
        if parsed.intent == "cancel":
            self._cancel(chat, draft)
            return
        if parsed.note == "negated":
            self._say(chat, "Страж настраивает только правила продажи. «Не продавай, если…» я не разбираю: напишите, "
                            "при каком условии продать и не дешевле какой цены.")
            return
        if parsed.note == "bad_number":
            self._say(chat, "Не понял число. Напишите его цифрами, например «не дешевле 850».")
            return
        if draft is not None:
            self._continue_draft(chat, draft, parsed, user)
            return
        if parsed.intent == "edit":
            self._start_edit(chat, parsed)
        elif parsed.intent == "rule" and parsed.fields:
            self._ask_or_confirm(chat, {"id": _new_id(), "mode": "create", "rule_id": None, "fields": _plain(parsed.fields)})
        else:
            self._say(chat, f"Не понял команду.\n{self._capability()}\n\n{texts.TEMPLATE_HELP}")

    def _callback(self, chat: str, data: str, user: str) -> None:
        action, _, arg = data.partition(":")
        draft = self.db.draft(chat)
        if action == "ref":
            draft_id, _, value = arg.partition(":")
            if value not in (r.value for r in Reference) or not self._current(chat, draft, draft_id):
                return
            draft["fields"]["reference"] = value
            draft["id"] = _new_id()                # показанный текст изменится: старые кнопки больше не действуют
            self._ask_or_confirm(chat, draft)
        elif action in ("confirm", "confirm_now"):
            self._confirm(chat, arg, user, consent=action == "confirm_now")
        elif action == "edit":
            if not self._current(chat, draft, arg):
                return
            draft["awaiting"] = "edit"
            self.db.save_draft(chat, draft)
            self._say(chat, "Что изменить? Например: «минимальная цена 800» или «падение на 15%».")
        elif action == "cancel":
            if arg and draft is not None and draft["id"] != arg:
                self._say(chat, "Эта кнопка относится к старому черновику. Текущий черновик не изменён.")
                return
            self._cancel(chat, draft)
        elif action == "noop":
            self._say(chat, "Хорошо, ничего не меняю.")
        elif action == "rule":
            verb, _, rule_id = arg.partition(":")
            if verb not in RULE_VERBS or not rule_id.isdigit():
                self._say(chat, "Эта кнопка больше не работает.")
                return
            self._rule_action(chat, RULE_VERBS[verb], int(rule_id))
        else:
            self._say(chat, "Эта кнопка больше не работает.")

    def _current(self, chat: str, draft: dict[str, Any] | None, draft_id: str) -> bool:
        """Кнопка относится к показанному сейчас черновику. Иначе честно сказать, что с ней."""
        if draft is not None and draft["id"] == draft_id:
            return True
        if self.db.claimed(f"draft-applied:{chat}:{draft_id}"):
            self._say(chat, "Этот черновик уже применён. Правила не изменились.")
        else:
            self._say(chat, "Этот черновик устарел. Правила не изменились.")
        return False

    def _say(self, chat: str, text: str, buttons: list | None = None) -> None:
        self.notifier.send(f"reply:{uuid.uuid4().hex}", text, buttons, chat=chat)

    def _capability(self) -> str:
        return "" if self.llm and self.llm.available else texts.DEMO_NOTE

    async def _parse(self, chat: str, text: str, draft: dict[str, Any] | None) -> parser.Parsed:
        if self.llm and self.llm.available:
            context = f"Сейчас обсуждается черновик: {draft['fields']}" if draft else ""
            try:
                return await self.llm.parse(text, context)
            except LLMUnavailable as exc:
                log.warning("Модель недоступна: %s", exc)
                self._say(chat, "Модель недоступна, разбираю по шаблонам. Торговый бот работает как обычно.")
        return parser.parse(text)

    def _cancel(self, chat: str, draft: dict[str, Any] | None) -> None:
        if draft is None:
            self._say(chat, "Черновика нет, отменять нечего. Активные правила не изменились.")
            return
        self.db.save_draft(chat, None)
        self._say(chat, "Черновик отменён, активные правила не изменились.")

    # --- черновик

    def _position(self) -> int:
        return self.sim.broker.get_positions(CLIENT).positions.get(INSTRUMENT.ticker, 0)

    def _continue_draft(self, chat: str, draft: dict[str, Any], parsed: parser.Parsed, user: str) -> None:
        fields = _plain(parsed.fields)
        if not fields:
            self._say(chat, "Не нашёл изменений. Ответьте кнопкой, напишите значение, например «минимальная цена 800», "
                            "или «отмена».")
            return
        draft["fields"] = _merge(draft["fields"], fields)
        draft["id"] = _new_id()                    # изменённый черновик нельзя подтвердить старой кнопкой
        draft.pop("awaiting", None)
        self._ask_or_confirm(chat, draft)

    def _start_edit(self, chat: str, parsed: parser.Parsed) -> None:
        rules = self.db.rules()
        if len(rules) != 1:
            self._say(chat, "Правил нет. Сначала создайте правило." if not rules else
                      "Правил несколько: выберите нужное в /rules и нажмите «Изменить».")
            return
        rule = rules[0]
        if rule.status != "active":
            self._say(chat, f"Правило №{rule.id} приостановлено. Сначала возобновите его, затем меняйте.")
            return
        draft = {"id": _new_id(), "mode": "edit", "rule_id": rule.id, "base_version": rule.version,
                 "fields": _merge(_from_spec(rule.spec), _plain(parsed.fields))}
        self._ask_or_confirm(chat, draft)

    def _ask_or_confirm(self, chat: str, draft: dict[str, Any]) -> None:
        f = draft["fields"]
        if f.get("floor_pct") is not None:
            if not self._floor_from_pct(chat, draft):
                return
        if not f.get("condition"):
            self._hold(chat, draft, "При каком условии начать продажу? Например: «падение на 10% от цены покупки» или «цена 900 рублей».")
        elif f["condition"] in ("drop_pct", "drop_abs") and not f.get("reference"):
            amount = f"{f['value']}%" if f["condition"] == "drop_pct" else f"{f['value']} ₽"
            self._hold(chat, draft, f"Снижение на {amount} от чего считать: от цены покупки "
                                    f"{fmt_rub(self.sim.settings.buy_price)} или от максимума после включения правила?",
                       reference_buttons(draft["id"]))
        elif f.get("floor") is None and not f.get("no_floor"):
            self._hold(chat, draft, "Какая минимальная цена продажи? Дешевле бот не продаст. Напишите, например, "
                                    "«минимальная цена 850» или «без минимальной цены».")
        else:
            self._show(chat, draft)

    def _floor_from_pct(self, chat: str, draft: dict[str, Any]) -> bool:
        """«Не дешевле 85%» — это 85% от цены покупки, в рублях. Клиент видит пересчёт в подтверждении."""
        f = draft["fields"]
        try:
            pct = D(f.pop("floor_pct"))
        except (InvalidOperation, ValueError):
            pct = Decimal(-1)
        if not Decimal(0) < pct <= Decimal(100):
            self._hold(chat, draft, "Минимальная цена в процентах должна быть от 0 до 100% цены покупки. "
                                    "Напишите её в рублях, например «не дешевле 850».")
            return False
        buy = self.sim.settings.buy_price
        f["floor"] = str((buy * pct / 100).quantize(INSTRUMENT.tick))
        f["floor_note"] = f"{fmt_pct(pct)} от цены покупки {fmt_rub(buy)}"
        f["no_floor"] = False
        return True

    def _hold(self, chat: str, draft: dict[str, Any], question: str, buttons: list | None = None) -> None:
        self.db.save_draft(chat, draft)
        self._say(chat, question, buttons)

    def _spec(self, fields: dict[str, Any]) -> RuleSpec:
        qty_mode = QtyMode(fields.get("qty_mode") or "all")
        condition = ConditionKind(fields["condition"])
        ttl = fields.get("order_ttl_sec")
        return RuleSpec(INSTRUMENT.ticker, condition, D(fields["value"]),
                        Reference(fields["reference"]) if condition is not ConditionKind.PRICE_BELOW else None, qty_mode,
                        D(fields["qty_value"]) if qty_mode is not QtyMode.ALL else None,
                        None if fields.get("no_floor") and fields.get("floor") is None else D(fields["floor"]),
                        int(ttl) if ttl is not None else self.default_ttl)

    def _errors(self, spec: RuleSpec) -> list[str]:
        """Ошибки не дают подтвердить правило. Проверяются до любого форматирования чисел."""
        errors = []
        buy = self.sim.settings.buy_price
        if not spec.value.is_finite() or spec.value <= 0:
            errors.append("Условие должно быть положительным числом.")
        elif spec.condition is ConditionKind.DROP_PCT and not spec.value < 100:
            errors.append("Снижение должно быть больше 0% и меньше 100%.")
        elif spec.condition is ConditionKind.DROP_ABS and spec.reference is Reference.BUY_PRICE and not spec.value < buy:
            errors.append(f"Снижение в рублях должно быть меньше цены покупки {fmt_rub(buy)}.")
        elif spec.condition is not ConditionKind.DROP_PCT and spec.value > MAX_PRICE:
            errors.append("Цена условия слишком большая: допустимо не больше 1 000 000 ₽.")
        elif spec.condition is not ConditionKind.DROP_PCT and spec.value != spec.value.quantize(INSTRUMENT.tick):
            errors.append("Цена условия задаётся с точностью до копейки.")
        if spec.floor is not None:
            if not spec.floor.is_finite() or spec.floor <= 0:
                errors.append("Минимальная цена должна быть больше нуля.")
            elif spec.floor > MAX_PRICE:
                errors.append("Минимальная цена слишком большая: допустимо не больше 1 000 000 ₽.")
            elif spec.floor != spec.floor.quantize(INSTRUMENT.tick):
                errors.append("Минимальная цена задаётся с точностью до копейки.")
        if spec.qty_mode is QtyMode.SHARES:
            q = spec.qty_value
            if not q.is_finite() or q <= 0 or q != q.to_integral_value():
                errors.append("Количество акций должно быть целым положительным числом.")
            elif q > MAX_SHARES:
                errors.append("Количество акций слишком большое: допустимо не больше 1 000 000.")
        if spec.qty_mode is QtyMode.PERCENT and not (spec.qty_value.is_finite() and Decimal(0) < spec.qty_value <= Decimal(100)):
            errors.append("Доля позиции должна быть больше 0% и не больше 100%.")
        if not 1 <= spec.order_ttl_sec <= MAX_TTL_SEC:
            errors.append("Срок заявки должен быть от 1 секунды до 24 часов учебного времени.")
        return errors

    def _warnings(self, spec: RuleSpec, draft: dict[str, Any]) -> tuple[list[str], bool]:
        """Предупреждения показываются, но не мешают. Второе значение: условие уже выполнено на текущей цене."""
        warnings = []
        position = self._position()
        buy = self.sim.settings.buy_price
        if spec.qty_mode is QtyMode.SHARES and spec.qty_value > position:
            warnings.append(f"В позиции {texts.shares(position)}: продать больше нельзя.")
        note = draft["fields"].get("floor_note")
        if note and spec.floor is not None:
            warnings.append(f"Минимальную цену {note} я понял как {fmt_rub(spec.floor)}.")
        threshold = texts.threshold_of(spec, buy)
        if spec.floor is not None and threshold is not None and spec.floor > threshold:
            warnings.append(f"Минимальная цена {fmt_rub(spec.floor)} выше порога {fmt_rub(threshold)}: "
                            "при срабатывании покупателей по такой цене, скорее всего, не будет.")
        if spec.floor is None:
            warnings.append("Без минимальной цены заявка заберёт любых покупателей, даже по очень низкой цене.")
        if draft["mode"] == "create":
            others = [r for r in self.db.rules() if r.status == "active"]
            if others:
                warnings.append(f"Уже есть правило №{others[0].id} на эту бумагу. Первое сработавшее зарезервирует бумаги, "
                                "второму может не хватить.")
        immediate = self._already_met(spec, draft.get("rule_id"))
        if immediate:
            when = ("бот на паузе, продажа начнётся сразу после /resume" if self.sim.bots[CLIENT].paused
                    else "продажа начнётся сразу после включения")
            warnings.append(f"Условие уже выполнено: цена {fmt_rub(self.sim.exchange.last)}, {when}.")
        return warnings, immediate

    def _already_met(self, spec: RuleSpec, rule_id: int | None) -> bool:
        last = self.sim.exchange.last
        runtime = self.sim.bots[CLIENT].runtimes.get(rule_id) if rule_id else None
        peak = runtime.peak if runtime and runtime.peak is not None else last
        return last <= threshold_of(spec, self.sim.settings.buy_price, peak)

    def _show(self, chat: str, draft: dict[str, Any], prefix: str = "") -> None:
        try:
            spec = self._spec(draft["fields"])
        except (KeyError, ValueError, InvalidOperation):
            self._hold(chat, draft, "Не хватает данных для правила. Уточните условие и минимальную цену.")
            return
        errors = self._errors(spec)
        if errors:
            self._hold(chat, draft, "Так правило включить нельзя:\n" + "\n".join(f"• {e}" for e in errors)
                       + "\n\nИсправьте значение или напишите «отмена».")
            return
        warnings, immediate = self._warnings(spec, draft)
        draft["immediate"] = immediate
        self.db.save_draft(chat, draft)
        buy, position = self.sim.settings.buy_price, self._position()
        text = texts.confirmation(spec, buy, position, warnings)
        if draft["mode"] == "edit":
            old = next(r for r in self.db.rules(include_deleted=True) if r.id == draft["rule_id"])
            text = f"Изменение правила №{old.id}, сейчас версия {old.version}:\n{texts.diff(old.spec, spec, buy, position)}\n\n{text}"
        if immediate:
            title = "Включить и продать сейчас" if draft["mode"] == "create" else "Применить и продать сейчас"
            if self.sim.bots[CLIENT].paused:
                title = title.replace("продать сейчас", "продать после /resume")
            confirm = {"text": title, "data": f"confirm_now:{draft['id']}"}
        else:
            confirm = {"text": "Включить" if draft["mode"] == "create" else "Применить изменения", "data": f"confirm:{draft['id']}"}
        self._say(chat, prefix + text, [[confirm, {"text": "Изменить", "data": f"edit:{draft['id']}"},
                                         {"text": "Отменить", "data": f"cancel:{draft['id']}"}]])

    def _confirm(self, chat: str, draft_id: str, user: str, consent: bool = False) -> None:
        draft = self.db.draft(chat)
        if not self._current(chat, draft, draft_id):
            return
        spec = self._spec(draft["fields"])
        if self._errors(spec):
            self._show(chat, draft)
            return
        if self._already_met(spec, draft.get("rule_id")) and not consent:
            # цена дошла до порога после показа: молча продавать нельзя, нужна кнопка явного согласия
            draft["id"] = _new_id()
            self._show(chat, draft, "Пока вы читали, цена дошла до порога. Подтвердите ещё раз.\n\n")
            return
        if draft["mode"] == "edit":
            current = next((r for r in self.db.rules(include_deleted=True) if r.id == draft["rule_id"]), None)
            if current is None or current.status == "deleted":
                self.db.save_draft(chat, None)
                self._say(chat, f"Правило №{draft['rule_id']} удалено. Изменение не применено.")
                return
            if current.status != "active":
                self.db.save_draft(chat, None)
                self._say(chat, f"Правило №{current.id} приостановлено. Изменение не применено: сначала возобновите правило.")
                return
            if draft.get("base_version") not in (None, current.version):
                self.db.save_draft(chat, None)
                self._say(chat, f"Правило №{current.id} уже изменилось (версия {current.version}). Напишите изменение заново.")
                return
            try:
                rule = self.db.add_version(current.id, spec, user, "изменение через чат")
            except ValueError as exc:
                self.db.save_draft(chat, None)
                self._say(chat, f"{exc}. Изменение не применено.")
                return
            self.sim.apply_rule(rule)
            title = f"Правило №{rule.id} обновлено до версии {rule.version}."
        else:
            rule = self.db.create_rule(spec, user)
            self.sim.apply_rule(rule)
            title = f"Правило №{rule.id} включено. Торговый бот следит за ценой."
        self.db.save_draft(chat, None)
        self.db.claim(f"draft-applied:{chat}:{draft_id}")
        self.sim.log(CLIENT, "rule", title, rule_id=rule.id, version=rule.version, spec=spec.to_dict(), confirmed_by=user)
        self.notifier.send(f"rule:{rule.id}:v{rule.version}", title, chat=chat)

    def _pick_rule(self, chat: str, rule_id: int | None):
        rules = self.db.rules()
        rule = next((r for r in rules if r.id == rule_id), None) if rule_id is not None else (rules[0] if len(rules) == 1 else None)
        if rule is None:
            self._say(chat, "Такого правила нет." if rule_id is not None and rules else "Правил нет." if not rules
                      else "Правил несколько: выберите нужное в /rules.")
        return rule

    def _ask_rule_action(self, chat: str, intent: str) -> None:
        """Действие, которое предложила модель, выполняется только по кнопке: вставленный текст не может удалить правило."""
        rule = self._pick_rule(chat, None)
        if rule is None:
            return
        verb, title = ACTION_TITLES[intent]
        self._say(chat, f"Вы хотите {verb} правило №{rule.id}? Сделаю это только по кнопке.",
                  [[{"text": f"{title} правило №{rule.id}", "data": f"rule:{intent.split('_')[0]}:{rule.id}"},
                    {"text": "Ничего не менять", "data": "noop"}]])

    def _rule_action(self, chat: str, intent: str, rule_id: int | None = None) -> None:
        rule = self._pick_rule(chat, rule_id)
        if rule is None:
            return
        if intent == "pause_rule":
            self.db.set_rule_status(rule.id, "paused")
            self.db.drop_edit_drafts(rule.id)
            self.sim.bots[CLIENT].pause_rule(rule.id)
            text = f"Правило №{rule.id} приостановлено. Его неисполненные заявки сняты."
        elif intent == "resume_rule":
            self.db.set_rule_status(rule.id, "active")
            self.db.drop_edit_drafts(rule.id)
            self.sim.bots[CLIENT].resume_rule(rule.id)
            text = f"Правило №{rule.id} снова действует."
        elif intent == "edit_rule":
            if rule.status != "active":
                self._say(chat, f"Правило №{rule.id} приостановлено. Сначала возобновите его, затем меняйте.")
                return
            self.db.save_draft(chat, {"id": _new_id(), "mode": "edit", "rule_id": rule.id, "base_version": rule.version,
                                      "fields": _from_spec(rule.spec), "awaiting": "edit"})
            self._say(chat, "Что изменить? Например: «минимальная цена 800».")
            return
        elif intent == "delete_rule":
            self.db.set_rule_status(rule.id, "deleted")
            self.db.drop_edit_drafts(rule.id)
            self.sim.remove_rule(rule.id)
            text = f"Правило №{rule.id} удалено. Сделки остаются, неисполненные заявки сняты."
        else:
            return
        self.sim.log(CLIENT, "rule", text, rule_id=rule.id)
        self._say(chat, text)

    # --- команды

    async def _command(self, chat: str, command: str) -> None:
        handler: Callable[[str], None] | None = {
            "/start": self._cmd_help, "/help": self._cmd_help, "/portfolio": self._cmd_portfolio, "/rules": self._cmd_rules,
            "/status": self._cmd_status, "/pause": self._cmd_pause, "/resume": self._cmd_resume}.get(command)
        if command == "/report":
            await self._cmd_report(chat)
        elif handler:
            handler(chat)
        else:
            self._say(chat, "Такой команды нет. Список: /help")

    def _cmd_help(self, chat: str) -> None:
        self._say(chat, "Я Страж, учебный помощник. Принимаю правила и объясняю, что сделал торговый бот.\n"
                        "/portfolio · /rules · /status · /pause · /resume · /report\n\n"
                        f"{self._capability()}\n\n{texts.TEMPLATE_HELP}".replace("\n\n\n", "\n\n"))

    def _cmd_portfolio(self, chat: str) -> None:
        p = self.sim.broker.get_positions(CLIENT)
        qty = p.positions.get(INSTRUMENT.ticker, 0)
        last = self.sim.exchange.last
        self._say(chat, f"Портфель (учебный счёт)\n{texts.NAME}: {texts.shares(qty)} по цене {fmt_rub(last)} = {fmt_rub(last * qty)}\n"
                        f"В заявках: {texts.shares(p.reserved.get(INSTRUMENT.ticker, 0))}\nСвободные деньги: {fmt_rub(p.cash)}")

    def _cmd_rules(self, chat: str) -> None:
        rules = self.db.rules()
        if not rules:
            self._say(chat, "Правил нет. Напишите условие, например: «Если цена упадёт на 10% от цены покупки, продай всё не дешевле 850».")
            return
        bot = self.sim.bots[CLIENT]
        for rule in rules:
            runtime = bot.runtimes.get(rule.id)
            status = ("приостановлено" if rule.status == "paused" else bot.state_title(runtime) if runtime else "не загружено")
            lines = "\n".join(f"• {l}" for l in texts.rule_lines(rule.spec, self.sim.settings.buy_price, self._position()))
            verb = ("resume", "Возобновить") if rule.status == "paused" else ("pause", "Приостановить")
            self._say(chat, f"Правило №{rule.id}, версия {rule.version}: {status}\n{lines}",
                      [[{"text": verb[1], "data": f"rule:{verb[0]}:{rule.id}"}, {"text": "Изменить", "data": f"rule:edit:{rule.id}"},
                        {"text": "Удалить", "data": f"rule:delete:{rule.id}"}]])

    def _cmd_status(self, chat: str) -> None:
        sim, bot = self.sim, self.sim.bots[CLIENT]
        scenario = SCENARIOS[sim.scenario_key].title if sim.scenario_key else "сценарий не выбран"
        ex = sim.exchange
        state = "приостановлен" if bot.paused else "работает" if bot.runtimes else "ждёт правила"
        best = f"лучшая цена покупателя {fmt_rub(ex.best_bid)}" if ex.best_bid else "покупателей сейчас нет совсем"
        lines = [f"Бот: {state}", f"Сценарий: {scenario} ({sim.status})", f"Торги: {'остановлены' if ex.halted else 'идут'}",
                 f"Цена: {fmt_rub(ex.last)}, {best}"]
        if sim.failure:
            lines.append("Прогон остановлен из-за внутренней ошибки. Нажмите «Сброс».")
        for r in bot.runtimes.values():
            line = f"Правило №{r.rule.id}: {bot.state_title(r)}"
            if r.target_qty:                   # до срабатывания объёма ещё нет: «0 из 0» не показываем
                line += f", продано {fmt_int(r.sold_qty)} из {texts.shares(r.target_qty)}"
            lines.append(line)
        self._say(chat, "\n".join(lines))

    def _cmd_pause(self, chat: str) -> None:
        cancelled = self.sim.pause_bots()
        client = [c for c in cancelled if f":{CLIENT}:" in c]
        self.sim.log(CLIENT, "control", f"Пауза: снято заявок {len(client)}")
        self._say(chat, f"Бот на паузе. Снято неисполненных заявок: {len(client)}. Уже исполненные сделки остаются.")

    def _cmd_resume(self, chat: str) -> None:
        self.sim.resume_bots()
        self.sim.log(CLIENT, "control", "Работа возобновлена")
        self._say(chat, "Работа возобновлена.")

    async def _cmd_report(self, chat: str) -> None:
        report = self.sim.report()
        text = texts.report_text(report)
        if self.llm and self.llm.available:
            text = await self.llm.explain(report.to_dict(), text)
            text = f"{texts.report_text(report)}\n\nОбъяснение: {text}" if text != texts.report_text(report) else text
        self._say(chat, text)


def _plain(fields: dict[str, Any]) -> dict[str, Any]:
    """Черновик хранится в JSON: числа как строки, без потери точности."""
    return {k: str(v) if isinstance(v, Decimal) else v for k, v in fields.items()}


def _merge(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Изменение поверх черновика. «Без минимальной цены» снимает границу, новая граница отменяет отказ от неё."""
    out = dict(old)
    if new.get("no_floor"):
        out.pop("floor", None)
        out.pop("floor_note", None)
    if "floor" in new or "floor_pct" in new:
        out.pop("floor", None)
        out.pop("floor_note", None)
        out["no_floor"] = False
    if new.get("condition") == "price_below":
        out.pop("reference", None)
    if new.get("qty_mode") == "all":
        out["qty_value"] = None
    out.update(new)
    return out


def _from_spec(spec: RuleSpec) -> dict[str, Any]:
    d = spec.to_dict()
    d["no_floor"] = spec.floor is None
    return {k: v for k, v in d.items() if k != "ticker" and v is not None or k in ("qty_value",)}
