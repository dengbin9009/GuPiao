from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest


def sample_recent_rows():
    start = datetime(2026, 6, 24, 14, 45)
    rows = []
    prices = [10.00, 10.02, 10.04, 10.06, 10.08]
    for index, price in enumerate(prices):
        rows.append(
            {
                "symbol": "000001.SZ",
                "timestamp": (start + timedelta(minutes=index)).isoformat(),
                "open": price - 0.01,
                "high": price + 0.02,
                "low": price - 0.02,
                "close": price,
                "volume": 100000 + index * 1000,
                "amount": price * (100000 + index * 1000),
                "provider": "cache-provider",
            }
        )
    next_start = datetime(2026, 6, 25, 9, 35)
    next_prices = [10.20, 10.18, 10.16]
    for index, price in enumerate(next_prices):
        rows.append(
            {
                "symbol": "000001.SZ",
                "timestamp": (next_start + timedelta(minutes=index)).isoformat(),
                "open": price - 0.01,
                "high": price + 0.02,
                "low": price - 0.02,
                "close": price,
                "volume": 110000 + index * 1000,
                "amount": price * (110000 + index * 1000),
                "provider": "cache-provider",
            }
        )
    return rows


def sample_hourly_rows():
    return [
        {
            "symbol": "000001.SZ",
            "timestamp": "2026-06-24T14:00:00",
            "open": 10.00,
            "high": 10.10,
            "low": 9.98,
            "close": 10.05,
            "volume": 300000,
            "amount": 3015000,
            "provider": "hour-provider",
        },
        {
            "symbol": "000001.SZ",
            "timestamp": "2026-06-24T15:00:00",
            "open": 10.08,
            "high": 10.18,
            "low": 10.02,
            "close": 10.15,
            "volume": 280000,
            "amount": 2842000,
            "provider": "hour-provider",
        },
        {
            "symbol": "000001.SZ",
            "timestamp": "2026-06-25T09:00:00",
            "open": 10.18,
            "high": 10.25,
            "low": 10.12,
            "close": 10.20,
            "volume": 320000,
            "amount": 3264000,
            "provider": "hour-provider",
        },
    ]


def test_recent_backtest_uses_cache_when_coverage_is_complete(tmp_path: Path):
    from app.market_cache import write_bar_cache
    from app.recent_overnight_backtest import run_recent_overnight_backtest

    cache_root = tmp_path / "market"
    write_bar_cache(cache_root / "000001.SZ-1m.parquet", sample_recent_rows())

    result = run_recent_overnight_backtest(
        symbol="000001.SZ",
        entry_date="2026-06-24",
        exit_date="2026-06-25",
        cache_root=cache_root,
        provider=None,
    )

    assert result["symbol"] == "000001.SZ"
    assert result["data_source"] == "cache"
    assert result["entry"]["timestamp"].startswith("2026-06-24T14:45")
    assert result["exit"]["timestamp"].startswith("2026-06-25T09:35")
    assert result["quantity"] == 1000
    assert result["net_pnl"] > 0


def test_recent_backtest_fetches_when_cache_is_incomplete(tmp_path: Path):
    from app.market_cache import write_bar_cache
    from app.recent_overnight_backtest import run_recent_overnight_backtest

    cache_root = tmp_path / "market"
    write_bar_cache(cache_root / "000001.SZ-1m.parquet", sample_recent_rows()[:3])

    class Provider:
        name = "online-provider"

        def bars(self, *, symbol, timeframe, start=None, end=None):
            assert symbol == "000001.SZ"
            assert timeframe == "1m"
            return sample_recent_rows()

    result = run_recent_overnight_backtest(
        symbol="000001.SZ",
        entry_date="2026-06-24",
        exit_date="2026-06-25",
        cache_root=cache_root,
        provider=Provider(),
    )

    assert result["data_source"] == "provider+cache"
    assert result["coverage"]["fetched"] is True


def test_recent_backtest_fails_when_coverage_still_missing(tmp_path: Path):
    from app.market_cache import write_bar_cache
    from app.recent_overnight_backtest import run_recent_overnight_backtest

    cache_root = tmp_path / "market"
    write_bar_cache(cache_root / "000001.SZ-1m.parquet", sample_recent_rows()[:2])

    class Provider:
        name = "broken-provider"

        def bars(self, *, symbol, timeframe, start=None, end=None):
            return sample_recent_rows()[:4]

    with pytest.raises(ValueError, match="1m 数据覆盖不足"):
        run_recent_overnight_backtest(
            symbol="000001.SZ",
            entry_date="2026-06-24",
            exit_date="2026-06-25",
            cache_root=cache_root,
            provider=Provider(),
        )


def test_recent_backtest_rejects_demo_generated_bars(tmp_path: Path):
    from app.market_cache import write_bar_cache
    from app.recent_overnight_backtest import run_recent_overnight_backtest

    cache_root = tmp_path / "market"
    rows = sample_recent_rows()
    for row in rows:
        row["provider"] = "akshare-demo"
    write_bar_cache(cache_root / "000001.SZ-1m.parquet", rows)

    with pytest.raises(ValueError, match="演示1m数据"):
        run_recent_overnight_backtest(
            symbol="000001.SZ",
            entry_date="2026-06-24",
            exit_date="2026-06-25",
            cache_root=cache_root,
            provider=None,
        )


def test_recent_backtest_falls_back_to_next_minute_provider(tmp_path: Path):
    from app.recent_overnight_backtest import run_recent_overnight_backtest
    from app.market_data import MarketDataError, ProviderRouter

    cache_root = tmp_path / "market"

    class BrokenMinuteProvider:
        name = "broken-minute"
        capabilities = frozenset({"minute"})

        def health(self):
            return True, None

        def bars(self, *, symbol, timeframe, start=None, end=None):
            raise MarketDataError("上游断开连接")

    class WorkingMinuteProvider:
        name = "working-minute"
        capabilities = frozenset({"minute"})

        def health(self):
            return True, None

        def bars(self, *, symbol, timeframe, start=None, end=None):
            return sample_recent_rows()

    result = run_recent_overnight_backtest(
        symbol="000001.SZ",
        entry_date="2026-06-24",
        exit_date="2026-06-25",
        cache_root=cache_root,
        provider=ProviderRouter([BrokenMinuteProvider(), WorkingMinuteProvider()]),
    )

    assert result["data_source"] == "provider+cache"
    assert result["coverage"]["fetched"] is True


def test_recent_backtest_accepts_router_with_mootdx_fallback(tmp_path: Path):
    from app.market_data import MarketDataError, ProviderRouter
    from app.recent_overnight_backtest import run_recent_overnight_backtest

    class BrokenMinuteProvider:
        name = "akshare"
        capabilities = frozenset({"minute"})

        def health(self):
            return True, None

        def bars(self, **_):
            raise MarketDataError("AKShare K 线获取失败")

    class MootdxMinuteProvider:
        name = "mootdx"
        capabilities = frozenset({"minute"})

        def health(self):
            return True, None

        def bars(self, *, symbol, timeframe, start=None, end=None):
            return sample_recent_rows()

    result = run_recent_overnight_backtest(
        symbol="000001.SZ",
        entry_date="2026-06-24",
        exit_date="2026-06-25",
        cache_root=tmp_path / "market",
        provider=ProviderRouter([BrokenMinuteProvider(), MootdxMinuteProvider()]),
    )

    assert result["data_source"] == "provider+cache"


def test_recent_backtest_falls_back_to_hourly_when_minute_unavailable(tmp_path: Path):
    from app.market_data import MarketDataError, ProviderRouter
    from app.recent_overnight_backtest import run_recent_overnight_backtest

    class BrokenMinuteProvider:
        name = "akshare"
        capabilities = frozenset({"minute", "hour"})

        def health(self):
            return True, None

        def bars(self, *, timeframe, **_):
            if timeframe == "1m":
                raise MarketDataError("分钟线权限不足")
            return sample_hourly_rows()

    result = run_recent_overnight_backtest(
        symbol="000001.SZ",
        entry_date="2026-06-24",
        exit_date="2026-06-25",
        cache_root=tmp_path / "market",
        provider=ProviderRouter([BrokenMinuteProvider()]),
    )

    assert result["timeframe_used"] == "60m"
    assert result["data_source"] == "provider+cache"
    assert result["entry"]["timestamp"].startswith("2026-06-24T15:00")
    assert result["exit"]["timestamp"].startswith("2026-06-25T09:00")


def test_recent_backtest_reports_hourly_failure_after_minute_failure(tmp_path: Path):
    from app.market_data import MarketDataError, ProviderRouter
    from app.recent_overnight_backtest import run_recent_overnight_backtest

    class BrokenProvider:
        name = "broken"
        capabilities = frozenset({"minute", "hour"})

        def health(self):
            return True, None

        def bars(self, *, timeframe, **_):
            raise MarketDataError(f"{timeframe} 上游不可用")

    with pytest.raises(ValueError, match="60m 也不可用"):
        run_recent_overnight_backtest(
            symbol="000001.SZ",
            entry_date="2026-06-24",
            exit_date="2026-06-25",
            cache_root=tmp_path / "market",
            provider=ProviderRouter([BrokenProvider()]),
        )
