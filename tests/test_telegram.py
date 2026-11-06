"""Telegram через поддельный Bot API: настоящий сервер не использовался."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from app.agent.dialog import ChatService
from app.agent.notifier import Notifier
from app.agent.telegram import TelegramService
from app.sim.runner import CLIENT, Simulation

CHAT, USER, STRANGER = 500, 77, 88


class FakeBotAPI:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.answers: list[dict] = []
        self.fail = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        if self.fail:
            return httpx.Response(502, json={"ok": False, "description": "bad gateway"})
        if method == "sendMessage":
            self.sent.append(body)
        elif method == "answerCallbackQuery":
            self.answers.append(body)
        return httpx.Response(200, json={"ok": True, "result": []})


@pytest.fixture
def tg(settings, db):
    conf = replace(settings, telegram_token="123:SECRET", telegram_chat_id=CHAT, telegram_allowed_users=frozenset({USER}))
    notifier = Notifier(db, True)
    sim = Simulation(conf, db, notifier.on_event)
    notifier.sim = sim
    chat = ChatService(sim, db, notifier)
    api = FakeBotAPI()
    service = TelegramService(conf, db, chat, transport=httpx.MockTransport(api))
    return service, api, sim, db


def text_update(uid: int, text: str, user: int = USER, chat: int = CHAT) -> dict:
    return {"update_id": uid, "message": {"text": text, "from": {"id": user}, "chat": {"id": chat}}}


def button_update(uid: int, data: str, user: int = USER) -> dict:
    return {"update_id": uid, "callback_query": {"id": "cb", "data": data, "from": {"id": user}, "message": {"chat": {"id": CHAT}}}}


def go(coro):
    return asyncio.run(coro)


MAIN = "Если цена упадёт на 10% от цены покупки, продай всё. Не дешевле 850 рублей"


def test_rule_through_telegram_buttons(tg):
    service, api, sim, db = tg
    go(service.handle_update(text_update(1, MAIN)))
    go(service.send_pending())
    keyboard = api.sent[-1]["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in keyboard] == ["Включить", "Изменить", "Отменить"]
    go(service.handle_update(button_update(2, keyboard[0]["callback_data"])))
    go(service.send_pending())
    assert db.rules()[0].spec.floor == 850 and api.answers
    assert "включено" in api.sent[-1]["text"] and all(m["chat_id"] == CHAT for m in api.sent)


def test_stranger_is_ignored(tg):
    service, api, sim, db = tg
    go(service.handle_update(text_update(1, MAIN, user=STRANGER)))
    go(service.handle_update(text_update(2, MAIN, user=USER, chat=999)))
    go(service.send_pending())
    assert api.sent == [] and db.draft(str(CHAT)) is None


def test_repeated_update_is_processed_once(tg):
    service, api, sim, db = tg
    for _ in range(3):
        go(service.handle_update(text_update(5, "/portfolio")))
    go(service.send_pending())
    assert len(api.sent) == 1


def test_failed_delivery_does_not_repeat_sale_and_is_retried(tg):
    service, api, sim, db = tg
    go(service.handle_update(text_update(1, MAIN)))
    data = json.loads(db.pending_outbox()[0]["buttons"])[0][0]["data"]
    go(service.handle_update(button_update(2, data)))
    api.fail = True
    sim.command("price", price=Decimal(890))
    sim.advance(2000)
    orders = db.orders(sim.run_id, CLIENT)
    go(service.send_pending())
    assert len(orders) == 1 and sim.report().sold_qty > 0, "сделка прошла, хотя Telegram лежит"
    db.conn.execute("UPDATE outbox SET next_try = 0")
    api.fail = False
    go(service.send_pending())
    assert api.sent and db.outbox_stats().get("pending", 0) == 0
    assert len(db.orders(sim.run_id, CLIENT)) == 1, "повторная отправка уведомления не запускает продажу"


def test_token_is_not_in_repr(tg):
    service, *_ = tg
    assert "SECRET" not in repr(service)
