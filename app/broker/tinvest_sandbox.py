"""Адаптер песочницы T-Invest. По умолчанию не используется: платформа работает на искусственном рынке.

Адрес песочницы зашит и другого нет, поэтому отсюда нельзя отправить настоящее поручение. Торговый бот к этому адаптеру
пока не подключён: нет потока рыночных данных, а методы здесь блокирующие (см. docs/TINVEST.md).

Разделение каналов:
- заявки, их состояние, стакан и позиции — прямой REST: у `PostOrder` есть обязательный ключ идемпотентности `order_id`,
  а у инструмента MCP `invest_create_order` такого поля нет;
- MCP (`McpReader`) — справочная информация для агента: поиск инструмента, новости, сведения о бумаге.
"""
from __future__ import annotations

import json
import ssl
import subprocess
import uuid
from decimal import Decimal
from typing import Any

import httpx

from ..models import (
    BookLevel, Execution, MarketSnapshot, OrderRequest, OrderStatus, OrderView, Portfolio, Side,
)
from ..money import D, commission, rub

HOST = "sandbox-invest-public-api.tbank.ru"
REST = f"https://{HOST}/rest/tinkoff.public.invest.api.contract.v1"
MCP = f"https://{HOST}/mcp"
ORDER_NAMESPACE = uuid.UUID("6f1f6a7e-7c55-4f3e-9f21-5d1f3a0a1b10")
STATUS = {"EXECUTION_REPORT_STATUS_NEW": OrderStatus.NEW, "EXECUTION_REPORT_STATUS_PARTIALLYFILL": OrderStatus.PARTIAL,
          "EXECUTION_REPORT_STATUS_FILL": OrderStatus.FILLED, "EXECUTION_REPORT_STATUS_CANCELLED": OrderStatus.CANCELLED,
          "EXECUTION_REPORT_STATUS_REJECTED": OrderStatus.REJECTED}
READ_ONLY_TOOLS = frozenset({"invest_find_instrument", "invest_get_share", "invest_get_bond", "invest_get_news",
                             "invest_get_bond_events", "invest_list_accounts"})


class DuplicateOrder(Exception):
    """Заявка с таким ключом уже принята."""


def request_key(client_order_id: str) -> str:
    return str(uuid.uuid5(ORDER_NAMESPACE, client_order_id))


def quotation(price: Decimal) -> dict[str, Any]:
    units = int(price)
    return {"units": str(units), "nano": int((price - units) * 1_000_000_000)}


def from_quotation(value: dict[str, Any] | None) -> Decimal:
    if not value:
        return Decimal(0)
    return Decimal(str(value.get("units", 0))) + Decimal(value.get("nano", 0)) / Decimal(1_000_000_000)


class PinnedTransport(httpx.BaseTransport):
    """Соединение с заданным адресом мимо VPN. Имя хоста остаётся в SNI, сертификат проверяется по нему."""

    def __init__(self, ip: str, verify: Any, local_address: str | None = None) -> None:
        self.ip, self.inner = ip, httpx.HTTPTransport(verify=verify, local_address=local_address)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        request.url = request.url.copy_with(host=self.ip)
        request.extensions = {**request.extensions, "sni_hostname": host}
        return self.inner.handle_request(request)


def transport_from_env(env: dict[str, str]) -> httpx.BaseTransport | None:
    """Только для сетей, где VPN не пропускает T-Банк: TINVEST_CONNECT_IP, TINVEST_BIND_INTERFACE, TINVEST_CA_BUNDLE."""
    ca = env.get("TINVEST_CA_BUNDLE")
    verify: Any = ssl.create_default_context(cafile=ca) if ca else True
    ip = env.get("TINVEST_CONNECT_IP")
    if not ip:
        return httpx.HTTPTransport(verify=verify) if ca else None
    local = None
    if interface := env.get("TINVEST_BIND_INTERFACE"):
        local = subprocess.run(["/usr/sbin/ipconfig", "getifaddr", interface], capture_output=True, text=True, timeout=2).stdout.strip()
    return PinnedTransport(ip, verify, local or None)


class TInvestSandboxBroker:
    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None, commission_bps: Decimal = Decimal(0)) -> None:
        if not token:
            raise ValueError("Нужен токен песочницы")
        self.http = httpx.Client(transport=transport, timeout=15, trust_env=False, headers={"Authorization": f"Bearer {token}"})
        self.commission_bps = commission_bps
        self._requests: dict[str, OrderRequest] = {}

    def __repr__(self) -> str:
        return "TInvestSandboxBroker()"

    def _call(self, service: str, method: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.http.post(f"{REST}.{service}/{method}", json=body)
        if response.status_code != 200:
            if '"description":"30057"' in response.text.replace(" ", ""):
                raise DuplicateOrder(method)
            raise RuntimeError(f"{method}: HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    def accounts(self) -> list[dict[str, Any]]:
        return self._call("SandboxService", "GetSandboxAccounts", {}).get("accounts", [])

    def get_positions(self, account: str) -> Portfolio:
        data = self._call("SandboxService", "GetSandboxPositions", {"accountId": account})
        cash = sum((from_quotation(m) for m in data.get("money", []) if m.get("currency") == "rub"), Decimal(0))
        positions, reserved = {}, {}
        for row in data.get("securities", []):
            uid = f"{row['ticker']}_{row['classCode']}"
            positions[uid] = int(row.get("balance", 0)) + int(row.get("blocked", 0))
            reserved[uid] = int(row.get("blocked", 0))
        return Portfolio(account, rub(cash), positions, reserved)

    def get_market_data(self, ticker: str) -> MarketSnapshot:
        book = self._call("MarketDataService", "GetOrderBook", {"instrumentId": ticker, "depth": 20})
        status = self._call("MarketDataService", "GetTradingStatus", {"instrumentId": ticker})

        def levels(rows: list[dict[str, Any]]) -> tuple[BookLevel, ...]:
            return tuple(BookLevel(from_quotation(r["price"]), int(r["quantity"])) for r in rows)

        bids, asks = levels(book.get("bids", [])), levels(book.get("asks", []))
        return MarketSnapshot(ticker, 0, from_quotation(book.get("lastPrice")), bids[0].price if bids else None,
                              asks[0].price if asks else None, bids, asks, not status.get("limitOrderAvailableFlag", False))

    def post_order(self, request: OrderRequest) -> OrderView:
        """Ключ запроса выводится из нашего ключа. Повтор не создаёт вторую заявку: сервер отвечает ошибкой 30057,
        и адаптер читает состояние уже принятой заявки по этому же ключу."""
        if request.side is not Side.SELL:
            raise ValueError("Страж только продаёт")
        self._requests[request.client_order_id] = request
        try:
            data = self._call("SandboxService", "PostSandboxOrder", {
                "accountId": request.account, "instrumentId": request.ticker, "quantity": str(request.qty),
                "price": quotation(request.limit_price), "direction": "ORDER_DIRECTION_SELL", "orderType": "ORDER_TYPE_LIMIT",
                "timeInForce": "TIME_IN_FORCE_DAY", "priceType": "PRICE_TYPE_POINT", "orderId": request_key(request.client_order_id)})
        except DuplicateOrder:
            return self.get_order_state(request.account, request.client_order_id)
        return self._view(request, data)

    def cancel_order(self, account: str, client_order_id: str) -> OrderView:
        self._call("SandboxService", "CancelSandboxOrder", {"accountId": account, "orderId": request_key(client_order_id),
                                                            "orderIdType": "ORDER_ID_TYPE_REQUEST"})
        return self.get_order_state(account, client_order_id)

    def get_order_state(self, account: str, client_order_id: str) -> OrderView:
        return self._view(self._requests[client_order_id], self._state(account, client_order_id))

    def _state(self, account: str, client_order_id: str) -> dict[str, Any]:
        return self._call("SandboxService", "GetSandboxOrderState", {"accountId": account, "orderId": request_key(client_order_id),
                                                                      "orderIdType": "ORDER_ID_TYPE_REQUEST"})

    def get_executions(self, account: str, client_order_id: str | None = None) -> list[Execution]:
        """Метод песочницы может не вернуть этапы исполнения: тогда сделка видна только итогом заявки."""
        ids = [client_order_id] if client_order_id else list(self._requests)
        out: list[Execution] = []
        for cid in ids:
            data = self._state(account, cid)
            request = self._requests[cid]
            for i, stage in enumerate(data.get("stages") or []):
                qty = int(stage.get("quantity", 0))
                price = from_quotation(stage.get("price"))
                gross = rub(price * qty)
                out.append(Execution(f"{cid}#{i}", cid, account, request.ticker, qty, price, gross,
                                     commission(gross, self.commission_bps), 0))
        return out

    @staticmethod
    def _view(request: OrderRequest, data: dict[str, Any]) -> OrderView:
        status = STATUS.get(data.get("executionReportStatus", ""), OrderStatus.PENDING)
        return OrderView(request.client_order_id, request.account, request.ticker, request.side, request.qty, request.limit_price,
                         status, filled_qty=int(data.get("lotsExecuted", 0)), gross=from_quotation(data.get("executedOrderPrice")))


class McpReader:
    """Чтение через MCP. Торговые инструменты здесь запрещены до обращения к сети."""

    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self.http = httpx.Client(transport=transport, timeout=30, trust_env=False,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"})
        self._session: str | None = None
        self._id = 0

    def __repr__(self) -> str:
        return "McpReader()"

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        headers = {"Mcp-Session-Id": self._session} if self._session else {}
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body.update(id=self._id, params=params)
        response = self.http.post(MCP, json=body, headers=headers)
        response.raise_for_status()
        self._session = response.headers.get("mcp-session-id", self._session)
        if params is None:
            return {}
        for line in response.text.splitlines() or [response.text]:
            chunk = line.removeprefix("data:").strip()
            if chunk.startswith("{"):
                message = json.loads(chunk)
                if message.get("id") == self._id:
                    return message
        raise RuntimeError("В ответе MCP нет результата")

    def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool not in READ_ONLY_TOOLS:
            raise PermissionError(f"Инструмент {tool} не разрешён")
        if self._session is None:
            self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "strazh-platform", "version": "0.1"}})
            self._rpc("notifications/initialized")
        result = self._rpc("tools/call", {"name": tool, "arguments": args})["result"]
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        return json.loads(next(c["text"] for c in result["content"] if c.get("type") == "text"))
