"""Повторы, пауза, сброс, общий рынок и независимость движка от чата и модели."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.models import MarketEvent, OrderRequest, OrderStatus, QtyMode, Side
from app.sim.runner import CLIENT
from conftest import finish, rule_spec, run_scenario


def armed(sim, **spec):
    sim.db.create_rule(rule_spec(**spec), "test")
    sim.reset()
    return next(iter(sim.bots[CLIENT].runtimes.values()))


def test_redelivered_event_does_not_sell_twice(sim):
    armed(sim)
    sim.command("price", price=Decimal(890))
    event = MarketEvent(sim.bots[CLIENT].last_seq, sim.now, "tape", Decimal(890), None, None)
    for _ in range(5):
        sim.bots[CLIENT].on_market_event(event)
    assert len(sim.db.orders(sim.run_id, CLIENT)) == 1


def test_old_event_replayed_later_is_ignored(sim):
    """Правило ещё ждёт, и старое событие с низкой ценой не должно его запустить."""
    armed(sim)
    sim.command("price", price=Decimal(950))
    stale = MarketEvent(1, 0, "tape", Decimal(100), None, None)
    sim.bots[CLIENT].on_market_event(stale)
    assert sim.db.orders(sim.run_id, CLIENT) == []


def test_rearmed_bot_with_same_action_key_does_not_post_again(sim):
    """Даже если состояние бота потеряно, тот же ключ действия не выполнится второй раз."""
    runtime = armed(sim, qty_mode=QtyMode.SHARES, qty_value=Decimal(10))    # в позиции остаётся что продавать
    sim.command("price", price=Decimal(890))
    assert len(sim.db.orders(sim.run_id, CLIENT)) == 1
    runtime.state, runtime.trigger_no, runtime.attempt = "armed", 0, 0
    posted = []
    real = sim.broker.post_order
    sim.broker.post_order = lambda request: posted.append(request) or real(request)
    sim.command("price", price=Decimal(885))
    assert posted == [], "ключ действия не дошёл до брокера повторно"


def test_broker_post_is_idempotent(sim):
    request = OrderRequest("same", CLIENT, "PRMT", Side.SELL, 10, Decimal(100), 1000)
    first, second = sim.broker.post_order(request), sim.broker.post_order(request)
    assert first is second and sim.broker.get_positions(CLIENT).reserved["PRMT"] == 10


def test_broker_refuses_more_than_available(sim):
    view = sim.broker.post_order(OrderRequest("big", CLIENT, "PRMT", Side.SELL, 101, Decimal(100), 1000))
    assert view.status is OrderStatus.REJECTED


def test_broker_refuses_to_buy(sim):
    view = sim.broker.post_order(OrderRequest("buy", CLIENT, "PRMT", Side.BUY, 1, Decimal(100), 1000))
    assert view.status is OrderStatus.REJECTED


def test_pause_cancels_open_order_and_keeps_trades(sim):
    armed(sim)
    sim.exchange.set_params(0, level_qty=5)
    sim.exchange.demand_pool = Decimal(30)
    sim.command("price", price=Decimal(890))
    sim.advance(1000)
    sold = sim.report().sold_qty
    assert 0 < sold < 100
    cancelled = sim.pause_bots()
    assert len(cancelled) == 1 and sim.db.orders(sim.run_id, CLIENT)[0]["status"] == "cancelled"
    assert sim.broker.get_positions(CLIENT).reserved["PRMT"] == 0
    sim.command("price", price=Decimal(860))
    sim.advance(3000)
    assert sim.report().sold_qty == sold, "на паузе новых действий нет, но сделанное осталось"


def test_resume_continues_remaining_sale(sim):
    runtime = armed(sim)
    sim.exchange.set_params(0, level_qty=5)
    sim.exchange.demand_pool = Decimal(30)
    sim.command("price", price=Decimal(890))
    sim.advance(1000)
    sold = sim.report().sold_qty
    sim.pause_bots()
    sim.resume_bots()
    sim.advance(1000)
    orders = sim.db.orders(sim.run_id, CLIENT)
    assert len(orders) == 2 and orders[1]["qty"] == 100 - sold and runtime.state == "selling"


def test_resume_after_price_fell_during_pause_triggers_now(sim):
    armed(sim)
    sim.pause_bots()
    sim.command("price", price=Decimal(890))
    sim.advance(1000)
    assert sim.db.orders(sim.run_id, CLIENT) == []
    sim.resume_bots()
    sim.advance(1000)
    assert len(sim.db.orders(sim.run_id, CLIENT)) == 1


def test_cancel_while_order_travels_to_market(sim):
    sim.exec_delay_ms = 5000
    armed(sim)
    sim.command("price", price=Decimal(890))
    sim.pause_bots()
    sim.advance(6000)
    assert sim.report().sold_qty == 0 and sim.db.orders(sim.run_id, CLIENT)[0]["status"] == "cancelled"


def test_reset_clears_state_and_old_tasks_do_not_fire(sim):
    sim.db.create_rule(rule_spec(), "test")
    sim.start_scenario("gradual")
    sim.advance(5000)
    old_run = sim.run_id
    sim.command("gradual", price=Decimal(100), seconds=5)
    sim.reset()
    assert sim.run_id != old_run and sim.scenario_key is None and sim.now == 0
    sim.advance(70_000)
    assert sim.exchange.last == 1000, "старый сценарий и снижение не должны дойти до нового прогона"
    assert sim.report().sold_qty == 0 and sim.db.orders(sim.run_id) == []


def test_reset_inside_time_step_stops_the_step(sim):
    sim.schedule(100, sim.reset)
    sim.advance(10_000)
    assert sim.now == 0, "время нового прогона не должно продолжиться со старого шага"


def test_new_scenario_replaces_running_one(sim):
    sim.db.create_rule(rule_spec(), "test")
    sim.start_scenario("gradual")
    sim.advance(5000)
    sim.start_scenario("rebound")
    sim.advance(1000)
    assert sim.scenario_key == "rebound" and sim.now == 1000


def test_rules_survive_reset_but_runtime_starts_clean(sim):
    run_scenario(sim, "gradual")
    sim.reset()
    runtime = next(iter(sim.bots[CLIENT].runtimes.values()))
    assert runtime.state == "armed" and runtime.sold_qty == 0


def test_failing_notifications_do_not_stop_trading(sim, db):
    calls = []

    def boom(kind, **facts):
        calls.append(kind)
        raise ConnectionError("Telegram недоступен")

    sim.on_event = boom
    db.create_rule(rule_spec(), "test")
    sim.reset()
    sim.command("price", price=Decimal(890))
    sim.advance(1000)
    assert calls and sim.report().sold_qty > 0 and sim.notify_failures == len(calls)


def test_fleet_shares_one_book(sim):
    sim.db.create_rule(rule_spec(), "test")
    sim.start_scenario("crowd", fleet_size=100)
    target = sim.exchange.params.demand_target
    finish(sim, 1000)
    fleet = sim.fleet_report()
    assert fleet["participants"] == 101 and fleet["total_wanted"] == 10_100
    assert fleet["total_sold"] < fleet["total_wanted"]
    assert fleet["total_sold"] <= target * 2, "продано не больше, чем позволяет общий спрос с учётом восстановления"
    sold = [r["sold"] for r in fleet["rows"]]
    assert max(sold) == 100 and min(sold) == 0, "одна и та же заявка покупателя не достаётся всем"


def test_one_bot_sells_more_alone_than_in_crowd(settings):
    from app.db import Database
    from app.sim.runner import Simulation

    def sold(size: int) -> int:
        db = Database(":memory:")
        sim = Simulation(settings, db)
        db.create_rule(rule_spec(), "test")
        sim.start_scenario("crowd", fleet_size=size)
        finish(sim, 1000)
        return sim.report().sold_qty + sum(r["sold"] for r in sim.fleet_report()["rows"] if r["account"] != CLIENT)

    assert sold(1) <= 200 and sold(100) > sold(1)


def test_same_seed_gives_same_order_and_other_seed_changes_it(settings):
    from app.db import Database
    from app.sim.runner import Simulation

    def arrivals(seed: int) -> list:
        db = Database(":memory:")
        sim = Simulation(settings, db)
        sim.fleet_config.seed, sim.fleet_config.jitter_ms = seed, 500
        db.create_rule(rule_spec(), "test")
        sim.start_scenario("crowd", fleet_size=10)
        finish(sim, 1000)
        return [a["account"] for a in sim.fleet_report()["arrivals"]]

    assert arrivals(1) == arrivals(1) and arrivals(1) != arrivals(2)


def test_partial_quantity_rule(sim):
    report = run_scenario(sim, "gradual", rule_spec(qty_mode=QtyMode.PERCENT, qty_value=Decimal(50)))
    assert report.sold_qty == 50 and report.remaining_qty == 50


def test_new_rule_version_cancels_order_with_old_limit(sim):
    runtime = armed(sim, floor=Decimal(880))
    sim.exchange.set_params(0, level_qty=1)
    sim.exchange.demand_pool = Decimal(6)
    sim.command("price", price=Decimal(890))
    sim.advance(1000)
    first = sim.db.orders(sim.run_id, CLIENT)[0]
    assert first["status"] in ("new", "partial")
    rule = sim.db.add_version(runtime.rule.id, replace(runtime.rule.spec, floor=Decimal(860)), "test")
    sim.apply_rule(rule)
    sim.advance(1000)
    orders = sim.db.orders(sim.run_id, CLIENT)
    assert orders[0]["status"] == "cancelled" and orders[1]["limit_price"] == "860"


def test_pause_survives_reset(sim):
    armed(sim)
    sim.pause_bots()
    sim.reset()
    sim.command("price", price=Decimal(890))
    sim.advance(2000)
    assert sim.bots[CLIENT].paused and sim.db.orders(sim.run_id, CLIENT) == []
    sim.resume_bots()
    sim.advance(1000)
    assert len(sim.db.orders(sim.run_id, CLIENT)) == 1


def test_restart_keeps_rules_versions_and_pause(tmp_path, settings):
    from app.db import Database
    from app.sim.runner import Simulation

    path = str(tmp_path / "s.db")
    first = Database(path)
    rule = first.create_rule(rule_spec(), "test")
    first.add_version(rule.id, rule_spec(floor=Decimal(800)), "test")
    sim = Simulation(settings, first)
    sim.pause_bots()
    old_run = sim.run_id
    first.conn.close()

    second = Database(path)
    restarted = Simulation(settings, second)
    runtime = next(iter(restarted.bots[CLIENT].runtimes.values()))
    assert runtime.rule.version == 2 and runtime.rule.spec.floor == 800 and restarted.bots[CLIENT].paused
    assert second._rows("SELECT status FROM runs WHERE id = ?", old_run)[0]["status"] == "interrupted"


def test_restart_does_not_repeat_old_notification_or_action(tmp_path, settings):
    from app.db import Database

    path = str(tmp_path / "s.db")
    db = Database(path)
    assert db.claim("action-1") and db.enqueue("n1", "текст")
    db.conn.close()
    again = Database(path)
    assert not again.claim("action-1") and not again.enqueue("n1", "текст")


def test_fleet_runs_on_default_rule_without_client_rule(sim):
    sim.start_scenario("crowd", fleet_size=3)
    finish(sim, 1000)
    assert sim.fleet_uses_default_rule and sim.fleet_report()["total_sold"] > 0
    assert sim.report().sold_qty == 0, "счёт клиента без правила не трогается"
