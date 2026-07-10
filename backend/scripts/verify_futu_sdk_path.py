from __future__ import annotations

import sys
from types import SimpleNamespace

from app.brokers import FutuOpenDAdapter


class FakeFrame:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient: str = "records"):
        assert orient == "records"
        return list(self._rows)


class FakeTradeContext:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.unlocked = False

    def unlock_trade(self, password: str):
        self.unlocked = password == "secret"
        return (0, "ok") if self.unlocked else (-1, "bad password")

    def get_acc_list(self):
        return 0, FakeFrame(
            [
                {
                    "acc_id": 12345678,
                    "acc_type": "STOCK",
                    "currency": "HKD",
                }
            ]
        )

    def place_order(self, **kwargs):
        return 0, FakeFrame(
            [
                {
                    "order_id": "FUTU-ORDER-1",
                    "order_status": "submitted",
                    "kwargs": kwargs,
                }
            ]
        )

    def close(self):
        self.closed = True


def main() -> None:
    fake_module = SimpleNamespace(
        RET_OK=0,
        TrdMarket=SimpleNamespace(HK="HK", US="US"),
        TrdEnv=SimpleNamespace(SIMULATE="SIMULATE", REAL="REAL"),
        SecurityFirm=SimpleNamespace(FUTUSECURITIES="FUTUSECURITIES"),
        TrdSide=SimpleNamespace(BUY="BUY", SELL="SELL"),
        OrderType=SimpleNamespace(NORMAL="NORMAL"),
        OpenSecTradeContext=FakeTradeContext,
    )
    original = sys.modules.get("futu")
    sys.modules["futu"] = fake_module
    try:
        adapter = FutuOpenDAdapter(
            "127.0.0.1",
            11111,
            trd_market="HK",
            security_firm="FUTUSECURITIES",
            trd_env="SIMULATE",
            unlock_password="secret",
        )
        accounts = adapter.query_accounts()
        assert accounts[0]["account_id"] == "12345678"
        assert accounts[0]["currency"] == "HKD"
        result = adapter.place_order({"symbol": "00700.HK", "quantity": 100, "side": "buy", "acc_id": 12345678})
        assert result["broker_order_id"] == "FUTU-ORDER-1"
        assert result["status"] == "submitted"
        print("futu_sdk_path_ok")
    finally:
        if original is None:
            sys.modules.pop("futu", None)
        else:
            sys.modules["futu"] = original


if __name__ == "__main__":
    main()
