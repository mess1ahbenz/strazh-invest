"""Приёмочный путь против запущенного сервера. Запуск: python scripts/acceptance.py [адрес]"""
from __future__ import annotations

import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
RULE = "Следи за моей позицией. Если цена упадёт на 10% от цены покупки, начни продажу. Не продавай дешевле 850 рублей."
http = httpx.Client(base_url=BASE, timeout=10, trust_env=False)


def post(path: str, **body) -> None:
    http.post(path, json=body).raise_for_status()


def state() -> dict:
    return http.get("/api/state").json()


def wait(check, seconds: float = 30) -> None:
    end = time.time() + seconds
    while time.time() < end:
        if check(state()):
            return
        time.sleep(0.2)
    raise SystemExit("Не дождался нужного состояния")


def main() -> None:
    post("/api/sim/reset")
    post("/api/sim/speed", speed=20)
    for rule in state()["rules"]:
        post("/api/chat/callback", data=f"rule:delete:{rule['id']}")
    after = max([m["id"] for m in http.get("/api/chat").json()] or [0])
    print("1. Сервер отвечает, прогон", state()["run"]["id"])
    post("/api/chat", text=RULE)
    rows = http.get("/api/chat", params={"after": after}).json()
    print("2. Ответ агента:\n" + rows[-1]["text"])
    button = next(b for row in rows[-1]["buttons"] for b in row if b["text"] == "Включить")
    post("/api/chat/callback", data=button["data"])
    print("3. Подтверждено:", state()["rules"][0]["lines"][1])
    post("/api/market/crash", percent=50)
    print("4. Цена обвалена, движок работает сам, ждём истечения заявки")
    wait(lambda s: s["bot"]["runtimes"] and s["bot"]["runtimes"][0]["state"] == "expired")
    s = state()
    print("5. Журнал:")
    for row in reversed(s["journal"][:6]):
        print(f"   {row['ts'] / 1000:7.1f} с  {row['text']}")
    report, order = s["report"], s["orders"][0]
    print(f"   Заявка: {order['status']}, исполнено {order['filled_qty']} из {order['qty']}, сделок {len(s['executions'])}")
    print(f"   Счёт: {report['with_bot']}, без бота: {report['hold_value']}, разница: {report['difference']}")
    messages = [m["text"] for m in http.get("/api/chat", params={"after": after}).json()]
    summary = next(t for t in messages if t.startswith("Сработало правило"))
    print("6. Сообщение:\n" + summary)
    plain = summary.replace("\u00a0", " ")
    assert f"Продано: {report['sold_qty']} из" in plain and f"Осталось: {report['remaining_qty']} акций" in plain
    print("Приёмочный путь пройден: сообщение совпадает с журналом и счётом.")
    post("/api/sim/speed", speed=1)


if __name__ == "__main__":
    main()
