"""Telegram Bot API: опрос обновлений или вебхук, и отправка очереди. Ни одна из этих задач не участвует в торговле.

- Нажатие кнопки сначала обрабатывается, потом подтверждается (`answerCallbackQuery`); ошибка подтверждения
  (например, «query is too old») нажатие не отменяет.
- Ошибка обработчика не теряет сообщение молча: клиент получает «не получилось», трассировка — в журнал.
- «Подключён» значит: последний опрос (или установка вебхука) прошёл и последняя отправка не упала.
- Постоянные ошибки 4xx повторяются не больше `MAX_ATTEMPTS` раз, затем сообщение получает статус `failed`
  с причиной. На 429 выдерживается `retry_after`.
- Работаем только в личном чате. Команды с упоминанием другого бота (`/pause@OtherBot`) игнорируются.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from ..config import Settings
from ..db import Database
from .dialog import ChatService

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
RETRY_SEC = 5.0
MAX_ATTEMPTS = 3              # для постоянных ошибок 4xx
FAILED_TEXT = "Не получилось обработать сообщение. Правила не изменились. Попробуйте ещё раз."
GROUP_TYPES = {"group", "supergroup", "channel"}


class TelegramError(RuntimeError):
    def __init__(self, method: str, status: int, description: str, retry_after: float | None = None) -> None:
        super().__init__(f"{method}: {description}")
        self.method, self.status, self.description, self.retry_after = method, status, description, retry_after

    @property
    def permanent(self) -> bool:
        return 400 <= self.status < 500 and self.status != 429


class TelegramService:
    def __init__(self, settings: Settings, db: Database, chat: ChatService, *, api_base: str = API,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings, self.db, self.chat = settings, db, chat
        self._url = f"{api_base}/bot{settings.telegram_token}"
        kw: dict[str, Any] = {"timeout": httpx.Timeout(35.0, connect=5.0), "trust_env": False}
        if transport is not None:
            kw["transport"] = transport
        elif settings.telegram_proxy:
            kw["proxy"] = settings.telegram_proxy
        self.http = httpx.AsyncClient(**kw)
        self.offset: int | None = None
        self.last_error = ""
        self.receive_ok = False       # последний getUpdates или setWebhook прошёл
        self.send_ok: bool | None = None   # None — ещё ничего не отправляли
        self.username: str | None = None
        self.mode = "polling"
        self._pause_until = 0.0       # глобальная пауза отправки после 429

    def __repr__(self) -> str:
        return "TelegramService()"          # адрес содержит токен

    @property
    def connected(self) -> bool:
        return self.receive_ok and self.send_ok is not False

    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        response = await self.http.post(f"{self._url}/{method}", json=payload)
        try:
            data = response.json()
        except ValueError:
            raise TelegramError(method, response.status_code, f"ответ не JSON ({response.status_code})") from None
        if not isinstance(data, dict) or not data.get("ok"):
            data = data if isinstance(data, dict) else {}
            retry = (data.get("parameters") or {}).get("retry_after")
            raise TelegramError(method, int(data.get("error_code") or response.status_code),
                                str(data.get("description", response.status_code)),
                                float(retry) if isinstance(retry, (int, float)) else None)
        return data["result"]

    def _allowed(self, user_id: int | None, chat_id: int | None) -> bool:
        return user_id in self.settings.telegram_allowed_users and chat_id == self.settings.telegram_chat_id

    async def _whoami(self) -> str | None:
        if self.username is None:
            try:
                me = await self._call("getMe", {})
                self.username = str(me.get("username") or "") if isinstance(me, dict) else ""
            except Exception as exc:  # noqa: BLE001
                log.warning("getMe не ответил: %s", type(exc).__name__)
        return self.username

    async def _addressed_elsewhere(self, text: str) -> bool:
        """`/pause@OtherBot` — команда другому боту. Если своё имя узнать не удалось, суффикс тоже не исполняем."""
        head = text.split()[0] if text.split() else ""
        if not head.startswith("/") or "@" not in head:
            return False
        mine = await self._whoami()
        return not mine or head.split("@", 1)[1].lower() != mine.lower()

    async def handle_update(self, update: dict[str, Any]) -> None:
        update_id = update.get("update_id") if isinstance(update, dict) else None
        if not isinstance(update_id, int):
            log.warning("Обновление Telegram без update_id отклонено")
            return
        self.offset = max(self.offset or 0, update_id + 1)
        if self.db.seen_update(update_id):
            return
        message, callback = update.get("message"), update.get("callback_query")
        origin = message or (callback or {}).get("message") or {}
        user = (message or callback or {}).get("from", {}).get("id")
        chat = origin.get("chat", {})
        chat_id = chat.get("id")
        if chat.get("type") in GROUP_TYPES:
            log.info("Сообщение из группового чата проигнорировано")
            return
        if not self._allowed(user, chat_id):
            log.warning("Сообщение от неразрешённого пользователя отклонено")
            return
        try:
            if message and message.get("text"):
                if await self._addressed_elsewhere(message["text"]):
                    return
                await self.chat.handle_text(str(chat_id), message["text"], f"telegram:{user}")
            elif callback:
                await self.chat.handle_callback(str(chat_id), str(callback.get("data", "")), f"telegram:{user}")
        except Exception:  # noqa: BLE001 — сообщение не теряется молча
            log.exception("Сбой обработки обновления Telegram")
            self.chat.notifier.send(f"tg-failed:{update_id}", FAILED_TEXT, chat=str(chat_id))
        finally:
            if callback and callback.get("id"):
                try:
                    await self._call("answerCallbackQuery", {"callback_query_id": callback["id"]})
                except Exception as exc:  # noqa: BLE001 — старая кнопка («query is too old») нажатие не отменяет
                    log.info("answerCallbackQuery: %s", type(exc).__name__)

    async def handle_webhook(self, update: dict[str, Any]) -> None:
        """Тот же путь, что и при опросе. Ошибки наружу не выходят: Telegram не должен повторять запрос бесконечно."""
        self.mode = "webhook"
        self.receive_ok = True
        try:
            await self.handle_update(update)
        except Exception:  # noqa: BLE001
            log.exception("Сбой обработки вебхука Telegram")

    async def set_webhook(self, url: str, secret: str) -> None:
        """Включить вебхук. Telegram будет присылать заголовок X-Telegram-Bot-Api-Secret-Token с этим секретом."""
        await self._whoami()
        await self._call("setWebhook", {"url": url, "secret_token": secret,
                                        "allowed_updates": ["message", "callback_query"]})
        self.mode, self.receive_ok, self.last_error = "webhook", True, ""

    async def delete_webhook(self) -> None:
        await self._call("deleteWebhook", {"drop_pending_updates": False})
        self.mode, self.receive_ok = "polling", False

    async def poll_forever(self) -> None:
        while True:
            try:
                payload: dict[str, Any] = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
                if self.offset is not None:
                    payload["offset"] = self.offset
                updates = await self._call("getUpdates", payload)
                self.receive_ok = True
                if self.send_ok is not False:
                    self.last_error = ""
                for update in updates:
                    await self.handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — недоступность Telegram не должна влиять на торговлю
                self.receive_ok, self.last_error = False, type(exc).__name__
                log.warning("Telegram недоступен: %s", type(exc).__name__)
                await asyncio.sleep(RETRY_SEC)

    async def send_pending(self) -> int:
        sent = 0
        if time.time() < self._pause_until:
            return 0
        for row in self.db.pending_outbox():
            payload: dict[str, Any] = {"chat_id": self.settings.telegram_chat_id, "text": row["text"]}
            buttons = json.loads(row["buttons"])
            if buttons:
                payload["reply_markup"] = {"inline_keyboard": [[{"text": b["text"], "callback_data": b["data"]} for b in rows]
                                                               for rows in buttons]}
            try:
                await self._call("sendMessage", payload)
            except TelegramError as exc:
                self.send_ok, self.last_error = False, f"sendMessage {exc.status}: {exc.description}"[:200]
                if exc.status == 429:
                    wait = exc.retry_after or RETRY_SEC
                    self._pause_until = time.time() + wait
                    self.db.mark_failed(row["id"], self.last_error, wait)
                    break                     # ограничение на весь бот: остальное подождёт
                if exc.permanent and row["attempts"] + 1 >= MAX_ATTEMPTS:
                    self.db.mark_dead(row["id"], self.last_error)
                    log.warning("Сообщение %s не доставлено окончательно: %s", row["id"], exc.status)
                else:
                    self.db.mark_failed(row["id"], self.last_error, min(2 ** row["attempts"], 60))
                continue
            except Exception as exc:  # noqa: BLE001 — сеть: запись останется в очереди, торговля об этом не узнает
                self.send_ok, self.last_error = False, type(exc).__name__
                self.db.mark_failed(row["id"], type(exc).__name__, min(2 ** row["attempts"], 60))
                continue
            self.db.mark_sent(row["id"])
            self.send_ok = True
            sent += 1
        return sent

    async def send_forever(self) -> None:
        while True:
            await self.send_pending()
            await asyncio.sleep(1.0)
