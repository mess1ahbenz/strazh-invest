"""HTTP-сервер: панель, встроенный чат, вход владельца и комнаты посетителей.

Локальный режим: одна комната владельца, как раньше. Публичный режим (сайт): каждый посетитель получает свою комнату
в памяти — свой рынок, бот, правила и чат, без Telegram. Владелец входит паролем и работает в основной комнате с базой и
Telegram. Чужую комнату не видно ни через API, ни через уведомления.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agent import texts
from .agent.dialog import ChatService
from .agent.llm import LLMClient
from .agent.notifier import TEST_CHAT, Notifier
from .agent.telegram import TelegramService
from .config import ROOT, Settings
from .db import Database
from .sim.runner import CLIENT, INSTRUMENT, Simulation
from .sim.scenarios import SCENARIOS

log = logging.getLogger(__name__)

WEB = ROOT / "web"
LOOP_SEC = 0.05
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver"}
ROOM_COOKIE, OWNER_COOKIE = "strazh_room", "strazh_owner"
ROOM_IDLE_SEC = 20 * 60          # комната посетителя освобождается после простоя
ACTIVE_SEC = 15                  # время идёт только в комнатах с открытой страницей
OWNER_SESSION_SEC = 12 * 3600
MAX_BODY = 16 * 1024
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
# Адрес посетителя берётся из X-Forwarded-For и подделывается, поэтому лимиты по адресу только вежливые:
# от перебора пароля защищает общий лимит "login_all", от захвата комнат — вытеснение неоткрытых комнат.
# Лимит комнат с адреса мягкий: жюри на площадке выходит в интернет через один адрес.
LIMITS = {"control": (20, 5.0), "chat": (20, 60.0), "start": (1, 2.0), "login": (5, 60.0), "login_all": (30, 60.0),
          "room": (30, 600.0)}


class BodyLimit:
    """Читает тело до приложения и обрывает сверх лимита: Content-Length можно не прислать и передать тело кусками."""

    def __init__(self, app: Any, limit: int) -> None:
        self.app, self.limit = app, limit

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return
        body, more = b"", True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                return                                   # клиент ушёл
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > self.limit:
                await JSONResponse({"detail": "Слишком большой запрос"}, status_code=413)(scope, receive, send)
                return
        sent = False

        async def replay() -> dict:
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)


class BudgetedLLM:
    """Общий для сайта бюджет запросов к модели. Сверх бюджета агент честно переходит на шаблоны."""

    def __init__(self, inner: LLMClient, hourly: int) -> None:
        self.inner, self.hourly = inner, hourly
        self.calls: deque[float] = deque()

    def _take(self) -> bool:
        now = time.monotonic()
        while self.calls and now - self.calls[0] > 3600:
            self.calls.popleft()
        if len(self.calls) >= self.hourly:
            return False
        self.calls.append(now)
        return True

    @property
    def available(self) -> bool:
        return self.inner.available

    @property
    def model(self) -> str:
        return self.inner.model

    @property
    def last_error(self) -> str:
        return self.inner.last_error

    async def parse(self, text: str, context: str = ""):
        from .agent.llm import LLMUnavailable
        if not self._take():
            raise LLMUnavailable("исчерпан часовой лимит запросов к модели")
        return await self.inner.parse(text, context)

    async def explain(self, facts: dict[str, Any], fallback: str) -> str:
        return await self.inner.explain(facts, fallback) if self._take() else fallback


class Platform:
    """Одна комната: свой рынок, бот, чат, база. Telegram и модель необязательны: движок работает и без них."""

    def __init__(self, settings: Settings, llm_client: Any = None, llm: Any = None, room_id: str = "owner") -> None:
        self.settings, self.room_id = settings, room_id
        self.db = Database(settings.db_path)
        self.notifier = Notifier(self.db, settings.telegram_enabled)
        self.sim = Simulation(settings, self.db, self.notifier.on_event)
        self.notifier.sim = self.sim
        self.llm = llm or LLMClient(settings.llm_api_key, settings.llm_model, llm_client)
        self.chat = ChatService(self.sim, self.db, self.notifier, self.llm, settings.default_ttl_sec)
        self.telegram = TelegramService(settings, self.db, self.chat) if settings.telegram_enabled else None
        self.engine_error = ""
        self.last_seen = time.monotonic()
        self._carry = 0.0

    def step(self, elapsed_sec: float) -> None:
        """Учебное время идёт за реальным с заданной скоростью. Ошибка шага не останавливает сайт."""
        self._carry += elapsed_sec * 1000 * self.sim.speed
        step = int(self._carry)
        self._carry -= step
        if self.sim.paused or step <= 0 or self.sim.status in ("finished", "failed"):
            return                               # после сценария часы стоят: график и итог не уезжают
        try:
            self.sim.advance(step)
            self.sim.markers[:] = self.sim.markers[-500:]
        except Exception as exc:  # noqa: BLE001
            self.engine_error = f"{type(exc).__name__}: {exc}"
            log.exception("Сбой шага симуляции в комнате %s", self.room_id)

    def state(self, journal_limit: int = 150) -> dict[str, Any]:
        sim, ex, broker = self.sim, self.sim.exchange, self.sim.broker
        portfolio = broker.get_positions(CLIENT)
        ticker = INSTRUMENT.ticker
        snapshot = ex.snapshot()
        bot = sim.bots[CLIENT]
        report = sim.report()
        settings = self.settings
        runtimes = [{"rule_id": r.rule.id, "version": r.rule.version, "state": r.state,
                     "state_title": bot.state_title(r), "description": bot.describe(r),
                     "threshold": str(bot.threshold(r)), "floor": _s(r.rule.spec.floor), "peak": _s(r.peak),
                     "sold": r.sold_qty, "target": r.target_qty, "remaining": r.remaining, "open_order": r.open_cid,
                     "stop_reason": r.stop_reason, "decision_us": r.decision_us} for r in bot.runtimes.values()]
        rules = [{"id": r.id, "version": r.version, "status": r.status,
                  "lines": texts.rule_lines(r.spec, settings.buy_price, settings.start_qty),
                  "history": [{"version": v["version"], "confirmed_by": v["confirmed_by"], "spec": json.loads(v["spec"])}
                              for v in self.db.rule_versions(r.id)]} for r in self.db.rules()]
        journal = [row for row in self.db.journal(sim.run_id, journal_limit * 3) if row["account"] == CLIENT][:journal_limit]
        return {
            "room": {"id": self.room_id if self.room_id == "owner" else "visitor"},
            "run": {"id": sim.run_id, "status": sim.status, "scenario": sim.scenario_key, "now_ms": sim.now, "speed": sim.speed,
                    "exec_delay_ms": sim.exec_delay_ms, "paused": sim.paused, "end_ms": sim.scenario_end},
            "market": {"name": INSTRUMENT.name, "ticker": ticker, "last": str(ex.last), "best_bid": _s(ex.best_bid), "best_ask": _s(ex.best_ask),
                       "halted": ex.halted, "spread": str(ex.params.spread), "demand_scale": str(ex.params.demand_scale),
                       "demand_pool": int(ex.demand_pool), "demand_target": ex.params.demand_target,
                       "bids": [{"price": str(l.price), "qty": l.qty} for l in snapshot.bids],
                       "asks": [{"price": str(l.price), "qty": l.qty} for l in snapshot.asks]},
            "portfolio": {"cash": str(portfolio.cash), "qty": portfolio.positions.get(ticker, 0), "reserved": portfolio.reserved.get(ticker, 0),
                          "value": str(report.remaining_value), "total": str(report.with_bot), "buy_price": str(settings.buy_price)},
            "bot": {"paused": bot.paused, "runtimes": runtimes, "fleet_rule_is_default": sim.fleet_uses_default_rule},
            "rules": rules, "history": sim.history[-600:], "markers": sim.markers[-120:],
            "journal": [{"id": j["id"], "ts": j["ts"], "kind": j["kind"], "text": j["text"]} for j in journal],
            "orders": self.db.orders(sim.run_id, CLIENT), "executions": self.db.executions(sim.run_id, CLIENT),
            "report": report.to_dict(), "fleet": sim.fleet_report() if len(sim.bots) > 1 else None,
            "fleet_config": {"size": sim.fleet_config.size, "jitter_ms": sim.fleet_config.jitter_ms, "seed": sim.fleet_config.seed},
            "scenarios": [{"key": s.key, "title": s.title, "description": s.description, "fleet": s.fleet} for s in SCENARIOS.values()],
            "services": {"llm": {"mode": "llm" if self.llm.available else "templates", "model": settings.llm_model if self.llm.available else None,
                                 "error": self.llm.last_error},
                         "telegram": {"enabled": settings.telegram_enabled, "connected": bool(self.telegram and self.telegram.connected),
                                      "error": self.telegram.last_error if self.telegram else ""},
                         "outbox": self.db.outbox_stats(), "engine_error": self.engine_error or getattr(sim, "failure", ""),
                         "commission_bps": str(settings.commission_bps)},
        }


class Rooms:
    """Комнаты посетителей публичного сайта и основная комната владельца."""

    def __init__(self, settings: Settings, owner: Platform, llm: Any) -> None:
        self.settings, self.owner, self.llm = settings, owner, llm
        self.visitors: dict[str, Platform] = {}

    def visitor(self, token: str) -> Platform | None:
        room = self.visitors.get(token)
        if room is not None:
            room.last_seen = time.monotonic()
        return room

    def has_space(self) -> bool:
        """Освобождает место: сначала комнаты после долгого простоя, затем самую давнюю из тех, что сейчас никто не открыл.
        Пустые комнаты, созданные потоком запросов, так уходят через ACTIVE_SEC и не закрывают сайт для людей."""
        self.evict()
        if len(self.visitors) < self.settings.max_rooms:
            return True
        now = time.monotonic()
        idle = [(r.last_seen, t) for t, r in self.visitors.items() if now - r.last_seen > ACTIVE_SEC]
        if not idle:
            return False
        self.visitors.pop(min(idle)[1]).db.conn.close()
        return True

    def create(self) -> tuple[str, Platform] | None:
        if not self.has_space():
            return None
        token = secrets.token_urlsafe(24)
        visitor_settings = replace(self.settings, db_path=":memory:", telegram_token="", telegram_chat_id=None,
                                   telegram_allowed_users=frozenset())
        room = Platform(visitor_settings, llm=self.llm, room_id=hashlib.sha256(token.encode()).hexdigest()[:10])
        self.visitors[token] = room
        return token, room

    def evict(self) -> None:
        now = time.monotonic()
        for token in [t for t, r in self.visitors.items() if now - r.last_seen > ROOM_IDLE_SEC]:
            room = self.visitors.pop(token)
            room.db.conn.close()

    def active(self) -> list[Platform]:
        now = time.monotonic()
        return [self.owner] + [r for r in self.visitors.values() if now - r.last_seen <= ACTIVE_SEC]


class RateLimiter:
    def __init__(self) -> None:
        self.hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(self, kind: str, key: str) -> bool:
        limit, window = LIMITS[kind]
        now = time.monotonic()
        if len(self.hits) > 10_000:          # поддельные адреса не должны раздувать память
            longest = max(w for _, w in LIMITS.values())
            for k in [k for k, h in self.hits.items() if not h or now - h[-1] > longest]:
                del self.hits[k]
        hits = self.hits[(kind, key)]
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _number(body: dict[str, Any], key: str, low: Decimal, high: Decimal) -> Decimal:
    try:
        value = Decimal(str(body[key]))
    except (KeyError, InvalidOperation, ValueError):
        raise HTTPException(422, f"Нужно число: {key}") from None
    if not value.is_finite() or not low <= value <= high:
        raise HTTPException(422, f"{key}: допустимо от {low} до {high}")
    return value


def _sign(secret: str, value: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def create_app(settings: Settings | None = None, platform: Platform | None = None, drive: bool = True) -> FastAPI:
    settings = settings or Settings.load()
    if settings.public and not settings.owner_password:
        log.warning("Публичный режим без STRAZH_OWNER_PASSWORD: вход владельца выключен")
    secret_key = settings.secret_key or secrets.token_hex(32)
    shared_llm = BudgetedLLM(LLMClient(settings.llm_api_key, settings.llm_model), settings.llm_hourly_limit)
    owner = platform or Platform(settings, llm=shared_llm)
    rooms = Rooms(settings, owner, shared_llm)
    limiter = RateLimiter()
    webhook = settings.public and settings.telegram_mode == "webhook" and owner.telegram is not None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if drive:
            tasks.append(asyncio.create_task(_drive()))
            if owner.telegram:
                tasks.append(asyncio.create_task(owner.telegram.send_forever()))
                if webhook:
                    try:
                        await owner.telegram.set_webhook(f"{settings.base_url}/telegram/hook", settings.telegram_hook_token)
                    except Exception as exc:  # noqa: BLE001
                        owner.telegram.last_error = f"вебхук не установлен: {type(exc).__name__}"
                        log.warning("Вебхук Telegram не установлен: %s", type(exc).__name__)
                else:
                    tasks.append(asyncio.create_task(owner.telegram.poll_forever()))
        yield
        for task in tasks:
            task.cancel()
        if owner.telegram:
            await owner.telegram.http.aclose()

    async def _drive() -> None:
        last = time.monotonic()
        spent = 0.0
        while True:
            await asyncio.sleep(max(LOOP_SEC, spent))      # не больше половины процессора: на бесплатном тарифе его 0,1
            now = time.monotonic()
            elapsed, last = now - last, now
            for room in rooms.active():
                room.step(elapsed)
            spent = time.monotonic() - now

    app = FastAPI(title="Страж инвестиций", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.platform = owner
    app.state.rooms = rooms
    allowed_hosts = LOCAL_HOSTS | {h for h in settings.allowed_hosts.split(",") if h}
    if settings.public_host:
        allowed_hosts.add(settings.public_host)

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "?"

    def is_owner(request: Request) -> bool:
        if not settings.public:
            return True                          # локальный режим: один владелец на своём компьютере
        raw = request.cookies.get(OWNER_COOKIE, "")
        expires, _, signature = raw.partition(".")
        return bool(expires.isdigit() and int(expires) > time.time()
                    and hmac.compare_digest(signature.encode("utf-8", "replace"), _sign(secret_key, f"owner:{expires}").encode()))

    def room_of(request: Request) -> Platform:
        if is_owner(request):
            return owner
        room = rooms.visitor(request.cookies.get(ROOM_COOKIE, ""))
        if room is None:
            raise HTTPException(409, "Комната устарела. Обновите страницу.")
        return room

    def limit(kind: str, key: str) -> None:
        if not limiter.allow(kind, key):
            raise HTTPException(429, "Слишком часто. Подождите немного.")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Чужой сайт и чужой адрес не дотянутся до панели; тело запроса ограничено; ответы с заголовками защиты."""
        host = request.headers.get("host", "").lower().rsplit(":", 1)[0]
        # проверка хостинга приходит на внутренний адрес; /healthz ничего не раскрывает и ничего не меняет
        if host not in allowed_hosts and request.url.path != "/healthz":
            return JSONResponse({"detail": "Адрес не разрешён"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method != "GET" and origin and origin.split("://", 1)[-1].rsplit(":", 1)[0].lower() not in allowed_hosts:
            return JSONResponse({"detail": "Источник не разрешён"}, status_code=403)
        length = request.headers.get("content-length") or "0"
        if not length.isdigit() or int(length) > MAX_BODY:
            return JSONResponse({"detail": "Слишком большой запрос"}, status_code=413)
        new_room = None
        if settings.public and request.url.path in ("/", "/api/session") and not is_owner(request) \
                and rooms.visitor(request.cookies.get(ROOM_COOKIE, "")) is None:
            # Без комнаты страница всё равно открывается: владелец должен суметь войти, посетитель — увидеть причину
            if not rooms.has_space():
                request.state.no_room = "Все учебные комнаты заняты. Страница сама попробует снова через полминуты."
            elif not limiter.allow("room", client_ip(request)):
                request.state.no_room = "Слишком много новых комнат с вашего адреса. Попробуйте через несколько минут."
            else:
                new_room = rooms.create()
                cookies = [v for k, v in request.scope["headers"] if k == b"cookie"]
                kept = "; ".join(p for c in cookies for p in c.decode("latin-1").split("; ") if not p.startswith(ROOM_COOKIE + "="))
                request.scope["headers"] = [(k, v) for k, v in request.scope["headers"] if k != b"cookie"] \
                    + [(b"cookie", f"{kept}; {ROOM_COOKIE}={new_room[0]}".lstrip("; ").encode("latin-1"))]
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 — подробности остаются в журнале сервера
            log.exception("Необработанная ошибка %s", request.url.path)
            response = JSONResponse({"detail": "Внутренняя ошибка"}, status_code=500)
        if new_room is not None:
            response.set_cookie(ROOM_COOKIE, new_room[0], httponly=True, secure=settings.public, samesite="lax",
                                max_age=ROOM_IDLE_SEC * 6)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if settings.public:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    @app.get("/healthz")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/api/session")
    async def session(request: Request) -> dict[str, Any]:
        own = is_owner(request)
        reason = getattr(request.state, "no_room", None)
        if reason:
            return {"mode": "full", "room": None, "public": True, "telegram": False, "detail": reason,
                    "owner_login": bool(settings.owner_password)}
        room = room_of(request)
        return {"mode": "owner" if own else "visitor", "room": room.room_id if own else "visitor", "public": settings.public,
                "telegram": bool(own and owner.telegram), "owner_login": settings.public and bool(settings.owner_password)}

    @app.post("/api/login")
    async def login(request: Request, response: Response, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if not settings.public:
            return {"ok": True}
        if not settings.owner_password:
            raise HTTPException(404, "Вход владельца выключен")
        limit("login", client_ip(request))
        limit("login_all", "*")
        if not hmac.compare_digest(str(body.get("password", "")).encode(), settings.owner_password.encode()):
            raise HTTPException(401, "Неверный пароль")
        expires = str(int(time.time()) + OWNER_SESSION_SEC)
        response.set_cookie(OWNER_COOKIE, f"{expires}.{_sign(secret_key, f'owner:{expires}')}", httponly=True, secure=True,
                            samesite="strict", max_age=OWNER_SESSION_SEC)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(response: Response) -> dict[str, Any]:
        response.delete_cookie(OWNER_COOKIE)
        return {"ok": True}

    @app.get("/api/state")
    async def state(request: Request) -> dict[str, Any]:
        return room_of(request).state()

    @app.post("/api/market/{action}")
    async def market(action: str, request: Request, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        room = room_of(request)
        limit("control", room.room_id)
        params: dict[str, Any] = {}
        if action in ("price", "gradual"):
            params["price"] = _number(body, "price", Decimal("0.01"), Decimal(100_000)).quantize(Decimal("0.01"))
            if action == "gradual":
                params["seconds"] = int(_number(body, "seconds", Decimal(1), Decimal(600)))
        elif action == "crash":
            params["percent"] = _number(body, "percent", Decimal(1), Decimal(99))
        elif action == "demand":
            params["scale"] = _number(body, "scale", Decimal(0), Decimal(2))
        elif action == "spread":
            params["spread"] = _number(body, "spread", Decimal(0), Decimal(100))
        elif action not in ("halt", "resume"):
            raise HTTPException(404, "Неизвестное действие")
        try:
            room.sim.command(action, **params)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"ok": True}

    @app.post("/api/sim/{action}")
    async def simulation(action: str, request: Request, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        room = room_of(request)
        sim = room.sim
        limit("control", room.room_id)
        if action == "start":
            limit("start", room.room_id)
            if body.get("scenario") not in SCENARIOS:
                raise HTTPException(422, "Неизвестный сценарий")
            try:
                if "fleet" in body:
                    sim.start_scenario(body["scenario"], fleet_size=int(_number(body, "fleet", Decimal(0), Decimal(200))))
                else:
                    sim.start_scenario(body["scenario"])
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        elif action == "pause":
            sim.paused = True
        elif action == "resume":
            sim.paused = False
        elif action == "reset":
            limit("start", room.room_id)
            sim.reset()
        elif action == "speed":
            sim.speed = float(_number(body, "speed", Decimal("0.1"), Decimal(50)))
        elif action == "delay":
            sim.exec_delay_ms = int(_number(body, "ms", Decimal(0), Decimal(60_000)))
        elif action == "fleet":
            config = sim.fleet_config
            config.size = int(_number(body, "size", Decimal(0), Decimal(200)))
            if "jitter_ms" in body:
                config.jitter_ms = int(_number(body, "jitter_ms", Decimal(0), Decimal(5000)))
            if "seed" in body:
                config.seed = int(_number(body, "seed", Decimal(0), Decimal(2 ** 31)))
        else:
            raise HTTPException(404, "Неизвестное действие")
        return {"ok": True}

    @app.get("/api/chat")
    async def chat_read(request: Request, after: int = 0) -> list[dict[str, Any]]:
        return room_of(request).db.chat(TEST_CHAT, max(after, 0))

    @app.post("/api/chat")
    async def chat_send(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        room = room_of(request)
        limit("chat", room.room_id)
        text = str(body.get("text", "")).strip()
        if not text:
            raise HTTPException(422, "Пустое сообщение")
        if len(text) > 1000:
            raise HTTPException(422, "Сообщение длиннее 1000 символов")
        await room.chat.handle_text(TEST_CHAT, text)
        return {"ok": True}

    @app.post("/api/chat/callback")
    async def chat_callback(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        room = room_of(request)
        limit("chat", room.room_id)
        data = str(body.get("data", ""))[:120]
        await room.chat.handle_callback(TEST_CHAT, data)
        return {"ok": True}

    @app.post("/telegram/hook")
    async def telegram_hook(request: Request) -> dict[str, Any]:
        """Telegram будит спящий бесплатный сервер этим запросом. Без секрета запрос не принимается."""
        given = request.headers.get("x-telegram-bot-api-secret-token", "").encode("utf-8", "replace")
        if not webhook or not hmac.compare_digest(given, settings.telegram_hook_token.encode()):
            raise HTTPException(403, "Нет доступа")
        try:
            update = await request.json()
        except ValueError:
            raise HTTPException(422, "Не JSON") from None
        await owner.telegram.handle_webhook(update)
        return {"ok": True}

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    app.add_middleware(BodyLimit, limit=MAX_BODY)
    return app
