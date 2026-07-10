from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import (
    DataSourceState,
    SimulationAccount,
    SimulationAccountLedger,
    StrategyConfig,
    StrategySchedule,
    Stock,
    WatchlistItem,
)


def test_prepare_simulation_runtime_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.simulation_runtime import prepare_simulation_runtime

    monkeypatch.setenv("SIMULATION_INITIAL_CASH", "100000")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("BROKER_ADAPTER", "simulation")
    engine = create_engine(f"sqlite:///{tmp_path / 'runtime.db'}")
    Base.metadata.create_all(engine)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
        simulation_initial_cash=100_000,
        live_enabled=False,
        broker_adapter="simulation",
    )

    with Session(engine) as db:
        first = prepare_simulation_runtime(db, settings)
        second = prepare_simulation_runtime(db, settings)

        account = db.scalar(select(SimulationAccount))
        configs = list(db.scalars(select(StrategyConfig)))
        schedules = list(db.scalars(select(StrategySchedule)))
        watchlist = list(db.scalars(select(WatchlistItem)))
        stocks = list(db.scalars(select(Stock)))
        data_sources = list(db.scalars(select(DataSourceState)))
        adjustments = list(
            db.scalars(
                select(SimulationAccountLedger).where(
                    SimulationAccountLedger.event_type == "adjustment"
                )
            )
        )

    assert first == second
    assert account.initial_cash == 100_000
    assert account.total_asset == 100_000
    assert len(configs) == 1
    assert configs[0].mode == "SIMULATION"
    assert len(schedules) == 2
    assert all(item.enabled for item in schedules)
    assert len(watchlist) == 1
    assert len(adjustments) == 0
    assert stocks
    assert all(stock.last_price is None for stock in stocks)
    assert all(stock.change_pct is None for stock in stocks)
    assert all(stock.turnover_amount is None for stock in stocks)
    assert all(stock.quote_updated_at is None for stock in stocks)
    assert data_sources
    assert all(not source.healthy for source in data_sources)
    assert all(source.last_quote_at is None for source in data_sources)


def test_prepare_simulation_runtime_adjusts_existing_account_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.simulation_runtime import prepare_simulation_runtime
    from app.services import seed_database

    db_url = f"sqlite:///{tmp_path / 'existing.db'}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("SIMULATION_INITIAL_CASH", "10000")
    with Session(engine) as db:
        seed_database(db, Settings(database_url=db_url))

    monkeypatch.setenv("SIMULATION_INITIAL_CASH", "100000")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("BROKER_ADAPTER", "simulation")
    settings = Settings(database_url=db_url)
    with Session(engine) as db:
        prepare_simulation_runtime(db, settings)
        prepare_simulation_runtime(db, settings)
        account = db.scalar(select(SimulationAccount))
        adjustments = list(
            db.scalars(
                select(SimulationAccountLedger).where(
                    SimulationAccountLedger.event_type == "adjustment"
                )
            )
        )

    assert account.initial_cash == 100_000
    assert account.cash_balance == 100_000
    assert account.available_cash == 100_000
    assert account.total_asset == 100_000
    assert len(adjustments) == 1
    assert adjustments[0].amount == 90_000


@pytest.mark.parametrize(
    ("live_enabled", "broker_adapter"),
    [(True, "simulation"), (False, "qmt")],
)
def test_prepare_simulation_runtime_rejects_live_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_enabled: bool,
    broker_adapter: str,
):
    from app.simulation_runtime import prepare_simulation_runtime

    monkeypatch.setenv("LIVE_TRADING_ENABLED", str(live_enabled).lower())
    monkeypatch.setenv("BROKER_ADAPTER", broker_adapter)
    engine = create_engine(f"sqlite:///{tmp_path / 'unsafe.db'}")
    Base.metadata.create_all(engine)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'unsafe.db'}",
        live_enabled=live_enabled,
        broker_adapter=broker_adapter,
    )

    with Session(engine) as db, pytest.raises(RuntimeError, match="模拟盘"):
        prepare_simulation_runtime(db, settings)


def test_observe_start_script_is_relocatable_and_uses_runtime_bootstrap():
    root = Path(__file__).resolve().parents[2]
    script = (root / "start_tonight_observe.sh").read_text(encoding="utf-8")

    assert "BASH_SOURCE[0]" in script
    assert "/Users/dengbin/Code/github/GuPiao" not in script
    assert "scripts/prepare_simulation_runtime.py" in script
    assert script.index("scripts/prepare_simulation_runtime.py") < script.index("start_bg()")
    assert "LIVE_TRADING_ENABLED=false" in script
    assert "BROKER_ADAPTER=simulation" in script
    assert "MARKET_DATA_STALE_AFTER_SECONDS=15" in script
    assert "CORPORATE_EVENT_STALE_AFTER_SECONDS=1800" in script
    assert "MARKET_DATA_STALE_AFTER_SECONDS=86400" not in script
    assert "CORPORATE_EVENT_STALE_AFTER_SECONDS=172800" not in script
    assert 'export MARKET_DATA_STALE_AFTER_SECONDS="${GUPIAO_OBSERVE_MARKET_STALE_SECONDS:-15}"' in script
    assert 'export CORPORATE_EVENT_STALE_AFTER_SECONDS="${GUPIAO_OBSERVE_EVENT_STALE_SECONDS:-1800}"' in script
    assert "GUPIAO_OBSERVE_INITIAL_CASH:-100000" in script
    assert 'export SIMULATION_INITIAL_CASH="$OBSERVE_INITIAL_CASH"' in script
    assert "kill -0" in script
    assert "stop_managed_pid" in script
    assert 'case "$cwd/" in' in script
    assert "Refusing to stop PID" in script
    assert "GUPIAO_ATTACHED" in script
    assert "GUPIAO_VERIFY_SECONDS" in script
    assert "verification passed" in script.lower()
    assert "--strictPort" in script
    assert script.index("trap cleanup_started EXIT INT TERM") < script.index(
        'start_bg "$BACKEND_PID_FILE"'
    )
    assert "trap - EXIT INT TERM" in script
    assert "update data_source_states" not in script.lower()
    assert 'echo "Administrator password: $GENERATED_PASSWORD"' not in script
