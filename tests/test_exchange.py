"""Биржа: общий стакан, сделки только по существующим заявкам, спрос расходуется."""
from __future__ import annotations

from decimal import Decimal

from app.market.exchange import Exchange, MarketParams
from app.models import Instrument

INST = Instrument("PRMT", "Пример-Тех")


def exchange(**params) -> tuple[Exchange, list]:
    ex = Exchange(INST, Decimal(1000), MarketParams(**params))
    fills: list = []
    ex.on_fill = fills.append
    return ex, fills


def test_sell_hits_bids_best_first_at_their_prices():
    ex, fills = exchange(level_qty=10, levels=3, spread=Decimal(2))
    ex.submit_sell("a", "c1", Decimal(980), 25, 1)
    assert [(f.price, f.qty) for f in fills] == [(Decimal(999), 10), (Decimal(994), 10), (Decimal(989), 5)]


def test_sell_never_trades_below_limit():
    ex, fills = exchange(level_qty=10, levels=3)
    order, left = ex.submit_sell("a", "c1", Decimal(990), 100, 1)
    assert all(f.price >= 990 for f in fills) and left == 80


def test_last_and_best_bid_differ():
    ex, _ = exchange(spread=Decimal(40))
    ex.set_price(Decimal(500), 1)
    assert ex.last == 500 and ex.best_bid == 480


def test_consumed_demand_does_not_return_when_book_is_rebuilt():
    ex, fills = exchange(level_qty=10, levels=3, refill_per_sec=Decimal(0))
    ex.submit_sell("a", "c1", Decimal(1), 30, 1)
    ex.set_price(Decimal(900), 2)
    assert ex.bids == [] and ex.best_bid is None


def test_demand_returns_only_at_configured_rate():
    ex, _ = exchange(level_qty=10, levels=3, refill_per_sec=Decimal("0.1"))
    ex.submit_sell("a", "c1", Decimal(1), 30, 1)
    ex.tick(1000, Decimal(1))
    assert sum(o.qty for o in ex.bids) == 3


def test_resting_sell_fills_when_buyers_come_back():
    ex, fills = exchange(level_qty=10, levels=3, refill_per_sec=Decimal(0))
    ex.set_price(Decimal(500), 1)
    _, left = ex.submit_sell("a", "c1", Decimal(850), 10, 2)
    assert left == 10 and not fills
    ex.set_price(Decimal(900), 3)
    assert sum(f.qty for f in fills) == 10 and all(f.price == 899 for f in fills), "по цене покупателя, а не по границе"


def test_price_time_priority_between_participants():
    ex, fills = exchange(level_qty=5, levels=2, refill_per_sec=Decimal(0))
    ex.set_price(Decimal(500), 1)
    ex.submit_sell("first", "a", Decimal(850), 5, 2)
    ex.submit_sell("second", "b", Decimal(850), 5, 3)
    ex.set_price(Decimal(900), 4)
    assert fills[0].seller == "first" and sum(f.qty for f in fills if f.seller == "first") == 5


def test_halt_freezes_tape_and_resume_prints_gap():
    ex, _ = exchange()
    ex.halt(1)
    ex.set_price(Decimal(500), 2)
    assert ex.last == 1000
    ex.resume(3)
    assert ex.last == 500


def test_resting_sell_without_floor_never_trades_at_a_kopeck_while_buyers_pay_more():
    """К3: остаток по 0,01 ₽ забирает вернувшихся покупателей по их цене, а не по копейке."""
    ex, fills = exchange(level_qty=10, levels=3, refill_per_sec=Decimal(0))
    ex.submit_sell("a", "c1", Decimal("0.01"), 50, 1)
    sold_at_once = sum(f.qty for f in fills)
    assert sold_at_once == 30 and min(f.price for f in fills) == 989
    ex.demand_pool = Decimal(30)
    ex.set_price(Decimal(890), 2)
    later = fills[3:]
    assert later and all(f.price >= 879 for f in later) and ex.last >= 879
    assert all(f.price > Decimal("0.01") for f in fills)


def test_resting_sell_with_floor_takes_buyer_price_when_price_returns():
    ex, fills = exchange(level_qty=10, levels=3, refill_per_sec=Decimal(0))
    ex.set_price(Decimal(500), 1)
    ex.submit_sell("a", "c1", Decimal(850), 10, 2)
    ex.demand_pool = Decimal(30)
    ex.set_price(Decimal(1000), 3)
    assert [(f.price, f.qty) for f in fills] == [(Decimal(999), 10)]


def test_prices_stay_on_tick_and_above_zero():
    ex, fills = exchange(spread=Decimal("0.01"), level_qty=10, levels=3)
    assert ex.best_bid == Decimal("999.99") and ex.best_ask == Decimal(1000)
    ex.submit_sell("a", "c1", Decimal("900.001"), 5, 1)
    assert all(f.price == f.price.quantize(Decimal("0.01")) for f in fills)
    ex.set_price(Decimal("0.004"), 2)
    assert ex.last == Decimal("0.01") and all(o.price >= Decimal("0.01") for o in ex.bids + ex.asks)
