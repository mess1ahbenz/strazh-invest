"""Деньги и проценты только в Decimal. Рубли округляются до копейки по правилу половины вверх."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

KOPECK = Decimal("0.01")
NBSP = "\u00a0"            # неразрывный пробел: «1 000 ₽» не разрывается по строкам


def D(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def rub(value: Any) -> Decimal:
    return D(value).quantize(KOPECK, ROUND_HALF_UP)


def commission(gross: Decimal, bps: Decimal) -> Decimal:
    """Комиссия с суммы сделки. 5 б.п. = 0,05%."""
    return rub(gross * bps / Decimal(10_000))


def fmt_int(value: Any) -> str:
    """Целое с неразрывными пробелами между разрядами: 10 100."""
    return f"{int(value):,}".replace(",", NBSP)


def fmt_rub(value: Any) -> str:
    x = rub(value)
    sign = "−" if x < 0 else ""
    whole, frac = f"{abs(x):.2f}".split(".")
    groups = fmt_int(whole)
    return f"{sign}{groups},{frac}{NBSP}₽" if frac != "00" else f"{sign}{groups}{NBSP}₽"


def fmt_pct(value: Any) -> str:
    return f"{D(value).normalize():f}".replace(".", ",") + "%"
