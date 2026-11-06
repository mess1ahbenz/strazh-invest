"""Публикация сайта на Render из публичного репозитория. Ключи берутся из окружения и .env и не печатаются.

    RENDER_API_KEY=... python scripts/deploy_render.py https://github.com/<user>/<repo>

Создаёт или обновляет веб-сервис, передаёт секреты (пароль владельца генерируется и сохраняется в deploy/owner_password.txt
с правами 600), запускает развёртывание и ждёт, пока сайт ответит.
"""
from __future__ import annotations

import os
import secrets
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.render.com/v1"
NAME = "strazh-invest"
PASSWORD_FILE = ROOT / "deploy" / "owner_password.txt"


def env_file() -> dict[str, str]:
    path = ROOT / ".env"
    if not path.exists():
        return {}
    return {k.strip(): v.strip() for k, v in (l.split("=", 1) for l in path.read_text().splitlines()
                                              if "=" in l and not l.lstrip().startswith("#"))}


def items(data) -> list[dict]:
    """Ответы Render приходят списком объектов-обёрток с курсором."""
    out = []
    for row in data if isinstance(data, list) else data.get("items", []):
        out.append(next((v for k, v in row.items() if k != "cursor" and isinstance(v, dict)), row))
    return out


def owner_password() -> str:
    if PASSWORD_FILE.exists():
        return PASSWORD_FILE.read_text().strip()
    PASSWORD_FILE.parent.mkdir(exist_ok=True)
    password = secrets.token_urlsafe(18)
    PASSWORD_FILE.write_text(password + "\n")
    PASSWORD_FILE.chmod(0o600)
    return password


def main(repo: str) -> None:
    local = env_file()
    key = os.environ.get("RENDER_API_KEY") or local.get("RENDER_API_KEY")
    if not key:
        sys.exit("Нужен RENDER_API_KEY")
    http = httpx.Client(base_url=API, timeout=30, trust_env=False, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})

    def call(method: str, path: str, **kw):
        response = http.request(method, path, **kw)
        if response.status_code >= 400:
            sys.exit(f"Render {method} {path}: HTTP {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else {}

    owners = items(call("GET", "/owners", params={"limit": 20}))
    owner_id = os.environ.get("RENDER_OWNER_ID") or owners[0]["id"]
    print("рабочее пространство:", next((o.get("name") for o in owners if o["id"] == owner_id), owner_id))
    env = {"STRAZH_MODE": "public", "STRAZH_HOST": "0.0.0.0", "STRAZH_SECRET_KEY": secrets.token_hex(32),
           "STRAZH_OWNER_PASSWORD": owner_password(), "TELEGRAM_MODE": "webhook", "TELEGRAM_WEBHOOK_SECRET": secrets.token_urlsafe(32)}
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALLOWED_USERS", "ANTHROPIC_API_KEY", "LLM_MODEL"):
        if value := os.environ.get(name) or local.get(name):
            env[name] = value
    env_vars = [{"key": k, "value": v} for k, v in env.items()]

    found = [s for s in items(call("GET", "/services", params={"name": NAME, "ownerId": owner_id, "limit": 20})) if s.get("name") == NAME]
    if found:
        service = found[0]
        call("PUT", f"/services/{service['id']}/env-vars", json=env_vars)
        call("POST", f"/services/{service['id']}/deploys", json={})
        print("сервис обновлён, развёртывание запущено")
    else:
        created = call("POST", "/services", json={
            "type": "web_service", "name": NAME, "ownerId": owner_id, "repo": repo, "branch": "main",
            "autoDeployTrigger": "commit", "envVars": env_vars,
            "serviceDetails": {"runtime": "docker", "plan": "free", "region": "frankfurt", "healthCheckPath": "/healthz",
                               "envSpecificDetails": {"dockerfilePath": "./Dockerfile", "dockerContext": "."}}})
        service = created.get("service", created)
        print("сервис создан")
    detail = call("GET", f"/services/{service['id']}")
    url = (detail.get("serviceDetails") or {}).get("url") or f"https://{NAME}.onrender.com"
    print("адрес:", url)
    for _ in range(90):
        time.sleep(10)
        try:
            if httpx.get(f"{url}/healthz", timeout=10, trust_env=False).status_code == 200:
                print("сайт отвечает")
                return
        except httpx.HTTPError:
            pass
    sys.exit("Сайт не ответил за 15 минут: смотрите журнал сборки в панели Render")


if __name__ == "__main__":
    main(sys.argv[1])
