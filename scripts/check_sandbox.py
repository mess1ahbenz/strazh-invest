"""Проверка адаптера на настоящей песочнице T-Invest. Деньги виртуальные, настоящие счета недоступны.

Нужен TINVEST_SANDBOX_TOKEN и бумага на счёте песочницы:
    TINVEST_SANDBOX_TOKEN=... python scripts/check_sandbox.py SU26238RMFS4_TQOB
"""
from __future__ import annotations

import os
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.broker.tinvest_sandbox import McpReader, TInvestSandboxBroker, transport_from_env  # noqa: E402
from app.models import OrderRequest, Side  # noqa: E402


PAUSE_SEC = 8      # песочница отвечает 429 при частых заявках


def main(uid: str) -> None:
    token = os.environ["TINVEST_SANDBOX_TOKEN"]
    broker = TInvestSandboxBroker(token, transport=transport_from_env(dict(os.environ)))
    accounts = broker.accounts()
    if not accounts:
        raise SystemExit("На токене нет счёта песочницы")
    account = accounts[0]["id"]
    run = str(int(time.time()))
    portfolio = broker.get_positions(account)
    print("счёт песочницы найден, бумаг по", uid, ":", portfolio.positions.get(uid, 0))
    market = broker.get_market_data(uid)
    print("рынок: последняя", market.last, "покупатель", market.best_bid, "продавец", market.best_ask, "торги остановлены:", market.halted)

    sell = OrderRequest(f"{run}-fill", account, uid, Side.SELL, 1, market.best_bid, 60_000)
    first = broker.post_order(sell)
    repeat = broker.post_order(sell)
    print("продажа 1 лота:", first.status, "| повтор с тем же ключом:", repeat.status, "исполнено", repeat.filled_qty)
    after = broker.get_positions(account).positions.get(uid, 0)
    print("позиция после двух вызовов:", after, "изменение:", after - portfolio.positions.get(uid, 0))

    time.sleep(PAUSE_SEC)
    high = market.best_ask + Decimal("0.5")
    rest = OrderRequest(f"{run}-rest", account, uid, Side.SELL, 1, high, 60_000)
    resting = broker.post_order(rest)
    print("заявка вне рынка:", resting.status)
    time.sleep(PAUSE_SEC)
    cancelled = broker.cancel_order(account, rest.client_order_id)
    print("снятие:", cancelled.status)
    print("состояние заявки:", broker.get_order_state(account, sell.client_order_id).status, "этапов исполнения:", len(broker.get_executions(account, sell.client_order_id)))

    reader = McpReader(token, transport=transport_from_env(dict(os.environ)))
    found = reader.call("invest_list_accounts", {"status": "ACCOUNT_STATUS_OPEN"})
    print("MCP: счетов песочницы", len(found.get("accounts", [])))
    try:
        reader.call("invest_create_order", {})
    except PermissionError:
        print("MCP: заявка запрещена до обращения к сети")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "SU26238RMFS4_TQOB")
