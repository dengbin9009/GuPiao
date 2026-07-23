from __future__ import annotations

from pathlib import Path
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select, text
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
    StockEvent,
    WatchlistItem,
    StrategyConfig,
    StrategyDefinition,
    now,
)
from app.security import create_session, hash_password, read_session, verify_password
from app.services import (
    _critical_event_symbols,
    create_backtest,
    execute_simulation_exit,
    execute_simulation_strategy,
    adjust_simulation_cash,
    release_simulation_cash,
    scan_plugins,
    seed_database,
    snapshot_account,
    validate_strategy_parameters,
)


@pytest.fixture()
def db(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed_database(session, Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}"))
        yield session


def make_event_data_fresh(db: Session) -> None:
    source = db.scalar(
        select(DataSourceState).where(DataSourceState.provider == "akshare_events")
    )
    source.enabled = True
    source.healthy = True
    source.last_checked_at = now()
    db.commit()


def test_model_count_and_seed(db: Session):
    assert len(Base.metadata.tables) == 35
    account = db.scalar(select(SimulationAccount))
    definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
    assert account is not None and account.initial_cash == 10000
    assert definition is not None and definition.required_timeframes == ["1m"]


def test_snapshot_revalues_total_asset_from_cash_and_all_positions(db: Session):
    account = db.scalar(select(SimulationAccount).limit(1))
    stocks = list(db.scalars(select(Stock).order_by(Stock.id).limit(2)))
    account.cash_balance = 7_000
    account.available_cash = 7_000
    account.total_asset = 99_999
    for stock, quantity in zip(stocks, (100, 200), strict=True):
        stock.last_price = 10
        db.add(
            Position(
                account_id=account.id,
                mode="SIMULATION",
                stock_id=stock.id,
                quantity=quantity,
                available_quantity=quantity,
                average_cost=9,
                market_value=0,
                unrealized_pnl=0,
            )
        )
    db.flush()

    snapshot = snapshot_account(db, account)

    assert snapshot.market_value == 3_000
    assert snapshot.total_asset == 10_000
    assert account.total_asset == 10_000


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


def test_seed_backfills_legacy_simulation_config_account(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-config.db'}")
    Base.metadata.create_all(engine)
    settings = Settings(database_url=str(engine.url))
    with Session(engine) as session:
        seed_database(session, settings)
        definition = session.scalar(
            select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold")
        )
        account = session.scalar(
            select(SimulationAccount).order_by(SimulationAccount.id)
        )
        account_id = account.id
        legacy = StrategyConfig(
            strategy_definition_id=definition.id,
            name="历史模拟配置",
            mode="SIMULATION",
            parameters={},
            simulation_account_id=None,
        )
        session.add(legacy)
        session.commit()

        seed_database(session, settings)
        session.refresh(legacy)

    assert legacy.simulation_account_id == account_id


def test_password_and_signed_session():
    encoded = hash_password("secret")
    assert verify_password("secret", encoded)
    assert not verify_password("wrong", encoded)
    token = create_session("admin", "key", ttl_seconds=60)
    assert read_session(token, "key")["sub"] == "admin"
    assert read_session(token, "wrong") is None


def test_simulation_run_creates_fill_position_and_t1_lock(db: Session):
    make_event_data_fresh(db)
    definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
    stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    stock.change_pct = 4.9
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


def test_simulation_entry_sizes_from_slippage_adjusted_price_before_risk_gate(db: Session):
    make_event_data_fresh(db)
    account = db.scalar(select(SimulationAccount).order_by(SimulationAccount.id))
    risk = db.scalar(select(RiskSettings).where(RiskSettings.mode == "SIMULATION"))
    account.initial_cash = 100_000
    account.cash_balance = 100_000
    account.available_cash = 100_000
    account.total_asset = 100_000
    risk.max_order_notional_abs = 20_000

    definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
    stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    stock.change_pct = 4.9
    stock.turnover_amount = 900_000_000
    stock.last_price = 2.94
    stock.quote_updated_at = now()
    config = StrategyConfig(
        strategy_definition_id=definition.id,
        name="滑点反推数量测试",
        mode="SIMULATION",
        parameters={},
        simulation_account_id=account.id,
    )
    db.add(config)
    db.commit()

    run = execute_simulation_strategy(db, config)

    order = db.scalar(select(Order).order_by(Order.id.desc()))
    fill = db.scalar(select(Fill).order_by(Fill.id.desc()))
    assert run.status == "completed"
    assert order.quantity == 6_700
    assert fill.price * fill.quantity <= 20_000


def test_simulation_run_uses_strategy_bound_account(db: Session):
    make_event_data_fresh(db)
    definition = db.scalar(
        select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold")
    )
    default_account = db.scalar(select(SimulationAccount).order_by(SimulationAccount.id))
    dedicated = SimulationAccount(
        name="一夜持股专用测试账户",
        initial_cash=20_000,
        cash_balance=20_000,
        available_cash=20_000,
        total_asset=20_000,
        commission_rate=0.0003,
        min_commission=5,
        stamp_tax_rate=0.0005,
        transfer_fee_rate=0,
        slippage_bps=5,
    )
    db.add(dedicated)
    db.flush()
    stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    stock.change_pct = 4.9
    stock.turnover_amount = 900_000_000
    stock.last_price = 10.01
    stock.quote_updated_at = now()
    config = StrategyConfig(
        strategy_definition_id=definition.id,
        name="绑定账户测试",
        mode="SIMULATION",
        parameters={},
        simulation_account_id=dedicated.id,
    )
    db.add(config)
    db.commit()

    execute_simulation_strategy(db, config)
    order = db.scalar(select(Order).order_by(Order.id.desc()))

    assert order.account_id == dedicated.id
    assert dedicated.cash_balance < 20_000
    assert default_account.cash_balance == default_account.initial_cash


def test_simulation_run_selects_best_universe_candidate_not_first_watchlist(db: Session):
    make_event_data_fresh(db)
    definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
    leading = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    stronger = db.scalar(select(Stock).where(Stock.symbol == "300750.SZ"))
    stronger.change_pct = 4.1
    stronger.turnover_amount = 900_000_000
    stronger.last_price = 10.56
    stronger.quote_updated_at = now()
    leading.change_pct = 1.8
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


def test_live_run_requires_runtime_switch_even_when_database_is_enabled(db: Session):
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
    assert "运行配置" in run.error_message
    assert db.scalar(select(Order).where(Order.mode == "LIVE")) is None


def test_simulation_exit_sells_available_next_session_position(db: Session):
    make_event_data_fresh(db)
    definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
    stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    stock.change_pct = 4.9
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
    db.execute(
        text("UPDATE fills SET filled_at = :filled_at WHERE id = (SELECT MAX(id) FROM fills)"),
        {"filled_at": now() - timedelta(days=1)},
    )
    db.commit()

    run = execute_simulation_exit(db, config)

    db.refresh(position)
    assert run.status == "completed"
    assert position.quantity == 0
    assert len(list(db.scalars(select(Order)))) == 2
    sell_fill = db.scalars(select(Fill).order_by(Fill.id.desc())).first()
    assert sell_fill.stamp_tax > 0


def test_simulation_exit_keeps_same_day_purchase_locked(db: Session):
    make_event_data_fresh(db)
    definition = db.scalar(
        select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold")
    )
    stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    stock.change_pct = 4.9
    stock.turnover_amount = 900_000_000
    stock.last_price = 10.01
    stock.quote_updated_at = now()
    config = StrategyConfig(
        strategy_definition_id=definition.id,
        name="测试当日锁定",
        mode="SIMULATION",
        parameters={},
    )
    db.add(config)
    db.commit()
    execute_simulation_strategy(db, config)

    run = execute_simulation_exit(db, config)

    position = db.scalar(select(Position))
    assert position.quantity > 0
    assert position.available_quantity == 0
    assert run.summary["reason"] == "没有可卖持仓"


def test_simulation_exit_retries_when_position_quote_is_stale(db: Session):
    make_event_data_fresh(db)
    definition = db.scalar(
        select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold")
    )
    stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    stock.change_pct = 4.9
    stock.turnover_amount = 900_000_000
    stock.last_price = 10.01
    stock.quote_updated_at = now()
    config = StrategyConfig(
        strategy_definition_id=definition.id,
        name="测试退出过期行情",
        mode="SIMULATION",
        parameters={},
    )
    db.add(config)
    db.commit()
    execute_simulation_strategy(db, config)
    db.execute(
        text("UPDATE fills SET filled_at = :filled_at WHERE id = (SELECT MAX(id) FROM fills)"),
        {"filled_at": now() - timedelta(days=1)},
    )
    stock.quote_updated_at = now() - timedelta(seconds=120)
    db.commit()

    run = execute_simulation_exit(db, config)

    position = db.scalar(select(Position))
    assert position.quantity > 0
    assert len(list(db.scalars(select(Order)))) == 1
    assert run.summary["accepted"] == 0
    assert run.summary["retryable"] is True
    assert "行情已过期" in run.summary["reason"]


def test_overnight_strategy_rejects_stale_corporate_events(db: Session):
    definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
    source = db.scalar(select(DataSourceState).where(DataSourceState.provider == "cninfo"))
    stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    stock.change_pct = 4.9
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


def test_overnight_strategy_accepts_fresh_fallback_event_source(db: Session):
    definition = db.scalar(
        select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold")
    )
    cninfo = db.scalar(
        select(DataSourceState).where(DataSourceState.provider == "cninfo")
    )
    fallback = db.scalar(
        select(DataSourceState).where(DataSourceState.provider == "akshare_events")
    )
    stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    stock.change_pct = 4.9
    stock.turnover_amount = 900_000_000
    stock.last_price = 10.01
    stock.quote_updated_at = now()
    cninfo.healthy = False
    fallback.healthy = True
    fallback.last_checked_at = now()
    config = StrategyConfig(
        strategy_definition_id=definition.id,
        name="测试公告后备源",
        mode="SIMULATION",
        parameters={"event_risk_enabled": True},
    )
    db.add(config)
    db.commit()

    run = execute_simulation_strategy(db, config)

    assert run.summary["accepted"] == 1
    assert db.scalar(select(Order)) is not None


def test_critical_event_filter_only_uses_recent_announcements(db: Session):
    current = now()
    stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    db.add_all(
        [
            StockEvent(
                stock_id=stock.id,
                event_type="material_litigation",
                severity="critical",
                title="历史诉讼公告",
                source="akshare",
                source_event_id="old-critical",
                published_at=current - timedelta(days=8),
            ),
            StockEvent(
                stock_id=stock.id,
                event_type="material_litigation",
                severity="critical",
                title="近期诉讼公告",
                source="akshare",
                source_event_id="recent-critical",
                published_at=current - timedelta(days=1),
            ),
        ]
    )
    db.commit()

    assert _critical_event_symbols(db, current=current) == {"000001.SZ"}

    recent = db.scalar(
        select(StockEvent).where(StockEvent.source_event_id == "recent-critical")
    )
    recent.published_at = current - timedelta(days=8)
    db.commit()

    assert _critical_event_symbols(db, current=current) == set()


def test_event_risk_blocks_specified_types_and_large_unlocks(db: Session):
    current = now()
    stocks = list(db.scalars(select(Stock).order_by(Stock.id).limit(4)))
    db.add_all(
        [
            StockEvent(
                stock_id=stocks[0].id,
                event_type="shareholder_reduction",
                severity="warning",
                title="股东减持公告",
                source="akshare",
                source_event_id="reduction-risk",
                published_at=current,
            ),
            StockEvent(
                stock_id=stocks[1].id,
                event_type="resumption",
                severity="info",
                title="复牌公告",
                source="akshare",
                source_event_id="resumption-risk",
                published_at=current,
            ),
            StockEvent(
                stock_id=stocks[2].id,
                event_type="unlock",
                severity="warning",
                title="限售股上市公告",
                source="akshare",
                source_event_id="small-unlock",
                published_at=current,
                unlock_free_float_pct=0.05,
            ),
            StockEvent(
                stock_id=stocks[3].id,
                event_type="unlock",
                severity="warning",
                title="大比例限售股上市公告",
                source="akshare",
                source_event_id="large-unlock",
                published_at=current,
                unlock_free_float_pct=0.051,
            ),
        ]
    )
    db.commit()

    blocked = _critical_event_symbols(db, current=current)

    assert stocks[0].symbol in blocked
    assert stocks[1].symbol in blocked
    assert stocks[2].symbol not in blocked
    assert stocks[3].symbol in blocked


def test_simulation_exit_only_sells_positions_owned_by_config(db: Session):
    make_event_data_fresh(db)
    definition = db.scalar(
        select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold")
    )
    first_stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    second_stock = db.scalar(select(Stock).where(Stock.symbol == "300750.SZ"))

    def prepare_candidate(target: Stock) -> None:
        for item in db.scalars(select(Stock)):
            item.change_pct = 4.9 if item.id == target.id else 0.2
            item.turnover_amount = 900_000_000 if item.id == target.id else 10_000_000
            item.last_price = 10.01
            item.quote_updated_at = now()
        db.commit()

    first_config = StrategyConfig(
        strategy_definition_id=definition.id,
        name="配置一",
        mode="SIMULATION",
        parameters={},
    )
    second_config = StrategyConfig(
        strategy_definition_id=definition.id,
        name="配置二",
        mode="SIMULATION",
        parameters={},
    )
    db.add_all([first_config, second_config])
    db.commit()

    prepare_candidate(first_stock)
    execute_simulation_strategy(db, first_config)
    prepare_candidate(second_stock)
    execute_simulation_strategy(db, second_config)
    db.execute(
        text("UPDATE fills SET filled_at = :filled_at"),
        {"filled_at": now() - timedelta(days=1)},
    )
    db.commit()

    run = execute_simulation_exit(db, first_config)

    assert run.summary["accepted"] == 1
    assert run.summary["sold"][0]["symbol"] == first_stock.symbol
    first_position = db.scalar(
        select(Position).where(Position.stock_id == first_stock.id)
    )
    second_position = db.scalar(
        select(Position).where(Position.stock_id == second_stock.id)
    )
    assert first_position.quantity == 0
    assert second_position.quantity > 0


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
    make_event_data_fresh(db)
    definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
    stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    stock.change_pct = 4.9
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
