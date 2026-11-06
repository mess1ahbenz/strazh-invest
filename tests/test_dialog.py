"""Диалог: уточнения, подтверждение, версии, команды, демо-режим и проверка ответов модели."""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.agent.dialog import ChatService
from app.agent.llm import LLMClient, numbers_in, verified_fields
from app.agent.notifier import TEST_CHAT, Notifier
from app.agent.parser import parse, parse_fields
from app.models import ConditionKind, QtyMode, Reference
from app.sim.runner import CLIENT, Simulation

MAIN = "Следи за моей позицией. Если цена упадёт на 10% от цены покупки, начни продажу. Не продавай дешевле 850 рублей."


class Chat:
    def __init__(self, settings, db, llm=None) -> None:
        self.db = db
        self.notifier = Notifier(db, False)
        self.sim = Simulation(settings, db, self.notifier.on_event)
        self.notifier.sim = self.sim
        self.service = ChatService(self.sim, db, self.notifier, llm)
        self.seen = 0

    def say(self, text: str) -> list[dict]:
        asyncio.run(self.service.handle_text(TEST_CHAT, text))
        return self.new()

    def press(self, data: str) -> list[dict]:
        asyncio.run(self.service.handle_callback(TEST_CHAT, data))
        return self.new()

    def new(self) -> list[dict]:
        """Ответы с обычными пробелами вместо неразрывных: так проверки короче. Сами пробелы проверяет отдельный тест."""
        all_rows = self.db.chat(TEST_CHAT, self.seen)
        rows = [{**m, "text": m["text"].replace("\u00a0", " ")} for m in all_rows if m["direction"] == "out"]
        self.seen = all_rows[-1]["id"] if all_rows else self.seen
        return rows

    def button(self, rows: list[dict], title: str) -> str:
        return next(b["data"] for m in rows for row in m["buttons"] for b in row if b["text"] == title)


@pytest.fixture
def chat(settings, db) -> Chat:
    return Chat(settings, db)


def test_main_example_is_understood_and_needs_confirmation(chat):
    rows = chat.say(MAIN)
    text = rows[-1]["text"]
    for part in ("Я правильно понял?", "следить за акциями Пример-Тех", "цена опустится до 900 ₽: снижение на 10% от цены покупки 1 000 ₽",
                 "продать до 100 акций",
                 "не выставлять продажу дешевле 850 ₽", "покупателей по разрешённой цене может не оказаться"):
        assert part in text, part
    assert chat.db.rules() == [], "до подтверждения правила нет"
    assert [b["text"] for b in rows[-1]["buttons"][0]] == ["Включить", "Изменить", "Отменить"]


def test_confirmation_reaches_bot_with_exact_terms(chat):
    rows = chat.say(MAIN)
    chat.press(chat.button(rows, "Включить"))
    rule = chat.db.rules()[0]
    spec = rule.spec
    assert (spec.condition, spec.value, spec.reference, spec.floor, spec.qty_mode) == (
        ConditionKind.DROP_PCT, Decimal(10), Reference.BUY_PRICE, Decimal(850), QtyMode.ALL)
    assert rule.id in chat.sim.bots[CLIENT].runtimes
    assert chat.db.rule_versions(rule.id)[0]["confirmed_by"] == "test"


def test_ambiguous_reference_is_asked_not_guessed(chat):
    rows = chat.say("Продай при падении на 10%. Не дешевле 850")
    assert "от чего считать" in rows[-1]["text"] and chat.db.rules() == []
    rows = chat.press(chat.button(rows, "От максимума после включения"))
    assert "максимума после включения" in rows[-1]["text"]
    chat.press(chat.button(rows, "Включить"))
    assert chat.db.rules()[0].spec.reference is Reference.PEAK


def test_reference_can_be_answered_in_words(chat):
    chat.say("Продай при падении на 10%. Не дешевле 850")
    rows = chat.say("от цены покупки")
    assert "от цены покупки" in rows[-1]["text"] and rows[-1]["buttons"]


def test_missing_floor_is_asked(chat):
    rows = chat.say("Продай при падении на 10% от цены покупки")
    assert "минимальная цена" in rows[-1]["text"].lower(), rows
    rows = chat.say("минимальная цена 800")
    assert "дешевле 800 ₽" in rows[-1]["text"]


def test_explicit_no_floor_is_shown_as_warning(chat):
    rows = chat.say("Продай всё при падении на 10% от цены покупки, без минимальной цены")
    assert "минимальная цена не задана" in rows[-1]["text"] and "⚠️" in rows[-1]["text"]
    chat.press(chat.button(rows, "Включить"))
    assert chat.db.rules()[0].spec.floor is None


def test_floor_above_threshold_is_warned(chat):
    rows = chat.say("Продай при падении на 10% от цены покупки, не дешевле 950")
    assert "выше порога" in rows[-1]["text"]


@pytest.mark.parametrize("text", ["Продай при падении на 150% от цены покупки, не дешевле 500"])
def test_impossible_rule_is_refused(chat, text):
    rows = chat.say(text)
    assert "нельзя" in rows[-1]["text"] and not rows[-1]["buttons"]


def test_edit_button_changes_draft_and_old_button_dies(chat):
    rows = chat.say(MAIN)
    enable, edit = chat.button(rows, "Включить"), chat.button(rows, "Изменить")
    chat.press(edit)
    new = chat.say("минимальная цена 800")
    assert "дешевле 800 ₽" in new[-1]["text"]
    old = chat.press(enable)
    assert "устарел" in old[-1]["text"] and chat.db.rules() == []
    chat.press(chat.button(new, "Включить"))
    assert chat.db.rules()[0].spec.floor == 800


def test_change_of_active_rule_needs_confirmation_and_creates_version(chat):
    chat.press(chat.button(chat.say(MAIN), "Включить"))
    rows = chat.say("Измени минимальную цену на 800")
    assert "Было: не выставлять продажу дешевле 850 ₽" in rows[-1]["text"]
    assert "Стало: не выставлять продажу дешевле 800 ₽" in rows[-1]["text"]
    rule = chat.db.rules()[0]
    assert rule.version == 1 and rule.spec.floor == 850, "без подтверждения ограничение не меняется"
    chat.press(chat.button(rows, "Применить изменения"))
    rule = chat.db.rules()[0]
    assert rule.version == 2 and rule.spec.floor == 800
    versions = chat.db.rule_versions(rule.id)
    assert [json.loads(v["spec"])["floor"] for v in versions] == ["850", "800"]
    assert chat.sim.bots[CLIENT].runtimes[rule.id].rule.version == 2


def test_rule_pause_resume_delete_in_words(chat):
    chat.press(chat.button(chat.say(MAIN), "Включить"))
    assert "приостановлено" in chat.say("Приостанови правило")[-1]["text"]
    assert chat.db.rules()[0].status == "paused"
    chat.sim.command("price", price=Decimal(890))
    chat.sim.advance(1000)
    assert chat.db.orders(chat.sim.run_id, CLIENT) == []
    assert "снова действует" in chat.say("Возобнови правило")[-1]["text"]
    assert "удалено" in chat.say("Удали правило")[-1]["text"]
    assert chat.db.rules() == [] and chat.db.rule_versions(1), "история версий сохраняется"


@pytest.mark.parametrize("command,part", [("/portfolio", "Свободные деньги: 0 ₽"), ("/rules", "Правил нет"), ("/status", "Бот: ждёт правила"),
                                          ("/report", "Разница со сценарием без бота"), ("/help", "Демо-режим")])
def test_commands(chat, command, part):
    assert part in chat.say(command)[-1]["text"]


def test_pause_and_resume_commands(chat):
    chat.press(chat.button(chat.say(MAIN), "Включить"))
    chat.sim.exec_delay_ms = 10_000
    chat.sim.command("price", price=Decimal(890))
    text = chat.say("/pause")[-1]["text"]
    assert "Снято неисполненных заявок: 1" in text and "исполненные сделки остаются" in text
    assert "возобновлена" in chat.say("/resume")[-1]["text"]


def test_unknown_text_in_demo_mode_says_limits_honestly(chat):
    text = chat.say("как думаешь, что будет с рынком?")[-1]["text"]
    assert "Демо-режим без языковой модели" in text and "ограниченные шаблоны" in text


def test_notifications_match_engine_and_do_not_repeat(chat):
    chat.press(chat.button(chat.say(MAIN), "Включить"))
    chat.sim.exchange.set_params(0, refill_per_sec=Decimal(0))
    chat.sim.command("crash", percent=Decimal(50))
    chat.sim.advance(70_000)
    rows = chat.new()
    texts = [m["text"] for m in rows]
    summary = next(t for t in texts if t.startswith("Сработало правило"))
    assert summary == ("Сработало правило по Пример-Тех, но продать не удалось.\nПродано: 0 из 100 акций. Сделок не было.\n"
                       "Осталось: 100 акций.\nПричина остановки: нет покупателей по разрешённой цене.")
    assert sum("Сработало условие" in t for t in texts) == 1
    assert sum("Продажа сейчас ограничена" in t for t in texts) == 1
    assert len(texts) <= 5, "не по сообщению на тик"


def test_same_notification_key_is_sent_once(chat):
    for _ in range(3):
        chat.notifier.event("same-key", "Одно событие")
    assert sum(m["text"] == "Одно событие" for m in chat.db.chat(TEST_CHAT)) == 1


def test_parser_forms():
    assert parse("Продай половину при падении на 15% от максимума, минимальная цена 800").fields == {
        "floor": Decimal(800), "qty_mode": "percent", "qty_value": Decimal(50), "condition": "drop_pct", "value": Decimal(15),
        "reference": "peak_since_enable"}
    fields = parse_fields("Если цена упадёт до 900 рублей, продай 50 акций не дешевле 850")
    assert fields == {"floor": Decimal(850), "qty_mode": "shares", "qty_value": Decimal(50), "condition": "price_below", "value": Decimal(900)}
    assert parse_fields("заявка живёт 5 минут")["order_ttl_sec"] == 300
    assert parse("привет").intent == "unknown"
    assert "reference" not in parse_fields("при падении на 10%"), "точка отсчёта не додумывается"


# --- модель

def fake_response(text: str, stop: str = "end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])


class FakeModel:
    def __init__(self, answer=None, error: Exception | None = None, stop: str = "end_turn") -> None:
        self.answer, self.error, self.stop = answer, error, stop
        self.calls = 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return fake_response(self.answer if isinstance(self.answer, str) else json.dumps(self.answer), self.stop)


BLANK = {"intent": "rule", "condition": None, "value": None, "reference": None, "qty_mode": None, "qty_value": None, "floor": None,
         "no_floor": False, "order_ttl_sec": None, "evidence": {}}


def test_model_cannot_invent_reference_or_numbers():
    raw = {**BLANK, "condition": "drop_pct", "value": "10", "reference": "buy_price", "floor": "700", "evidence": {"value": "при падении на 10%"}}
    fields = verified_fields(raw, "Продай при падении на 10%")
    assert fields == {"condition": "drop_pct", "value": Decimal(10)}


def test_model_field_needs_quote_from_text():
    raw = {**BLANK, "reference": "buy_price", "evidence": {"reference": "от цены покупки"}}
    assert verified_fields(raw, "от цены покупки") == {"reference": "buy_price"}
    assert verified_fields(raw, "от максимума") == {}


def test_model_path_still_asks_about_reference(settings, db):
    answer = {**BLANK, "condition": "drop_pct", "value": "10", "floor": "850", "evidence": {"value": "падении на 10%", "floor": "не дешевле 850"}}
    llm = LLMClient("", "m", FakeModel(answer))
    chat = Chat(settings, db, llm)
    rows = chat.say("Продай при падении на 10%, не дешевле 850")
    assert "от чего считать" in rows[-1]["text"]


def test_model_failure_falls_back_to_templates_and_says_so(settings, db):
    llm = LLMClient("", "m", FakeModel(error=ConnectionError("нет сети")))
    chat = Chat(settings, db, llm)
    rows = chat.say(MAIN)
    assert any("Модель недоступна" in m["text"] for m in rows) and "Я правильно понял?" in rows[-1]["text"]


def test_model_refusal_falls_back(settings, db):
    llm = LLMClient("", "m", FakeModel(BLANK, stop="refusal"))
    chat = Chat(settings, db, llm)
    assert "Я правильно понял?" in chat.say(MAIN)[-1]["text"]


def test_engine_does_not_call_model_on_market_path(settings, db):
    model = FakeModel(BLANK)
    chat = Chat(settings, db, LLMClient("", "m", model))
    rows = chat.say("Продай при падении на 10%")
    calls = model.calls
    chat.db.create_rule(__import__("conftest").rule_spec(), "test")
    chat.sim.reset()
    chat.sim.command("price", price=Decimal(890))
    chat.sim.advance(3000)
    assert model.calls == calls and chat.sim.report().sold_qty > 0


def test_explanation_with_foreign_number_is_discarded(settings):
    llm = LLMClient("", "m", FakeModel("Бот сохранил 999 ₽."))
    assert asyncio.run(llm.explain({"difference": "-23244.66"}, "обычный текст")) == "обычный текст"


def test_explanation_with_known_numbers_is_kept(settings):
    llm = LLMClient("", "m", FakeModel("Разница составила -23244.66."))
    assert asyncio.run(llm.explain({"difference": "-23244.66"}, "обычный текст")).startswith("Разница")


def test_numbers_in_handles_russian_format():
    assert numbers_in("1 000,50 ₽ и 850") == {Decimal("1000.5"), Decimal(850)}
