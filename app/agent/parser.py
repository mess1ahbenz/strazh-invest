"""Шаблонный разбор команд. Это честно ограниченный режим: он понимает перечисленные формы, а не произвольный текст.

Разбор только извлекает поля. Он ничего не подставляет за клиента: если в тексте нет точки отсчёта, поля не будет.
Числа с пробелами между разрядами («1 000») понимаются, но разряд не склеивается с количеством акций
(«не дешевле 850 100 акций» — это 850 ₽ и 100 акций).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

F = re.IGNORECASE | re.UNICODE
SP = "[ \u00a0]"
QTY_UNIT = r"(?:акци\w*|штук\w*|шт\b|бумаг\w*)"
# Разряды через пробел, но группа перед единицей количества («100 акций») к числу не приклеивается.
_GROUPS = rf"\d{{1,3}}(?:{SP}\d{{3}}(?!\d)(?!{SP}*{QTY_UNIT}))+"
NUM = rf"((?:{_GROUPS}|\d+)(?:[.,]\d+)?)"
SNUM = rf"([-−]?{SP}*(?:{_GROUPS}|\d+)(?:[.,]\d+)?)"           # со знаком: «на -10%» — ошибка, а не 10%
NUM_QTY = rf"((?:\d{{1,3}}(?:{SP}\d{{3}}(?!\d))+|\d+))"
PCT_SIGN = r"(?:%|процент\w*)"
RUB = r"(?:₽|руб\w*|р\.)"

FLOOR_WORDS = (r"(?:не\s+(?:продавай\s+|продавать\s+|выставляй\s+|выставлять\s+)?(?:\w+\s+)?(?:дешевле|ниже)"
               r"|минимальн\w+\s+цен\w*(?:\s+продажи)?(?:\s+на)?(?:\s*[:—-])?|границ\w+(?:\s+цены)?(?:\s+на)?)")
FLOOR_PCT = re.compile(FLOOR_WORDS + r"\s*" + NUM + r"\s*" + PCT_SIGN, F)
FLOOR = re.compile(FLOOR_WORDS + r"\s*" + NUM + rf"(?:\s*{RUB})?", F)
NO_FLOOR = re.compile(r"без\s+(?:минимальн\w+|ограничени\w+|границ\w+|нижн\w+)", F)
REF_BUY = re.compile(r"(?:цен\w+\s+)?(?:покупк\w+|входа)|средн\w+\s+цен\w+", F)
REF_PEAK = re.compile(r"максимум\w*|пик\w*|хай", F)
DROP_VERB = r"(?<!не\s)(?:упад\w*|падени\w*|сниж\w*|сниз\w*|опуст\w*|подешеве\w*|просяд\w*|просел\w*)"
DROP_PCT = re.compile(DROP_VERB + r"(?:\s+цен\w*)?\s+(?:на\s+)?" + SNUM + r"\s*" + PCT_SIGN, F)
DROP_ABS = re.compile(DROP_VERB + r"(?:\s+цен\w*)?\s+(?:на\s+)?" + SNUM + r"\s*" + RUB, F)
BELOW = re.compile(r"(?<!не\s)(?:(?:упад\w+|опуст\w+|снизит\w+|дойд\w+|пробь\w+)\s+(?:до|ниже|уровня)|ниже|меньше)\s+"
                   r"(?:уровня\s+)?" + NUM + rf"(?!\s*{PCT_SIGN})(?!\s*{QTY_UNIT})(?:\s*{RUB})?", F)
PCT = re.compile(SNUM + r"\s*" + PCT_SIGN, F)
QTY_ALL = re.compile(r"\b(?:всю|все|всё|целиком|полностью|всей)\b", F)
QTY_HALF = re.compile(r"\bполовин\w*", F)
QTY_FRACTION = re.compile(r"\b(\d+)\s*/\s*(\d+)\s*(?:от\s+)?позици\w*", F)
QTY_SHARES = re.compile(NUM_QTY + r"\s*" + QTY_UNIT, F)
QTY_PCT = re.compile(NUM + r"\s*" + PCT_SIGN + r"\s+(?:от\s+)?позици\w*", F)
QTY_PCT_SELL = re.compile(r"прода\w*\s+" + NUM + r"\s*" + PCT_SIGN, F)          # «продай 30% …»
TTL = re.compile(r"(?:заявк\w*|срок\w*|держ\w+)[^.]*?" + SNUM + r"\s*(секунд\w*|сек\b|минут\w*|мин\b|час\w*)", F)
SALE_WORDS = re.compile(r"прода\w*|следи\w*|упад\w+|сниж\w+|стоп|защит\w*", F)
NEGATED_SALE = re.compile(r"^\W*не\s+прода\w*\b(?!\s+(?:\w+\s+)?(?:дешевле|ниже))", F)
PAUSE_RULE = re.compile(r"(?:приостанов\w*|останов\w*|пауз\w*)[^.]*правил", F)
RESUME_RULE = re.compile(r"(?:возобнов\w*|включ\w*)[^.]*правил", F)
DELETE_RULE = re.compile(r"(?:удал\w*|убер\w*|отмен\w*)[^.]*правил", F)
CANCEL = re.compile(r"^\W*(?:отмен\w*|не\s+надо|передумал\w*)(?:\s+(?:черновик|изменени\w*|это|всё|все))?\W*$", F)
EDIT_WORDS = re.compile(r"\b(?:измени|поменяй|поставь|замени|сделай|исправь)\b", F)

UNIT_SECONDS = {"сек": 1, "мин": 60, "час": 3600}


def _num(raw: str) -> Decimal:
    return Decimal(re.sub(r"\s", "", raw).replace(",", ".").replace("−", "-"))


@dataclass
class Parsed:
    intent: str                       # rule, edit, pause_rule, resume_rule, delete_rule, cancel, unknown
    fields: dict[str, Any] = field(default_factory=dict)
    source: str = "templates"
    note: str = ""


def _cut(text: str, m: re.Match) -> str:
    return text[:m.start()] + " " + text[m.end():]


def parse_fields(text: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    rest = text
    if m := FLOOR_PCT.search(rest):
        fields["floor_pct"] = _num(m.group(1))          # процент от цены покупки, рубли считает диалог
        rest = _cut(rest, m)
    elif m := FLOOR.search(rest):
        fields["floor"] = _num(m.group(1))
        rest = _cut(rest, m)
    elif NO_FLOOR.search(rest):
        fields["no_floor"] = True
    if m := TTL.search(rest):
        unit = next(v for k, v in UNIT_SECONDS.items() if m.group(2).lower().startswith(k))
        fields["order_ttl_sec"] = int(_num(m.group(1)) * unit)
        rest = _cut(rest, m)
    if m := QTY_PCT.search(rest):
        fields["qty_mode"], fields["qty_value"] = "percent", _num(m.group(1))
        rest = _cut(rest, m)
    elif m := QTY_FRACTION.search(rest):
        num, den = int(m.group(1)), int(m.group(2))
        if den:
            fields["qty_mode"], fields["qty_value"] = "percent", Decimal(num * 100) / Decimal(den)
        rest = _cut(rest, m)
    elif m := QTY_SHARES.search(rest):
        fields["qty_mode"], fields["qty_value"] = "shares", _num(m.group(1))
        rest = _cut(rest, m)
    elif (m := QTY_PCT_SELL.search(rest)) and len(PCT.findall(rest)) > 1:
        fields["qty_mode"], fields["qty_value"] = "percent", _num(m.group(1))     # второй процент — условие
        rest = rest[:m.start(1)] + " " + rest[m.end():]
    elif QTY_HALF.search(rest):
        fields["qty_mode"], fields["qty_value"] = "percent", Decimal(50)
    elif QTY_ALL.search(rest):
        fields["qty_mode"], fields["qty_value"] = "all", None
    if m := DROP_PCT.search(rest):
        fields["condition"], fields["value"] = "drop_pct", _num(m.group(1))
    elif m := DROP_ABS.search(rest):
        fields["condition"], fields["value"] = "drop_abs", _num(m.group(1))
    elif m := BELOW.search(rest):
        fields["condition"], fields["value"] = "price_below", _num(m.group(1))
    elif len(found := PCT.findall(rest)) == 1:
        fields["condition"], fields["value"] = "drop_pct", _num(found[0])
    if REF_PEAK.search(rest):
        fields["reference"] = "peak_since_enable"
    elif REF_BUY.search(rest):
        fields["reference"] = "buy_price"
    return fields


def parse(text: str) -> Parsed:
    if CANCEL.search(text):
        return Parsed("cancel")
    if DELETE_RULE.search(text):
        return Parsed("delete_rule")
    if PAUSE_RULE.search(text):
        return Parsed("pause_rule")
    if RESUME_RULE.search(text):
        return Parsed("resume_rule")
    if NEGATED_SALE.search(text):
        return Parsed("unknown", note="negated")         # «не продавай, если…» — это не правило продажи
    try:
        fields = parse_fields(text)
    except (InvalidOperation, ValueError, OverflowError):
        return Parsed("unknown", note="bad_number")
    if EDIT_WORDS.search(text) and fields:
        return Parsed("edit", fields)
    if fields or SALE_WORDS.search(text):
        return Parsed("rule", fields)
    return Parsed("unknown")
