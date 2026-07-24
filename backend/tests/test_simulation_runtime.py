from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import (
    Administrator,
    BrokerGateway,
    DataSourceState,
    LiveTradingAccount,
    RiskSettings,
    SimulationAccount,
    SimulationAccountLedger,
    StrategyConfig,
    StrategySchedule,
    Stock,
    WatchlistItem,
)
from app.security import verify_password


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
    assert all(stock.tail_30m_return is None for stock in stocks)
    assert all(stock.factor_updated_at is None for stock in stocks)
    assert data_sources
    assert "mootdx" in {source.provider for source in data_sources}
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


def test_prepare_simulation_runtime_syncs_admin_password_from_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.services import seed_database
    from app.simulation_runtime import prepare_simulation_runtime

    db_url = f"sqlite:///{tmp_path / 'credentials.db'}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("GUPIAO_ADMIN_PASSWORD", "old-password")
    with Session(engine) as db:
        seed_database(
            db,
            Settings(database_url=db_url),
        )
        monkeypatch.setenv("GUPIAO_ADMIN_PASSWORD", "new-password")
        prepare_simulation_runtime(
            db,
            Settings(
                database_url=db_url,
                live_enabled=False,
                broker_adapter="simulation",
            ),
        )
        admin = db.scalar(select(Administrator).where(Administrator.username == "admin"))

        assert verify_password("new-password", admin.password_hash)
        assert not verify_password("old-password", admin.password_hash)


def test_prepare_simulation_runtime_forces_database_live_state_closed(tmp_path: Path):
    from app.services import seed_database
    from app.simulation_runtime import prepare_simulation_runtime

    db_url = f"sqlite:///{tmp_path / 'force-live-closed.db'}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(
            database_url=db_url,
            live_enabled=False,
            broker_adapter="simulation",
        )
        seed_database(db, settings)
        risk = db.scalar(select(RiskSettings).where(RiskSettings.mode == "LIVE"))
        simulation_risk = db.scalar(
            select(RiskSettings).where(RiskSettings.mode == "SIMULATION")
        )
        gateway = db.scalar(select(BrokerGateway).where(BrokerGateway.type == "qmt"))
        risk.live_enabled = True
        simulation_risk.emergency_stop_enabled = True
        account = LiveTradingAccount(
            broker="test",
            account_alias="测试账户",
            account_no_masked="******0001",
            gateway_id=gateway.id,
            enabled=True,
            read_only=False,
            market_permissions=["A_SHARE"],
            account_capabilities=["orders"],
        )
        db.add(account)
        db.commit()

        prepare_simulation_runtime(db, settings)

        assert risk.live_enabled is False
        assert simulation_risk.emergency_stop_enabled is False
        assert account.enabled is False
        assert account.read_only is True


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
    assert "MARKET_DATA_STALE_AFTER_SECONDS=60" in script
    assert "CORPORATE_EVENT_STALE_AFTER_SECONDS=1800" in script
    assert "MARKET_DATA_STALE_AFTER_SECONDS=86400" not in script
    assert "CORPORATE_EVENT_STALE_AFTER_SECONDS=172800" not in script
    assert 'export MARKET_DATA_STALE_AFTER_SECONDS="${GUPIAO_OBSERVE_MARKET_STALE_SECONDS:-60}"' in script
    assert 'export CORPORATE_EVENT_STALE_AFTER_SECONDS="${GUPIAO_OBSERVE_EVENT_STALE_SECONDS:-1800}"' in script
    assert "GUPIAO_OBSERVE_INITIAL_CASH:-100000" in script
    assert 'export SIMULATION_INITIAL_CASH="$OBSERVE_INITIAL_CASH"' in script
    assert "kill -0" in script
    assert "stop_managed_pid" in script
    assert 'case "$cwd/" in' in script
    assert "Refusing to stop PID" in script
    assert "GUPIAO_ATTACHED" in script
    assert "GUPIAO_VERIFY_SECONDS" in script
    assert "GUPIAO_STARTUP_TIMEOUT_SECONDS" in script
    assert "wait_for_http" in script
    assert "sleep 4" not in script
    assert "verification passed" in script.lower()
    assert "--strictPort" in script
    assert script.index("trap cleanup_started EXIT INT TERM") < script.index(
        'start_bg "$BACKEND_PID_FILE"'
    )
    assert "trap - EXIT INT TERM" in script
    assert "update data_source_states" not in script.lower()


def test_background_processes_do_not_compete_for_runtime_bootstrap():
    root = Path(__file__).resolve().parents[2]
    process_sources = (
        root / "backend/app/worker.py",
        root / "backend/app/scheduler_runner.py",
        root / "backend/app/trading_agents/worker.py",
        root / "backend/app/quant_strategies/worker.py",
    )

    for source_path in process_sources:
        source = source_path.read_text(encoding="utf-8")
        assert "Base.metadata.create_all" not in source
        assert "apply_runtime_migrations()" not in source
        assert "seed_strategy_runtimes(db" not in source
        assert "wait_for_runtime_database()" in source


def test_background_runtime_waits_for_api_database_without_writing(tmp_path: Path):
    from app.runtime_bootstrap import wait_for_runtime_database
    from app.services import seed_database

    database_url = f"sqlite:///{tmp_path / 'wait-for-api.db'}"
    engine = create_engine(database_url)
    settings = Settings(
        database_url=database_url,
        live_enabled=False,
        broker_adapter="simulation",
    )
    sleeps = []

    def initialize_after_first_poll(seconds: float) -> None:
        sleeps.append(seconds)
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            seed_database(db, settings)

    wait_for_runtime_database(
        session_factory=lambda: Session(engine),
        sleep=initialize_after_first_poll,
        poll_seconds=0,
    )

    assert sleeps == [0]
    with Session(engine) as db:
        assert db.scalar(select(StrategyConfig.id)) is None


def test_local_and_compose_start_workers_only_after_backend_is_healthy():
    root = Path(__file__).resolve().parents[2]
    script = (root / "start_tonight_observe.sh").read_text(encoding="utf-8")
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")

    health_wait = 'wait_for_http "backend" "http://127.0.0.1:8000/api/health"'
    assert script.index(health_wait) < script.index(
        'start_bg "$WORKER_PID_FILE"'
    )
    for service in (
        "worker",
        "scheduler",
        "tradingagents-worker",
        "quant-strategy-worker",
    ):
        match = re.search(
            rf"(?ms)^  {re.escape(service)}:\n(?P<body>.*?)(?=^  \S|\Z)",
            compose,
        )
        assert match is not None
        section = match.group("body")
        assert "backend:" in section
        assert "condition: service_healthy" in section
    assert 'echo "Administrator password: $GENERATED_PASSWORD"' not in script
