from __future__ import annotations


def test_recent_overnight_script_uses_router_without_select(monkeypatch):
    from app import recent_overnight_backtest
    from scripts import backtest_recent_overnight as script

    class DummyRouter:
        pass

    captured: dict[str, object] = {}

    def fake_market_router():
        captured["router_called"] = True
        return DummyRouter()

    def fake_run_recent_overnight_backtest(**kwargs):
        captured.update(kwargs)
        return {
            "symbol": "000001.SZ",
            "entry": {"timestamp": "2026-06-24T14:45:00", "price": 10.0},
            "exit": {"timestamp": "2026-06-25T09:35:00", "price": 10.2},
            "net_pnl": 100.0,
            "return_pct": 0.01,
        }

    monkeypatch.setattr(script, "market_router", fake_market_router)
    monkeypatch.setattr(script, "run_recent_overnight_backtest", fake_run_recent_overnight_backtest)
    monkeypatch.setattr(
        "sys.argv",
        [
            "backtest_recent_overnight.py",
            "--symbol",
            "000001.SZ",
            "--entry-date",
            "2026-06-24",
            "--exit-date",
            "2026-06-25",
        ],
    )

    script.main()

    assert captured["router_called"] is True
    assert isinstance(captured["provider"], DummyRouter)
