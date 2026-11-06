"""Настройки из окружения и `.env`. Секреты только здесь, в браузер и журнал они не попадают."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _ids(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(" ", "").split(",") if x)


@dataclass(frozen=True)
class Settings:
    db_path: str
    host: str
    port: int
    mode: str                    # local — один владелец на этом компьютере; public — сайт с комнатами посетителей
    owner_password: str          # вход владельца на публичном сайте
    secret_key: str              # подпись cookie владельца
    public_host: str             # домен публичного сайта, например strazh.onrender.com
    allowed_hosts: str
    max_rooms: int
    telegram_mode: str           # polling или webhook
    telegram_webhook_secret: str
    llm_hourly_limit: int        # запросов к модели в час на весь сайт
    commission_bps: Decimal
    exec_delay_ms: int
    start_qty: int
    buy_price: Decimal
    start_cash: Decimal
    default_ttl_sec: int
    telegram_token: str
    telegram_chat_id: int | None
    telegram_allowed_users: frozenset[int]
    telegram_proxy: str
    llm_api_key: str
    llm_model: str

    @classmethod
    def load(cls, env_file: Path | None = ROOT / ".env") -> "Settings":
        if env_file:
            _load_env(env_file)
        e = os.environ
        chat = e.get("TELEGRAM_CHAT_ID", "").strip()
        return cls(db_path=e.get("STRAZH_DB", str(ROOT / "data" / "strazh.db")),
                   host=e.get("STRAZH_HOST", "127.0.0.1"), port=int(e.get("STRAZH_PORT") or e.get("PORT") or "8000"),
                   mode=e.get("STRAZH_MODE", "local").strip().lower(), owner_password=e.get("STRAZH_OWNER_PASSWORD", ""),
                   secret_key=e.get("STRAZH_SECRET_KEY", ""), public_host=(e.get("STRAZH_PUBLIC_HOST") or e.get("RENDER_EXTERNAL_HOSTNAME") or "").strip().lower(),
                   allowed_hosts=e.get("STRAZH_ALLOWED_HOSTS", ""), max_rooms=int(e.get("STRAZH_MAX_ROOMS", "50")),
                   telegram_mode=e.get("TELEGRAM_MODE", "polling").strip().lower(),
                   telegram_webhook_secret=e.get("TELEGRAM_WEBHOOK_SECRET", ""),
                   llm_hourly_limit=int(e.get("STRAZH_LLM_HOURLY_LIMIT", "300")),
                   commission_bps=Decimal(e.get("STRAZH_COMMISSION_BPS", "5")),
                   exec_delay_ms=int(e.get("STRAZH_EXEC_DELAY_MS", "300")),
                   start_qty=int(e.get("STRAZH_START_QTY", "100")), buy_price=Decimal(e.get("STRAZH_BUY_PRICE", "1000")),
                   start_cash=Decimal(e.get("STRAZH_START_CASH", "0")),
                   default_ttl_sec=int(e.get("STRAZH_DEFAULT_TTL_SEC", "60")),
                   telegram_token=e.get("TELEGRAM_BOT_TOKEN", ""), telegram_chat_id=int(chat) if chat else None,
                   telegram_allowed_users=_ids(e.get("TELEGRAM_ALLOWED_USERS", "")),
                   telegram_proxy=e.get("TELEGRAM_PROXY", ""),
                   llm_api_key=e.get("ANTHROPIC_API_KEY", ""), llm_model=e.get("LLM_MODEL", "claude-opus-5-5"))

    @property
    def public(self) -> bool:
        return self.mode == "public"

    @property
    def base_url(self) -> str:
        return f"https://{self.public_host}" if self.public_host else ""

    @property
    def telegram_hook_token(self) -> str:
        """Telegram принимает в secret_token только A-Z, a-z, 0-9, _ и -, а Render генерирует секрет с + / =."""
        return hashlib.sha256(self.telegram_webhook_secret.encode()).hexdigest()

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id and self.telegram_allowed_users)
