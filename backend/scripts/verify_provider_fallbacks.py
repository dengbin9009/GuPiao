from __future__ import annotations

from app.market_data import ProviderRouter, TushareProvider


class MasterProvider:
    def __init__(self, name: str, healthy: bool, value):
        self.name = name
        self.capabilities = frozenset({"stock_master"})
        self._healthy = healthy
        self._value = value

    def health(self):
        return self._healthy, None if self._healthy else "offline"

    def stock_master(self):
        return self._value


def main() -> None:
    router = ProviderRouter(
        [
            MasterProvider("akshare", False, []),
            MasterProvider("tushare", True, [{"ts_code": "600519.SH", "name": "贵州茅台"}]),
        ]
    )
    result = router.call("stock_master", "stock_master")
    assert result.provider == "tushare"
    assert result.data[0]["name"] == "贵州茅台"

    provider = TushareProvider(token="")
    provider.client = type(
        "Client",
        (),
        {
            "trade_cal": lambda self, **_: [
                {"cal_date": "2026-06-23"},
                {"cal_date": "2026-06-24"},
            ]
        },
    )()
    provider.import_error = None
    assert provider.trading_days(start="2026-06-23", end="2026-06-24") == ["2026-06-23", "2026-06-24"]
    print("provider_fallbacks_ok")


if __name__ == "__main__":
    main()
