"""Главный приёмочный путь через HTTP: чат → подтверждение → обвал в панели → действия → сообщение совпадает с журналом."""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.api import Platform, create_app

MAIN = "Следи за моей позицией. Если цена упадёт на 10% от цены покупки, начни продажу. Не продавай дешевле 850 рублей."


@pytest.fixture
def client(settings):
    platform = Platform(settings)
    app = create_app(settings, platform, drive=False)
    with TestClient(app) as test_client:
        test_client.platform = platform
        yield test_client


def enable_rule(client) -> None:
    client.post("/api/chat", json={"text": MAIN})
    rows = client.get("/api/chat").json()
    data = next(b["data"] for row in rows[-1]["buttons"] for b in row if b["text"] == "Включить")
    assert client.post("/api/chat/callback", json={"data": data}).status_code == 200


def test_main_acceptance_path_with_crash(client):
    enable_rule(client)
    state = client.get("/api/state").json()
    assert state["rules"][0]["version"] == 1 and state["bot"]["runtimes"][0]["state"] == "armed"

    assert client.post("/api/market/crash", json={"percent": 50}).status_code == 200
    client.platform.sim.advance(70_000)

    state = client.get("/api/state").json()
    runtime = state["bot"]["runtimes"][0]
    assert runtime["state"] == "expired" and runtime["sold"] == 0
    assert state["orders"][0]["status"] == "expired" and state["executions"] == []
    assert state["report"]["remaining_qty"] == 100 and state["report"]["revenue"] == "0.00"

    chat = [m["text"].replace("\u00a0", " ") for m in client.get("/api/chat").json()]
    assert any("Продано: 0 из 100 акций" in t and "нет покупателей по разрешённой цене" in t for t in chat)
    kinds = {j["text"].split(":")[0] for j in state["journal"]}
    assert {"Сработало условие", "Заявка отправлена", "Продажа остановлена"} <= kinds


def test_acceptance_message_numbers_equal_engine_when_partly_sold(client):
    enable_rule(client)
    client.post("/api/market/demand", json={"scale": "0.3"})
    client.post("/api/market/price", json={"price": 895})
    client.platform.sim.advance(70_000)
    state = client.get("/api/state").json()
    report = state["report"]
    sold = report["sold_qty"]
    assert 0 < sold < 100
    summary = next(m["text"].replace("\u00a0", " ") for m in client.get("/api/chat").json()
                   if m["text"].startswith("Сработало правило"))
    assert f"Продано: {sold} из 100 акций" in summary and f"Осталось: {100 - sold} акций" in summary
    average = Decimal(report["avg_price"])
    assert f"{average:.2f}".replace(".", ",") in summary
    assert sum(e["qty"] for e in state["executions"]) == sold


def test_scenario_via_api_and_report_command(client):
    enable_rule(client)
    assert client.post("/api/sim/start", json={"scenario": "rebound"}).status_code == 200
    sim = client.platform.sim
    while sim.status == "running":
        sim.advance(1000)
    report = client.get("/api/state").json()["report"]
    assert Decimal(report["difference"]) < 0
    client.post("/api/chat", json={"text": "/report"})
    assert any("−" in m["text"] and "Разница со сценарием без бота" in m["text"] for m in client.get("/api/chat").json())


def test_panel_goes_to_same_engine_as_chat(client):
    client.post("/api/market/price", json={"price": 700})
    client.post("/api/chat", json={"text": "/status"})
    assert "Цена: 700\u00a0₽" in client.get("/api/chat").json()[-1]["text"]


def test_reset_through_api(client):
    enable_rule(client)
    client.post("/api/market/crash", json={"percent": 50})
    run = client.get("/api/state").json()["run"]["id"]
    client.post("/api/sim/reset", json={})
    state = client.get("/api/state").json()
    assert state["run"]["id"] != run and state["orders"] == [] and state["market"]["last"] == "1000"
    assert len(state["rules"]) == 1


@pytest.mark.parametrize("path,body", [("/api/market/price", {"price": -5}), ("/api/market/price", {"price": "abc"}),
                                       ("/api/market/crash", {"percent": 150}), ("/api/sim/start", {"scenario": "нет"}),
                                       ("/api/sim/speed", {"speed": 0}), ("/api/sim/fleet", {"size": 5000})])
def test_bad_input_is_refused(client, path, body):
    assert client.post(path, json=body).status_code == 422


def test_foreign_host_and_origin_are_refused(client):
    assert client.get("/api/state", headers={"host": "evil.example"}).status_code == 403
    assert client.post("/api/market/halt", json={}, headers={"origin": "https://evil.example"}).status_code == 403


def test_local_mode_needs_no_login(client):
    assert client.get("/api/session").json()["mode"] == "owner"
    assert client.post("/api/market/halt", json={}).status_code == 200


def test_state_contains_no_secrets(settings):
    from dataclasses import replace
    conf = replace(settings, telegram_token="123:SECRET", telegram_chat_id=1, telegram_allowed_users=frozenset({1}), llm_api_key="sk-SECRET")
    with TestClient(create_app(conf, Platform(conf), drive=False)) as client:
        assert "SECRET" not in client.get("/api/state").text


def test_interface_page_has_required_label(client):
    page = client.get("/")
    assert page.status_code == 200 and "Учебный симулятор · искусственные данные" in page.text
