from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.config import Settings
from app.db import Database
from app.models import ConditionKind, QtyMode, Reference, RuleSpec
from app.sim.runner import Simulation


def rule_spec(**changes) -> RuleSpec:
    base = RuleSpec("PRMT", ConditionKind.DROP_PCT, Decimal(10), Reference.BUY_PRICE, QtyMode.ALL, None, Decimal(850), 60)
    return replace(base, **changes)


@pytest.fixture
def settings(monkeypatch) -> Settings:
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALLOWED_USERS", "ANTHROPIC_API_KEY", "STRAZH_ACCESS_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("STRAZH_DB", ":memory:")
    return Settings.load(None)


@pytest.fixture
def db() -> Database:
    return Database(":memory:")


@pytest.fixture
def sim(settings, db) -> Simulation:
    return Simulation(settings, db)


def finish(sim: Simulation, step: int = 500) -> None:
    while sim.status == "running":
        sim.advance(step)


def run_scenario(sim: Simulation, key: str, spec: RuleSpec | None = None):
    sim.db.create_rule(spec or rule_spec(), "test")
    sim.start_scenario(key)
    finish(sim)
    return sim.report()
