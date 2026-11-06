"""Прогон сценариев А–Д без сервера и браузера: таблица для проверки. Запуск: python scripts/run_scenarios.py"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.models import ConditionKind, QtyMode, Reference, RuleSpec  # noqa: E402
from app.money import fmt_rub  # noqa: E402
from app.sim.runner import CLIENT, Simulation  # noqa: E402
from app.sim.scenarios import SCENARIOS  # noqa: E402

SPEC = RuleSpec("PRMT", ConditionKind.DROP_PCT, Decimal(10), Reference.BUY_PRICE, QtyMode.ALL, None, Decimal(850), 60)


def run(key: str, fleet: int = 0, jitter: int = 0, seed: int = 42):
    db = Database(":memory:")
    settings = Settings.load(None)
    db.create_rule(SPEC, "scenario-script")
    sim = Simulation(settings, db)
    sim.fleet_config.jitter_ms, sim.fleet_config.seed = jitter, seed
    sim.start_scenario(key, fleet_size=fleet)
    while sim.status == "running":
        sim.advance(500)
    return sim


def main() -> None:
    print("Правило: падение 10% от цены покупки, продать всё, не дешевле 850 ₽, срок заявки 60 с учебного времени\n")
    print("| Сценарий | Сработало | Продано | Средняя | Остаток | Цена оценки | С ботом | Без бота | Разница |")
    print("|---|---|---|---|---|---|---|---|---|")
    for key, scenario in SCENARIOS.items():
        sim = run(key, fleet=0 if key != "crowd" else 10)
        r = sim.report()
        runtime = next(iter(sim.bots[CLIENT].runtimes.values()))
        avg = fmt_rub(r.avg_price) if r.avg_price else "—"
        print(f"| {scenario.title} | {fmt_rub(runtime.trigger_price)} | {r.sold_qty} | {avg} | {r.remaining_qty} | {fmt_rub(r.mark_price)} | "
              f"{fmt_rub(r.with_bot)} | {fmt_rub(r.hold_value)} | {fmt_rub(r.difference)} |")
    print("\nОбщий стакан, сценарий Д\n")
    print("| Ботов всего | Хотели продать | Продано | Продали всё | Не продали ничего |")
    print("|---|---|---|---|---|")
    for size in (1, 10, 100):
        f = run("crowd", fleet=size).fleet_report()
        full = sum(r["sold"] == 100 for r in f["rows"])
        zero = sum(r["sold"] == 0 for r in f["rows"])
        print(f"| {f['participants']} | {f['total_wanted']} | {f['total_sold']} | {full} | {zero} |")
    print("\nСлучайная задержка 0–500 мс, 10 ботов: другой seed меняет победителей, но не объём спроса\n")
    print("| Seed | Продано всего | Первые в очереди |")
    print("|---|---|---|")
    for seed in (1, 2, 3):
        f = run("crowd", fleet=10, jitter=500, seed=seed).fleet_report()
        print(f"| {seed} | {f['total_sold']} | {', '.join(a['account'] for a in f['arrivals'][:3])} |")


if __name__ == "__main__":
    main()
