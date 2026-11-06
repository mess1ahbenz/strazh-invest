"""Обязательные сценарии. Расписание хранится здесь и уходит в очередь симуляции: бот его не видит."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

S = 1000      # секунда учебного времени


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    description: str
    market: dict[str, Any]
    steps: tuple[tuple[int, str, dict[str, Any]], ...]
    duration_ms: int
    fleet: int = 0


def _fall(start: int, frm: int, to: int, step: int, every: int) -> tuple[tuple[int, str, dict[str, Any]], ...]:
    prices = range(frm - step, to - 1, -step)
    return tuple((start + every * i, "price", {"price": p}) for i, p in enumerate(prices))


SCENARIOS: dict[str, Scenario] = {s.key: s for s in (
    Scenario("gradual", "А. Постепенное падение", "Цена шаг за шагом снижается с 1 000 до 500 ₽. Покупатели есть, "
             "но их объём ограничен и восстанавливается медленно.",
             {"level_qty": 15, "refill_per_sec": Decimal("0.03")}, _fall(2 * S, 1000, 500, 10, S), 60 * S),
    Scenario("crash", "Б. Мгновенный обвал", "Цена одним скачком падает с 1 000 до 500 ₽. Покупателей выше 500 ₽ больше нет.",
             {"level_qty": 15, "refill_per_sec": Decimal("0.02")}, ((3 * S, "price", {"price": 500}),), 75 * S),
    Scenario("no_buyers", "В. Исчезновение покупателей", "Правило срабатывает, но спроса хватает только на часть позиции. "
             "Остаток виден отдельно.", {"level_qty": 7, "refill_per_sec": Decimal(0)},
             ((3 * S, "price", {"price": 895}),), 75 * S),
    Scenario("rebound", "Г. Падение с восстановлением", "Цена снижается, бот продаёт, затем цена возвращается выше начальной. "
             "Бот может оказаться хуже удержания.", {"level_qty": 30, "refill_per_sec": Decimal("0.05")},
             _fall(2 * S, 1000, 880, 20, S) + tuple((12 * S + 2 * S * i, "price", {"price": 900 + 25 * i}) for i in range(10)),
             40 * S),
    Scenario("crowd", "Д. Несколько ботов на одном рынке", "Боты с похожими правилами продают в один общий стакан. "
             "Спрос один на всех.", {"level_qty": 40, "refill_per_sec": Decimal("0.01")},
             _fall(2 * S, 1000, 890, 20, S), 80 * S, fleet=10),
)}
