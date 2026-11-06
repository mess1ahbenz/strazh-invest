"""Обязательные сценарии и инварианты исполнения."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.sim.runner import CLIENT
from app.sim.scenarios import SCENARIOS
from conftest import finish, rule_spec, run_scenario


def test_gradual_triggers_near_threshold_and_sells(sim):
    report = run_scenario(sim, "gradual")
    runtime = next(iter(sim.bots[CLIENT].runtimes.values()))
    assert runtime.trigger_price == Decimal(900) and runtime.threshold == Decimal(900)
    assert report.sold_qty == 100 and report.avg_price > Decimal(850)


def test_crash_differs_from_gradual_and_respects_floor(settings, db, sim):
    gradual = run_scenario(sim, "gradual")
    crash_report = None
    sim.start_scenario("crash")
    finish(sim)
    crash_report = sim.report()
    runtime = next(iter(sim.bots[CLIENT].runtimes.values()))
    assert crash_report.sold_qty == 0 and gradual.sold_qty == 100
    assert runtime.trigger_price == Decimal(500), "сработало по факту цены 500, а не по порогу 900"
    assert runtime.state == "expired" and runtime.stop_reason == "Нет покупателей по разрешённой цене"
    assert crash_report.difference == 0


def test_no_fill_at_threshold_price_after_gap(sim):
    """Цена перескочила через порог: продажа по 900 невозможна, потому что таких покупателей уже нет."""
    report = run_scenario(sim, "crash", rule_spec(floor=Decimal(400)))
    assert report.sold_qty > 0 and report.avg_price < Decimal(500)
    assert all(Decimal(e["price"]) < 500 for e in sim.db.executions(sim.run_id, CLIENT))


@pytest.mark.parametrize("key", list(SCENARIOS))
def test_no_execution_below_floor_in_any_scenario(sim, key):
    run_scenario(sim, key)
    for row in sim.db.executions(sim.run_id):
        assert Decimal(row["price"]) >= 850


def test_no_buyers_leaves_visible_remainder(sim):
    report = run_scenario(sim, "no_buyers")
    runtime = next(iter(sim.bots[CLIENT].runtimes.values()))
    assert 0 < report.sold_qty < 100
    assert report.remaining_qty == 100 - report.sold_qty and runtime.remaining == report.remaining_qty
    assert report.remaining_value == report.mark_price * report.remaining_qty
    orders = sim.db.orders(sim.run_id, CLIENT)
    assert orders[0]["status"] == "expired" and orders[0]["filled_qty"] == report.sold_qty


def test_rebound_can_be_worse_than_holding(sim):
    report = run_scenario(sim, "rebound")
    assert report.sold_qty == 100 and report.mark_price > Decimal(1000)
    assert report.difference < 0 and report.with_bot < report.hold_value


def test_commission_and_balance_are_exact(sim):
    report = run_scenario(sim, "gradual")
    rows = sim.db.executions(sim.run_id, CLIENT)
    gross, fee = sum(Decimal(r["gross"]) for r in rows), sum(Decimal(r["fee"]) for r in rows)
    assert report.revenue == gross and report.fees == fee and fee > 0
    assert report.cash == gross - fee
    assert report.with_bot == report.cash + report.remaining_value
    assert report.hold_value == report.mark_price * 100


def test_position_never_negative_and_never_oversold(sim):
    sim.db.create_rule(rule_spec(), "test")
    sim.start_scenario("crowd", fleet_size=5)
    finish(sim)
    for account, portfolio in sim.broker.accounts.items():
        assert portfolio.positions["PRMT"] >= 0 and portfolio.reserved["PRMT"] >= 0
        assert sum(e.qty for e in sim.broker.get_executions(account)) <= 100


def test_order_is_not_a_trade(sim):
    sim.db.create_rule(rule_spec(), "test")
    sim.reset()
    sim.command("price", price=Decimal(500))
    sim.advance(1000)
    portfolio = sim.broker.get_positions(CLIENT)
    assert portfolio.positions["PRMT"] == 100 and portfolio.reserved["PRMT"] == 100
    assert sim.report().revenue == 0 and sim.db.orders(sim.run_id, CLIENT)[0]["status"] == "new"


def test_exec_delay_is_scenario_time(sim):
    sim.exec_delay_ms = 2000
    sim.db.create_rule(rule_spec(floor=Decimal(800)), "test")
    sim.reset()
    sim.command("price", price=Decimal(890))
    sim.advance(1900)
    assert sim.db.orders(sim.run_id, CLIENT)[0]["status"] == "pending"
    sim.advance(200)
    order = sim.db.orders(sim.run_id, CLIENT)[0]
    assert order["arrival_at"] - order["sent_at"] == 2000 and order["filled_qty"] > 0


def test_market_can_move_during_delay(sim):
    """Решение принято при 890, но заявка поступает уже в другой стакан."""
    sim.exec_delay_ms = 2000
    sim.db.create_rule(rule_spec(), "test")
    sim.reset()
    sim.command("price", price=Decimal(890))
    sim.advance(1000)
    sim.command("price", price=Decimal(500))
    sim.advance(1500)
    assert sim.report().sold_qty == 0


def test_decisions_do_not_depend_on_future_prices(settings):
    """Два рынка совпадают до момента T и расходятся после. До T действия бота обязаны быть одинаковыми."""
    from app.db import Database
    from app.sim.runner import Simulation

    def prefix(after_price: Decimal) -> list:
        db = Database(":memory:")
        sim = Simulation(settings, db)
        db.create_rule(rule_spec(), "test")
        sim.reset()
        sim.schedule(1000, lambda: sim.exchange.set_price(Decimal(895), sim.now))
        sim.schedule(5000, lambda: sim.exchange.set_price(after_price, sim.now))
        sim.advance(4000)
        return [(o["status"], o["qty"], o["limit_price"], o["sent_at"]) for o in db.orders(sim.run_id, CLIENT)]

    assert prefix(Decimal(100)) == prefix(Decimal(1500))


def test_trigger_ignores_liquidity(sim):
    """Спрос исчез, но цена не дошла до порога: продажа не начинается."""
    sim.db.create_rule(rule_spec(), "test")
    sim.reset()
    sim.command("demand", scale=Decimal(0))
    sim.advance(3000)
    assert sim.db.orders(sim.run_id, CLIENT) == []


def test_peak_reference_follows_maximum(sim):
    from app.models import Reference
    sim.db.create_rule(rule_spec(reference=Reference.PEAK, floor=Decimal(500)), "test")
    sim.reset()
    sim.command("price", price=Decimal(1200))
    sim.command("price", price=Decimal(1085))
    sim.advance(1000)
    assert sim.db.orders(sim.run_id, CLIENT) == []
    sim.command("price", price=Decimal(1079))
    sim.advance(1000)
    assert sim.report().sold_qty > 0


def test_halt_rejects_order_and_bot_returns_after_resume(sim):
    sim.db.create_rule(rule_spec(), "test")
    sim.reset()
    sim.command("price", price=Decimal(890))
    sim.command("halt")
    sim.advance(1000)
    runtime = next(iter(sim.bots[CLIENT].runtimes.values()))
    orders = sim.db.orders(sim.run_id, CLIENT)
    assert runtime.state == "waiting_market" and orders[0]["status"] == "rejected"
    sim.command("resume")
    sim.advance(1000)
    assert sim.report().sold_qty > 0
