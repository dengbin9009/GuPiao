from __future__ import annotations

from app.brokers import BrokerHealth


class MockQMTAdapter:
    name = "QMT"

    def health(self):
        return BrokerHealth(True, "ok", ("accounts", "orders", "positions"))

    def query_accounts(self):
        return [{"account_id": "1234567890", "alias": "QMT 主账户", "currency": "CNY", "read_only": False, "markets": ["A_SHARE"]}]

    def place_order(self, order):
        return {"broker_order_id": "QMT-1", "status": "submitted", "symbol": order["symbol"], "quantity": order["quantity"]}


class MockPTradeAdapter:
    name = "PTrade"

    def health(self):
        return BrokerHealth(True, "ok", ("accounts", "orders", "positions"))

    def query_accounts(self):
        return [{"account_no": "99887766", "alias": "PTrade 云账户", "currency": "CNY", "read_only": False, "markets": ["A_SHARE"]}]

    def place_order(self, order):
        return {"broker_order_id": "PTRADE-1", "status": "submitted", "symbol": order["symbol"], "quantity": order["quantity"]}


class MockFutuAdapter:
    name = "Futu OpenD"

    def health(self):
        return BrokerHealth(True, "ok", ("accounts", "orders", "quotes"))

    def query_accounts(self):
        return [{"account_id": "66554433", "alias": "Futu 模拟账户", "currency": "HKD", "read_only": True, "markets": ["HK", "US"]}]

    def place_order(self, order):
        return {"broker_order_id": "FUTU-1", "status": "submitted", "symbol": order["symbol"], "quantity": order["quantity"]}


def main() -> None:
    adapters = [MockQMTAdapter(), MockPTradeAdapter(), MockFutuAdapter()]
    for adapter in adapters:
        health = adapter.health()
        assert health.healthy
        assert health.capabilities
        accounts = adapter.query_accounts()
        assert accounts
        order = adapter.place_order({"symbol": "000001.SZ", "quantity": 100})
        assert order["status"] == "submitted"
        assert order["quantity"] == 100
    print("adapter_contracts_ok")


if __name__ == "__main__":
    main()
