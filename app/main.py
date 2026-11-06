"""Запуск: `python -m app.main`. Локальный режим слушает только этот компьютер, публичный — сайт за прокси."""
from __future__ import annotations

import logging
import os
import sys

import uvicorn

from .api import create_app
from .config import Settings

LOOPBACK = ("127.0.0.1", "localhost", "::1")


def problems(settings: Settings) -> list[str]:
    """Что не даёт безопасно запуститься. Пустой список — можно."""
    out = []
    if settings.mode not in ("local", "public"):
        out.append("STRAZH_MODE должен быть local или public")
    # В контейнере сервер слушает 0.0.0.0, а наружу порт открыт только на 127.0.0.1 (docker-compose.yml).
    if settings.mode == "local" and settings.host not in LOOPBACK and not os.environ.get("STRAZH_LOCAL_CONTAINER") == "1":
        out.append("Локальный режим слушает только 127.0.0.1. Для сайта используйте STRAZH_MODE=public.")
    if settings.public:
        if not settings.public_host:
            out.append("Для публичного режима нужен STRAZH_PUBLIC_HOST (домен сайта)")
        if settings.owner_password and len(settings.owner_password) < 12:
            out.append("STRAZH_OWNER_PASSWORD короче 12 символов")
        if settings.telegram_enabled and settings.telegram_mode == "webhook" and len(settings.telegram_webhook_secret) < 24:
            out.append("Для вебхука Telegram нужен TELEGRAM_WEBHOOK_SECRET длиной от 24 символов")
        if settings.telegram_enabled and not settings.owner_password:
            out.append("Telegram на публичном сайте только вместе с входом владельца (STRAZH_OWNER_PASSWORD)")
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)      # httpx печатает адрес запроса, а в нём токен Telegram
    settings = Settings.load()
    if found := problems(settings):
        sys.exit("Отказ запуска:\n- " + "\n- ".join(found))
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="warning",
                proxy_headers=settings.public, forwarded_allow_ips="*" if settings.public else None)


if __name__ == "__main__":
    main()
