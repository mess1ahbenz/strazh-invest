"""Публичный сайт: изоляция посетителей, вход владельца, лимиты, заголовки, вебхук. Атаки из docs/review/adversarial.md."""
from __future__ import annotations

import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.api import Platform, create_app
from app.main import problems

HOST = "strazh.example.org"
PASSWORD = "очень-длинный-пароль-1"
RULE = "Если цена упадёт на 10% от цены покупки, продай всё. Не дешевле 850 рублей"


@pytest.fixture
def public(settings, tmp_path):
    return replace(settings, mode="public", public_host=HOST, owner_password=PASSWORD, secret_key="k" * 32,
                   db_path=str(tmp_path / "owner.db"))


def client(conf) -> TestClient:
    return TestClient(create_app(conf, drive=False), base_url=f"https://{HOST}")


def visit(c: TestClient) -> TestClient:
    assert c.get("/").status_code == 200
    return c


def test_each_visitor_gets_own_isolated_room(public):
    app = create_app(public, drive=False)
    with TestClient(app, base_url=f"https://{HOST}") as alice, TestClient(app, base_url=f"https://{HOST}") as bob:
        visit(alice), visit(bob)
        alice.post("/api/chat", json={"text": RULE})
        alice.post("/api/market/crash", json={"percent": 50})
        assert any(RULE in m["text"] for m in alice.get("/api/chat").json())
        assert bob.get("/api/chat").json() == [], "чужой чат не виден"
        assert bob.get("/api/state").json()["market"]["last"] == "1000", "чужой обвал не касается"
        assert alice.get("/api/session").json()["mode"] == "visitor"


def test_visitor_cannot_touch_owner_room(public):
    app = create_app(public, drive=False)
    owner = app.state.platform
    owner.db.create_rule(__import__("conftest").rule_spec(), "owner")
    with TestClient(app, base_url=f"https://{HOST}") as stranger:
        visit(stranger)
        stranger.post("/api/chat", json={"text": "Удали правило"})
        stranger.post("/api/chat", json={"text": "/pause"})
        assert len(owner.db.rules()) == 1 and not owner.sim.bots["client"].paused
        assert stranger.get("/api/state").json()["rules"] == []


def test_no_cookie_no_room(public):
    with client(public) as c:
        assert c.get("/api/state").status_code == 409


def test_owner_login_and_logout(public):
    with client(public) as c:
        visit(c)
        assert c.post("/api/login", json={"password": "неверно"}).status_code == 401
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
        session = c.get("/api/session").json()
        assert session["mode"] == "owner" and session["room"] == "owner"
        c.post("/api/logout")
        assert c.get("/api/session").json()["mode"] == "visitor"


def test_forged_owner_cookie_is_rejected(public):
    with client(public) as c:
        visit(c)
        c.cookies.set("strazh_owner", "9999999999.deadbeef", domain=HOST)
        assert c.get("/api/session").json()["mode"] == "visitor"


def test_login_attempts_are_limited(public):
    with client(public) as c:
        visit(c)
        codes = [c.post("/api/login", json={"password": "нет"}).status_code for _ in range(7)]
        assert codes[:5] == [401] * 5 and codes[-1] == 429


def test_chat_flood_is_limited(public):
    with client(public) as c:
        visit(c)
        codes = [c.post("/api/chat", json={"text": "/status"}).status_code for _ in range(25)]
        assert 429 in codes and codes[0] == 200


def test_full_site_still_lets_owner_in(public):
    app = create_app(replace(public, max_rooms=2), drive=False)
    clients = [visit(TestClient(app, base_url=f"https://{HOST}")) for _ in range(2)]
    with TestClient(app, base_url=f"https://{HOST}") as late:
        assert late.get("/").status_code == 200, "страница открывается и без комнаты"
        session = late.get("/api/session").json()
        assert session["mode"] == "full" and session["owner_login"]
        assert late.post("/api/login", json={"password": PASSWORD}).status_code == 200
        assert late.get("/api/session").json()["mode"] == "owner"
    assert len(app.state.rooms.visitors) == 2
    for c in clients:
        c.close()


def test_unwatched_rooms_make_way(public):
    """Поток пустых комнат не закрывает сайт: комнату, которую никто не открыл, вытесняет новый посетитель."""
    from app.api import ACTIVE_SEC
    app = create_app(replace(public, max_rooms=2), drive=False)
    for _ in range(2):
        with TestClient(app, base_url=f"https://{HOST}") as bot:
            visit(bot)
    for room in app.state.rooms.visitors.values():
        room.last_seen -= ACTIVE_SEC + 1
    with TestClient(app, base_url=f"https://{HOST}") as person:
        visit(person)
        assert person.get("/api/session").json()["mode"] == "visitor"
        assert person.get("/api/state").status_code == 200
    assert len(app.state.rooms.visitors) == 2


def test_stale_room_cookie_gets_fresh_room_at_once(public):
    with client(public) as c:
        c.cookies.set("strazh_room", "давно-истекла".encode().hex(), domain=HOST)
        assert c.get("/api/session").json()["mode"] == "visitor"
        assert c.get("/api/state").status_code == 200


def test_chunked_body_is_refused(public):
    with client(public) as c:
        visit(c)
        chunks = (b"x" * 4096 for _ in range(64))
        assert c.post("/api/chat", content=chunks, headers={"content-type": "application/json"}).status_code == 413
        assert c.get("/api/state").status_code == 200


@pytest.mark.parametrize("path,body", [("/api/market/gradual", {"price": 500, "seconds": 6_000_000}),
                                       ("/api/market/price", {"price": "1e27"}), ("/api/sim/fleet", {"size": 1, "seed": "1e5000"}),
                                       ("/api/market/price", {"price": "9" * 40}), ("/api/sim/delay", {"ms": -1})])
def test_absurd_numbers_are_refused(public, path, body):
    with client(public) as c:
        visit(c)
        assert c.post(path, json=body).status_code == 422
        assert c.get("/api/state").status_code == 200


def test_huge_body_is_refused(public):
    with client(public) as c:
        visit(c)
        assert c.post("/api/chat", content="x" * 20_000, headers={"content-type": "application/json"}).status_code == 413


def test_security_headers(public):
    with client(public) as c:
        response = c.get("/")
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["x-frame-options"] == "DENY" and "max-age" in response.headers["strict-transport-security"]
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie


def test_foreign_host_and_origin_refused(public):
    with client(public) as c:
        assert c.get("/", headers={"host": "evil.example"}).status_code == 403
        assert c.get("/api/state", headers={"host": "evil.example"}).status_code == 403
        assert c.get("/healthz", headers={"host": "10.0.0.7:10000"}).json() == {"ok": True}, "проверка хостинга идёт на внутренний адрес"
        visit(c)
        assert c.post("/api/market/halt", json={}, headers={"origin": "https://evil.example"}).status_code == 403


def test_webhook_requires_secret(public):
    conf = replace(public, telegram_token="1:x", telegram_chat_id=5, telegram_allowed_users=frozenset({5}),
                   telegram_mode="webhook", telegram_webhook_secret="s" * 30)
    app = create_app(conf, drive=False)
    calls = []

    async def fake(update):
        calls.append(update)

    app.state.platform.telegram.handle_webhook = fake
    with TestClient(app, base_url=f"https://{HOST}") as c:
        assert c.post("/telegram/hook", json={"update_id": 1}).status_code == 403
        assert c.post("/telegram/hook", json={"update_id": 1}, headers={"x-telegram-bot-api-secret-token": "wrong"}).status_code == 403
        assert c.post("/telegram/hook", json={"update_id": 1}, headers=[(b"x-telegram-bot-api-secret-token", "секрет".encode())]).status_code == 403
        assert c.post("/telegram/hook", json={"update_id": 2}, headers={"x-telegram-bot-api-secret-token": conf.telegram_hook_token}).status_code == 200
        assert c.post("/telegram/hook", json={"update_id": 3}, headers={"x-telegram-bot-api-secret-token": "s" * 30}).status_code == 403
    assert calls == [{"update_id": 2}]
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,256}", replace(conf, telegram_webhook_secret="a+b/c=" * 8).telegram_hook_token)


def test_owner_room_is_the_only_one_with_telegram(public):
    conf = replace(public, telegram_token="1:x", telegram_chat_id=5, telegram_allowed_users=frozenset({5}))
    app = create_app(conf, drive=False)
    with TestClient(app, base_url=f"https://{HOST}") as c:
        visit(c)
        room = next(iter(app.state.rooms.visitors.values()))
        assert room.telegram is None and room.notifier.telegram_enabled is False
        assert c.get("/api/session").json()["telegram"] is False
        c.post("/api/chat", json={"text": "/status"})
        assert app.state.platform.db.outbox_stats() == {}


def test_internal_errors_do_not_leak(public):
    app = create_app(public, drive=False)
    with TestClient(app, base_url=f"https://{HOST}", raise_server_exceptions=False) as c:
        visit(c)
        room = next(iter(app.state.rooms.visitors.values()))
        room.state = lambda: 1 / 0
        response = c.get("/api/state")
        assert response.status_code == 500 and response.json() == {"detail": "Внутренняя ошибка"}


@pytest.mark.parametrize("changes,expected", [
    ({"mode": "public", "public_host": ""}, "STRAZH_PUBLIC_HOST"),
    ({"mode": "public", "public_host": HOST, "owner_password": "short"}, "короче 12"),
    ({"mode": "local", "host": "0.0.0.0"}, "только 127.0.0.1"),
    ({"mode": "public", "public_host": HOST, "owner_password": "", "telegram_token": "1:x", "telegram_chat_id": 5,
      "telegram_allowed_users": frozenset({5})}, "вместе с входом владельца"),
])
def test_unsafe_configuration_refuses_to_start(settings, monkeypatch, changes, expected):
    monkeypatch.delenv("STRAZH_LOCAL_CONTAINER", raising=False)
    assert any(expected in p for p in problems(replace(settings, **changes)))


def test_safe_public_configuration_starts(public):
    assert problems(public) == []
