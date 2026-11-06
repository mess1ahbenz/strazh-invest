"""Тексты для клиента. Все числа приходят из движка, здесь они только форматируются.

Между числом и единицей («100 акций», «60 сек.») стоит неразрывный пробел: число не отрывается от единицы.
"""
from __future__ import annotations

from decimal import Decimal

from ..models import ConditionKind, QtyMode, Reference, RuleSpec
from ..money import NBSP, fmt_int, fmt_pct, fmt_rub
from ..report import Report

NAME = "Пример-Тех"
DEMO_NOTE = "Демо-режим без языковой модели: понимаю только ограниченные шаблоны команд."
TEMPLATE_HELP = ("Примеры, которые я разберу:\n"
                 "• Если цена упадёт на 10% от цены покупки, продай всё. Не дешевле 850 рублей\n"
                 "• Продай половину при падении на 15% от максимума, минимальная цена 800\n"
                 "• Если цена упадёт до 900 рублей, продай 50 акций не дешевле 850\n"
                 "• Измени минимальную цену на 800 · Отмена\n"
                 "• Приостанови правило · Возобнови правило · Удали правило")


def shares(n: int) -> str:
    return f"{fmt_int(n)}{NBSP}акций"


def threshold_of(spec: RuleSpec, buy_price: Decimal) -> Decimal | None:
    """Порог, если он известен заранее: для максимума после включения он зависит от будущего максимума."""
    if spec.condition is ConditionKind.PRICE_BELOW:
        return spec.value
    if spec.reference is not Reference.BUY_PRICE:
        return None
    if spec.condition is ConditionKind.DROP_ABS:
        return buy_price - spec.value
    return buy_price * (100 - spec.value) / 100


def condition_line(spec: RuleSpec, buy_price: Decimal) -> str:
    if spec.condition is ConditionKind.PRICE_BELOW:
        return f"начать продажу, когда цена опустится до {fmt_rub(spec.value)}"
    amount = fmt_pct(spec.value) if spec.condition is ConditionKind.DROP_PCT else fmt_rub(spec.value)
    if spec.reference is Reference.BUY_PRICE:
        return (f"начать продажу, когда цена опустится до {fmt_rub(threshold_of(spec, buy_price))}: "
                f"снижение на {amount} от цены покупки {fmt_rub(buy_price)}")
    return f"начать продажу при снижении цены на {amount} от максимума после включения правила"


def qty_line(spec: RuleSpec, position: int) -> str:
    if spec.qty_mode is QtyMode.ALL:
        return f"продать до {shares(position)} (вся доступная позиция)" if position else "продать всю доступную позицию"
    if spec.qty_mode is QtyMode.SHARES:
        return f"продать до {shares(int(spec.qty_value))}"
    return f"продать {fmt_pct(spec.qty_value)} позиции"


def floor_line(spec: RuleSpec, buy_price: Decimal | None = None) -> str:
    if spec.floor is None:
        return "минимальная цена не задана: заявка может исполниться по любой цене покупателя"
    return f"не выставлять продажу дешевле {fmt_rub(spec.floor)}"


def rule_lines(spec: RuleSpec, buy_price: Decimal, position: int) -> list[str]:
    return [f"следить за акциями {NAME}", condition_line(spec, buy_price), qty_line(spec, position), floor_line(spec),
            f"срок действия заявки: {fmt_int(spec.order_ttl_sec)}{NBSP}сек. учебного времени"]


def confirmation(spec: RuleSpec, buy_price: Decimal, position: int, warnings: list[str]) -> str:
    bullets = "\n".join(f"• {line}" for line in rule_lines(spec, buy_price, position))
    tail = "".join(f"\n\n⚠️ {w}" for w in warnings)
    return f"Я правильно понял?\n{bullets}\n\nПри резком падении покупателей по разрешённой цене может не оказаться.{tail}"


def diff(old: RuleSpec, new: RuleSpec, buy_price: Decimal, position: int) -> str:
    lines = []
    for before, after in zip(rule_lines(old, buy_price, position), rule_lines(new, buy_price, position)):
        if before != after:
            lines.append(f"Было: {before}\nСтало: {after}")
    return "\n".join(lines) or "Условия не изменились."


def _reason(reason: str) -> str:
    return reason[:1].lower() + reason[1:] if reason else reason


def sale_summary(sold: int, target: int, avg: Decimal | None, left: int, reason: str) -> str:
    if not sold:
        return (f"Сработало правило по {NAME}, но продать не удалось.\nПродано: 0 из {shares(target)}. Сделок не было.\n"
                f"Осталось: {shares(left)}.\nПричина остановки: {_reason(reason)}.")
    return (f"Сработало правило по {NAME}.\nПродано: {fmt_int(sold)} из {shares(target)}.\n"
            f"Средняя цена исполнения: {fmt_rub(avg)}.\nОсталось: {shares(left)}.\nПричина остановки: {_reason(reason)}.")


def report_text(r: Report, title: str = "Результат сценария") -> str:
    sold = (f"Продано: {shares(r.sold_qty)}, средняя цена {fmt_rub(r.avg_price)}" if r.avg_price is not None
            else "Продано: 0 акций. Сделок не было")
    sign = "+" if r.difference > 0 else ""
    return (f"{title}\nНачальная стоимость: {fmt_rub(r.initial_value)}\n{sold}\n"
            f"Выручка: {fmt_rub(r.revenue)}, комиссии: {fmt_rub(r.fees)} (считаются с каждой сделки)\n"
            f"Осталось: {shares(r.remaining_qty)}, оценка {fmt_rub(r.remaining_value)} по цене {fmt_rub(r.mark_price)}\n"
            f"Счёт с ботом: {fmt_rub(r.with_bot)}\nПозиция без бота: {fmt_rub(r.hold_value)}\n"
            f"Разница со сценарием без бота: {sign}{fmt_rub(r.difference)}\n"
            f"Оценка остатка не равна сумме, которую удастся получить при продаже.")
