"""Адаптер песочницы без сети. Форматы и ошибка 30057 сняты с настоящей песочницы 31.10.2026."""
from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from app.broker.tinvest_sandbox import HOST, McpReader, TInvestSandboxBroker, from_quotation, quotation, request_key
from app.models import OrderRequest, OrderStatus, Side


class Server:
    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.posts = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == HOST, "только адрес песочницы"
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        if method == "PostSandboxOrder":
            self.posts += 1
            key = body["orderId"]
            if key in self.orders:
                return httpx.Response(400, json={"code": 3, "message": "The order is a duplicate", "description": "30057"})
            self.orders[key] = {"orderId": "exchange-1", "lotsExecuted": "1", "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
                                "executedOrderPrice": {"units": "521", "nano": 540000000}}
            return httpx.Response(200, json=self.orders[key])
        if method == "GetSandboxOrderState":
            assert body["orderIdType"] == "ORDER_ID_TYPE_REQUEST"
            return httpx.Response(200, json=self.orders[body["orderId"]])
        if method == "CancelSandboxOrder":
            self.orders[body["orderId"]]["executionReportStatus"] = "EXECUTION_REPORT_STATUS_CANCELLED"
            return httpx.Response(200, json={"time": "2026-10-31T00:00:00Z"})
        if method == "GetSandboxPositions":
            return httpx.Response(200, json={"money": [{"currency": "rub", "units": "94474", "nano": 908935000}],
                                             "securities": [{"ticker": "OFZ", "classCode": "TQOB", "balance": "10", "blocked": "2"}]})
        if method == "GetOrderBook":
            return httpx.Response(200, json={"bids": [{"price": {"units": "52", "nano": 154000000}, "quantity": "6"}],
                                             "asks": [{"price": {"units": "52", "nano": 200000000}, "quantity": "74"}],
                                             "lastPrice": {"units": "52", "nano": 200000000}})
        if method == "GetTradingStatus":
            return httpx.Response(200, json={"limitOrderAvailableFlag": True})
        return httpx.Response(404, text="нет")


@pytest.fixture
def server():
    return Server()


@pytest.fixture
def broker(server):
    return TInvestSandboxBroker("token", transport=httpx.MockTransport(server))


REQUEST = OrderRequest("cid-1", "acc", "OFZ_TQOB", Side.SELL, 1, Decimal("52.154"), 60_000)


def test_quotation_round_trip():
    assert quotation(Decimal("52.154")) == {"units": "52", "nano": 154000000}
    assert from_quotation({"units": "52", "nano": 154000000}) == Decimal("52.154")


def test_repeat_with_same_key_does_not_create_second_order(broker, server):
    first, second = broker.post_order(REQUEST), broker.post_order(REQUEST)
    assert first.status is OrderStatus.FILLED and second.status is OrderStatus.FILLED
    assert len(server.orders) == 1 and request_key("cid-1") in server.orders


def test_request_key_is_stable_across_restarts():
    assert request_key("cid-1") == request_key("cid-1") and request_key("cid-1") != request_key("cid-2")


def test_buy_is_refused_before_network(broker, server):
    with pytest.raises(ValueError):
        broker.post_order(OrderRequest("b", "acc", "OFZ_TQOB", Side.BUY, 1, Decimal(50), 1000))
    assert server.posts == 0


def test_positions_and_market(broker):
    portfolio = broker.get_positions("acc")
    assert portfolio.cash == Decimal("94474.91") and portfolio.positions["OFZ_TQOB"] == 12 and portfolio.available("OFZ_TQOB") == 10
    market = broker.get_market_data("OFZ_TQOB")
    assert market.best_bid == Decimal("52.154") and market.last == Decimal("52.2") and not market.halted


def test_cancel_uses_request_key(broker, server):
    broker.post_order(REQUEST)
    assert broker.cancel_order("acc", "cid-1").status is OrderStatus.CANCELLED


def test_secret_not_in_repr():
    broker = TInvestSandboxBroker("very-secret", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert "very-secret" not in repr(broker)


def test_mcp_reader_refuses_trading_before_network():
    reader = McpReader("t", transport=httpx.MockTransport(lambda r: pytest.fail("сети быть не должно")))
    with pytest.raises(PermissionError):
        reader.call("invest_create_order", {})
