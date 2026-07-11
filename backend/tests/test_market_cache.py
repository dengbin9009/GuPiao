from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys


def sample_rows():
    start = datetime(2026, 6, 23, 14, 30)
    return [
        {
            "timestamp": (start + timedelta(minutes=index)).isoformat(),
            "open": 10 + index * 0.01,
            "high": 10 + index * 0.02,
            "low": 10 + index * 0.005,
            "close": 10 + index * 0.015,
            "volume": 100000 + index * 100,
            "amount": 1000000 + index * 1000,
        }
        for index in range(5)
    ]


def test_bar_cache_writes_parquet_and_reads_back(tmp_path: Path):
    from app.market_cache import write_bar_cache, read_bar_cache

    rows = sample_rows()
    target = tmp_path / "cache.parquet"
    write_bar_cache(target, rows)
    cached = read_bar_cache(target)

    assert target.exists()
    assert len(cached) == 5
    assert cached[0]["timestamp"] == rows[0]["timestamp"]


def test_text_cache_fallback_preserves_provider(tmp_path: Path, monkeypatch):
    from app.market_cache import read_bar_cache, write_bar_cache

    rows = sample_rows()
    rows[0]["symbol"] = "000001.SZ"
    rows[0]["provider"] = "akshare-demo"
    target = tmp_path / "cache.parquet"
    monkeypatch.setitem(sys.modules, "pandas", None)

    write_bar_cache(target, rows[:1])
    cached = read_bar_cache(target)

    assert cached[0]["symbol"] == "000001.SZ"
    assert cached[0]["provider"] == "akshare-demo"


def test_quote_staleness_uses_configured_threshold():
    from app.market_cache import quote_is_stale

    current = datetime(2026, 6, 23, 14, 50)

    assert quote_is_stale(current - timedelta(seconds=16), current=current, stale_after_seconds=15)
    assert not quote_is_stale(current - timedelta(seconds=5), current=current, stale_after_seconds=15)
    assert quote_is_stale(current + timedelta(minutes=1), current=current, stale_after_seconds=15)


def test_minute_bar_cache_refresh_writes_named_file(tmp_path: Path):
    from app.market_cache import refresh_bar_cache

    rows = sample_rows()

    class MinuteProvider:
        def bars(self, *, symbol, timeframe, start=None, end=None):
            assert symbol == "000001.SZ"
            assert timeframe == "1m"
            return rows

    target = refresh_bar_cache(tmp_path, MinuteProvider(), symbol="000001.SZ", timeframe="1m")

    assert target.name == "000001.SZ-1m.parquet"
    assert target.exists()


def test_bar_cache_coverage_detects_missing_exit_window(tmp_path: Path):
    from app.market_cache import bar_cache_coverage, write_bar_cache

    rows = sample_rows()
    target = tmp_path / "000001.SZ-1m.parquet"
    write_bar_cache(target, rows)

    coverage = bar_cache_coverage(
        target,
        entry_start="2026-06-23T14:30:00",
        entry_end="2026-06-23T14:34:00",
        exit_start="2026-06-24T09:35:00",
        exit_end="2026-06-24T09:40:00",
    )

    assert coverage["entry_covered"] is True
    assert coverage["exit_covered"] is False
