from __future__ import annotations

from pathlib import Path
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import (
    AccountSnapshot,
    BacktestTrade,
    DataSourceState,
    Fill,
    Order,
    Position,
    RiskSettings,
    SimulationAccount,
    SimulationAccountLedger,
    Stock,
    WatchlistItem,
    StrategyConfig,
    StrategyDefinition,
    StrategyRun,
    now,
)
from app.security import create_session, hash_password, read_session, verify_password
from app.services import (
    create_backtest,
    execute_simulation_exit,
    execute_simulation_strategy,
    adjust_simulation_cash,
    release_simulation_cash,
    scan_plugins,
    seed_database,
    validate_strategy_parameters,
)


@pytest.fixture()
def db(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed_database(session, Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}"))
        yield session


def test_model_count_and_seed(db: Session):
    assert len(Base.metadata.tables) == 26
    account = db.scalar(select(SimulationAccount))
    definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
    assert account is not None and account.initial_cash == 10000
    assert definition is not None and definition.required_timeframes == ["1m"]


def test_seed_uses_configurable_risk_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'risk.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setenv("SIMULATION_MAX_ORDER_NOTIONAL_ABS", "1234")
    monkeypatch.setenv("LIVE_MAX_DAILY_ORDERS", "9")
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'risk.db'}",
    )
    with Session(engine) as session:
        seed_database(session, settings)
        simulation = session.scalar(select(RiskSettings).where(RiskSettings.mode == "SIMULATION"))
        live = session.scalar(select(RiskSettings).where(RiskSettings.mode == "LIVE"))
        assert simulation.max_order_notional_abs == 1234
        assert live.max_daily_orders == 9


def test_password_and_signed_session():
    encoded = hash_password("secret")
    assert verify_password("secret", encoded)
    assert not verify_password("wrong", encoded)
    token = create_session("admin", "key", ttl_seconds=60)
    assert read_session(token, "key")["sub"] == "admin"
    assert read_session(token, "wrong") is None


def test_simulation_run_creates_fill_position_and_t1_lock(db: Session):
    definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
    stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    stock.change_pct = 0.049
    stock.turnover_amount = 900_000_000
    stock.last_price = 10.01
    stock.quote_updated_at = now()
    config = StrategyConfig(
        strategy_definition_id=definition.id,
        name="测试模拟策略",
        mode="SIMULATION",
        parameters={},
    )
    db.add(config)
    db.commit()
    run = execute_simulation_strategy(db, config)
    assert run.status == "completed"
    assert db.scalar(select(Order)) is not None
    assert db.scalar(select(Fill)) is not None
    position = db.scalar(select(Position))
    assert position is not None
    assert position.quantity >= 100
    assert position.quantity % 100 == 0
    assert position.available_quantity == 0
    assert run.summary["exit_plan"]["trigger_type"] == "exit_evaluation"
    assert run.summary["exit_plan"]["run_time"] == "09:35"
    assert db.scalar(select(AccountSnapshot).order_by(AccountSnapshot.id.desc())) is not None
    ledger_events = [item.event_type for item in db.scalars(select(SimulationAccountLedger).order_by(SimulationAccountLedger.id))]
    assert ledger_events == ["initialize", "order_freeze", "fill"]


def test_simulation_run_selects_best_universe_candidate_not_first_watchlist(db: Session):
    definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
    leading = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    stronger = db.scalar(select(Stock).where(Stock.symbol == "300750.SZ"))
    stronger.change_pct = 0.041
    stronger.turnover_amount = 900_000_000
    stronger.last_price = 10.56
    stronger.quote_updated_at = now()
    leading.change_pct = 0.018
    leading.turnover_amount = 200_000_000
    leading.quote_updated_at = now()
    db.add(WatchlistItem(stock_id=leading.id, note="自选第一只"))
    db.commit()

    config = StrategyConfig(
        strategy_definition_id=definition.id,
        name="股票池优先测试",
        mode="SIMULATION",
        parameters={},
    )
    db.add(config)
    db.commit()

    run = execute_simulation_strategy(db, config)

    order = db.scalar(select(Order).order_by(Order.id.desc()))
    assert run.status == "completed"
    assert order is not None
    assert order.stock_id == stronger.id
    assert run.summary["selected_symbol"] == "300750.SZ"


def test_live_run_without_selected_account_fails_closed(db: Session):
    definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
    live_risk = db.scalar(select(RiskSettings).where(RiskSettings.mode == "LIVE"))
    live_risk.live_enabled = True
    config = StrategyConfig(
        strategy_definition_id=definition.id,
        name="测试真实盘策略",
        mode="LIVE",
        parameters={},
    )
    db.add(config)
    db.commit()

    run = execute_simulation_strategy(db, config)

    assert run.status == "failed"
    assert "真实盘账户" in run.error_message
    assert db.scalar(select(Order).where(Order.mode == "LIVE")) is None


def test_simulation_exit_sells_available_next_session_position(db: Session):
    definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
    stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    stock.change_pct = 0.049
    stock.turnover_amount = 900_000_000
    stock.last_price = 10.01
    stock.quote_updated_at = now()
    config = StrategyConfig(
        strategy_definition_id=definition.id,
        name="测试隔夜退出",
        mode="SIMULATION",
        parameters={},
    )
    db.add(config)
    db.commit()
    execute_simulation_strategy(db, config)
    position = db.scalar(select(Position))
    position.available_quantity = position.quantity
    db.commit()

    run = execute_simulation_exit(db, config)

    db.refresh(position)
    assert run.status == "completed"
    assert position.quantity == 0
    assert len(list(db.scalars(select(Order)))) == 2
    sell_fill = db.scalars(select(Fill).order_by(Fill.id.desc())).first()
    assert sell_fill.stamp_tax > 0


def test_overnight_strategy_rejects_stale_corporate_events(db: Session):
    definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
    source = db.scalar(select(DataSourceState).where(DataSourceState.provider == "cninfo"))
    stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    stock.change_pct = 0.049
    stock.turnover_amount = 900_000_000
    stock.last_price = 10.01
    stock.quote_updated_at = now()
    source.last_checked_at = now() - timedelta(seconds=1801)
    config = StrategyConfig(
        strategy_definition_id=definition.id,
        name="测试过期事件数据",
        mode="SIMULATION",
        parameters={"event_risk_enabled": True},
    )
    db.add(config)
    db.commit()

    run = execute_simulation_strategy(db, config)

    assert run.summary["accepted"] == 0
    assert "公司事件" in run.summary["reason"]
    assert db.scalar(select(Order)) is None


def test_strategy_parameter_validation_rejects_wrong_type(db: Session):
    definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))

    with pytest.raises(ValueError, match="max_candidates"):
        validate_strategy_parameters(definition.parameter_schema, {"max_candidates": "3"})


def test_simulation_ledger_records_release_and_adjustment(db: Session):
    account = db.scalar(select(SimulationAccount))
    starting_cash = account.cash_balance
    account.available_cash -= 200
    account.frozen_cash += 200
    db.commit()

    release_simulation_cash(db, account, 200, "撤销测试订单")
    adjust_simulation_cash(db, account, 500, "管理员入金调整")

    events = [item.event_type for item in db.scalars(select(SimulationAccountLedger).order_by(SimulationAccountLedger.id))]
    assert events[-2:] == ["order_release", "adjustment"]
    assert account.frozen_cash == 0
    assert account.cash_balance == starting_cash + 500


def test_fill_and_ledger_are_append_only(db: Session):
    definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
    stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    stock.change_pct = 0.049
    stock.turnover_amount = 900_000_000
    stock.last_price = 10.01
    stock.quote_updated_at = now()
    config = StrategyConfig(strategy_definition_id=definition.id, name="审计测试", mode="SIMULATION", parameters={})
    db.add(config)
    db.commit()
    execute_simulation_strategy(db, config)
    fill = db.scalar(select(Fill))
    fill.commission = 0

    with pytest.raises(ValueError, match="append-only"):
        db.commit()


def test_backtest_requires_minute_data_and_records_trades(db: Session):
    definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
    with pytest.raises(ValueError, match="1m"):
        create_backtest(db, definition, {"timeframe": "1d"})
    run = create_backtest(db, definition, {"timeframe": "1m", "initial_cash": 10000})
    assert run.status == "completed"
    assert run.metrics["trade_count"] == 2
    assert len(list(db.scalars(select(BacktestTrade).where(BacktestTrade.backtest_run_id == run.id)))) == 2


def test_plugin_scanner_only_reads_literal_metadata(tmp_path: Path):
    (tmp_path / "valid.py").write_text(
        "STRATEGY_METADATA = {'key': 'demo', 'name': '演示', 'version': '1.0', 'parameter_schema': {'type': 'object', 'properties': {}}}\n",
        encoding="utf-8",
    )
    (tmp_path / "invalid.py").write_text("print('not executed')\n", encoding="utf-8")
    result = scan_plugins(str(tmp_path))
    assert len(result) == 2
    assert next(item for item in result if item["key"] == "demo")["validation_error"] is None
    assert next(item for item in result if item["key"] == "invalid")["validation_error"]


def test_plugin_runs_in_json_subprocess(tmp_path: Path):
    from app.plugins import run_plugin

    plugin = tmp_path / "signals.py"
    plugin.write_text(
        "STRATEGY_METADATA = {'key': 'demo', 'name': '演示', 'version': '1.0', 'parameter_schema': {'type': 'object', 'properties': {}}}\n"
        "def generate_signals(context):\n"
        "    return [{'symbol': context['symbol'], 'side': 'buy', 'quantity': 100, 'reason': 'test'}]\n",
        encoding="utf-8",
    )

    result = run_plugin(plugin, {"symbol": "000001.SZ"}, timeout_seconds=2)

    assert result[0]["symbol"] == "000001.SZ"
    assert result[0]["side"] == "buy"


def test_plugin_scan_syncs_disabled_strategy_definition(db: Session, tmp_path: Path):
    from app.services import sync_plugin_definitions

    (tmp_path / "demo.py").write_text(
        "STRATEGY_METADATA = {'key': 'plugin_demo', 'name': '插件演示', 'version': '1.0', 'parameter_schema': {'type': 'object', 'properties': {}}}\n",
        encoding="utf-8",
    )

    result = sync_plugin_definitions(db, str(tmp_path))

    definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "plugin_demo"))
    assert result[0]["validation_error"] is None
    assert definition is not None
    assert definition.type == "plugin"
    assert not definition.enabled
