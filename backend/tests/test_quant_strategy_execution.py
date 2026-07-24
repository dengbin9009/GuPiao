from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import (
    DataSourceState,
    Fill,
    MarketDailyBar,
    Order,
    NotificationChannel,
    NotificationDelivery,
    Position,
    QuantPortfolioDecision,
    SimulationAccount,
    Stock,
    StrategyConfig,
    StrategyPerformanceDaily,
    StrategyPositionLot,
    StrategyRiskProfile,
    StrategySchedule,
    RiskSettings,
)
from app.quant_strategies.execution import execute_quant_rebalance
from app.quant_strategies.performance import record_quant_daily_performance
from app.quant_strategies.readiness import configuration_fingerprint
from app.quant_strategies.runtime import seed_quant_strategy_runtimes
from app.services import seed_database


SHANGHAI = ZoneInfo("Asia/Shanghai")


def setup_runtime(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'execution.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    settings = Settings(database_url=database_url, live_enabled=False, broker_adapter="simulation")
    current = datetime(2026, 7, 24, 9, 35, tzinfo=SHANGHAI)
    with Session(engine) as db:
        seed_database(db, settings)
        configs = seed_quant_strategy_runtimes(db, settings)
        event_source = db.scalar(
            select(DataSourceState).where(
                DataSourceState.provider == "akshare_events"
            )
        )
        event_source.healthy = True
        event_source.last_checked_at = current
        for stock in db.scalars(select(Stock).where(Stock.exchange.in_(["SSE", "SZSE"]))):
            stock.last_price = 10.0
            stock.quote_updated_at = current
        db.commit()
        config_ids = {key: value.id for key, value in configs.items()}
    return engine, current, config_ids


def decision_for(db: Session, config: StrategyConfig, current: datetime, weights):
    definition = db.get(__import__("app.models", fromlist=["StrategyDefinition"]).StrategyDefinition, config.strategy_definition_id)
    decision = QuantPortfolioDecision(
        strategy_config_id=config.id,
        simulation_account_id=config.simulation_account_id,
        trading_date=(current.date() - timedelta(days=1)).isoformat(),
        decision_type="signal",
        status="ready",
        data_as_of=current - timedelta(hours=17),
        config_fingerprint=configuration_fingerprint(
            config.parameters or {},
            simulation_account_id=config.simulation_account_id,
            strategy_version=definition.version,
        ),
        strategy_version=definition.version,
        data_version="1",
        target_weights=weights,
    )
    db.add(decision)
    db.commit()
    db.refresh(decision)
    return decision


def test_dry_run_records_decision_without_orders(tmp_path: Path):
    engine, current, config_ids = setup_runtime(tmp_path)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_ids["multi_factor_core"])
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        decision = decision_for(db, config, current, {stock.symbol: 0.10})

        run = execute_quant_rebalance(db, decision, current=current, dry_run=True)

        assert run.summary["dry_run"] is True
        assert run.summary["order_ids"] == []
        assert db.scalar(select(func.count()).select_from(Order)) == 0


def test_after_close_dry_run_uses_completed_daily_close_when_quote_is_stale(
    tmp_path: Path,
):
    engine, current, config_ids = setup_runtime(tmp_path)
    current = current.replace(hour=22)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_ids["multi_factor_core"])
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        stock.quote_updated_at = current - timedelta(hours=7)
        event_source = db.scalar(
            select(DataSourceState).where(
                DataSourceState.provider == "akshare_events"
            )
        )
        event_source.last_checked_at = current
        db.add(
            MarketDailyBar(
                stock_id=stock.id,
                trade_date=current.date().isoformat(),
                open=11.8,
                high=12.2,
                low=11.7,
                close=12.0,
                adjusted_close=12.0,
                adjustment_factor=1.0,
                volume=1_000_000,
                amount=12_000_000,
                quality_status="valid",
                source="akshare",
                captured_at=current,
            )
        )
        decision = decision_for(db, config, current, {stock.symbol: 0.10})
        decision.trading_date = current.date().isoformat()
        db.commit()

        run = execute_quant_rebalance(db, decision, current=current, dry_run=True)

        assert run.summary["precheck_passed"] is True
        assert run.summary["planning_price_source"] == "completed_daily_close"
        assert run.summary["planned_orders"] == [
            {"symbol": stock.symbol, "side": "buy", "quantity": 16_600}
        ]
        assert run.summary["order_ids"] == []
        assert db.scalar(select(func.count()).select_from(Order)) == 0


def test_rebalance_uses_only_bound_account_and_applies_fees_and_lot_size(tmp_path: Path):
    engine, current, config_ids = setup_runtime(tmp_path)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_ids["multi_factor_core"])
        other = db.get(StrategyConfig, config_ids["relative_strength_rotation"])
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        decision = decision_for(db, config, current, {stock.symbol: 0.10})

        run = execute_quant_rebalance(db, decision, current=current, dry_run=False)
        order = db.scalar(select(Order).where(Order.strategy_run_id == run.id))
        fill = db.scalar(select(Fill).where(Fill.order_id == order.id))
        position = db.scalar(
            select(Position).where(
                Position.account_id == config.simulation_account_id,
                Position.stock_id == stock.id,
            )
        )
        account = db.get(SimulationAccount, config.simulation_account_id)
        other_account = db.get(SimulationAccount, other.simulation_account_id)

        assert order.account_id == config.simulation_account_id
        assert order.mode == "SIMULATION"
        assert order.quantity % stock.lot_size == 0
        assert fill.price == pytest.approx(10.005)
        assert fill.commission >= 5
        assert position.quantity == order.quantity
        assert position.available_quantity == 0
        assert account.cash_balance < account.initial_cash
        assert other_account.cash_balance == other_account.initial_cash
        assert db.scalar(
            select(func.count()).select_from(Order).where(
                Order.account_id == other.simulation_account_id
            )
        ) == 0


def test_buy_quantity_is_sized_from_slippage_adjusted_price(tmp_path: Path):
    engine, current, config_ids = setup_runtime(tmp_path)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_ids["multi_factor_core"])
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        stock.last_price = 99.98
        stock.quote_updated_at = current
        db.commit()
        decision = decision_for(db, config, current, {stock.symbol: 0.05})

        run = execute_quant_rebalance(db, decision, current=current, dry_run=False)
        order = db.scalar(select(Order).where(Order.strategy_run_id == run.id))

        expected = int(
            (2_000_000 * 0.05) // (99.98 * 1.0005 * stock.lot_size)
        ) * stock.lot_size
        assert order.quantity == expected
        assert order.quantity * 99.98 * 1.0005 <= 2_000_000 * 0.05


def test_system_emergency_stop_blocks_quant_rebalance(tmp_path: Path):
    engine, current, config_ids = setup_runtime(tmp_path)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_ids["multi_factor_core"])
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        system_risk = db.scalar(
            select(RiskSettings).where(RiskSettings.mode == "SIMULATION")
        )
        system_risk.emergency_stop_enabled = True
        db.commit()
        decision = decision_for(db, config, current, {stock.symbol: 0.05})

        run = execute_quant_rebalance(db, decision, current=current, dry_run=False)

        assert run.summary["accepted"] == 0
        assert "系统级紧急停止" in run.summary["reason"]
        assert db.scalar(select(func.count()).select_from(Order)) == 0


def test_stale_corporate_event_source_blocks_quant_rebalance(tmp_path: Path):
    engine, current, config_ids = setup_runtime(tmp_path)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_ids["multi_factor_core"])
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        source = db.scalar(
            select(DataSourceState).where(
                DataSourceState.provider == "akshare_events"
            )
        )
        source.last_checked_at = current - timedelta(
            seconds=source.stale_after_seconds + 1
        )
        db.commit()
        decision = decision_for(db, config, current, {stock.symbol: 0.05})

        run = execute_quant_rebalance(db, decision, current=current, dry_run=False)

        assert run.summary["accepted"] == 0
        assert run.summary["retryable"] is True
        assert "风险公告数据已过期" in run.summary["reason"]
        assert db.scalar(select(func.count()).select_from(Order)) == 0


def test_maximum_drawdown_uses_full_account_history(tmp_path: Path):
    engine, current, config_ids = setup_runtime(tmp_path)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_ids["multi_factor_core"])
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        start = current.date() - timedelta(days=400)
        for offset in range(253):
            db.add(
                StrategyPerformanceDaily(
                    strategy_config_id=config.id,
                    simulation_account_id=config.simulation_account_id,
                    trading_date=(start + timedelta(days=offset)).isoformat(),
                    cash_balance=3_000_000 if offset == 0 else 2_000_000,
                    market_value=0,
                    total_asset=3_000_000 if offset == 0 else 2_000_000,
                    daily_return=0,
                    cumulative_return=0,
                    drawdown=0,
                    exposure=0,
                    captured_at=current - timedelta(days=400 - offset),
                )
            )
        db.commit()
        decision = decision_for(db, config, current, {stock.symbol: 0.05})

        run = execute_quant_rebalance(db, decision, current=current, dry_run=False)

        assert run.summary["accepted"] == 0
        assert "最大回撤熔断" in run.summary["reason"]
        assert db.scalar(select(func.count()).select_from(Order)) == 0


def test_limit_price_state_blocks_untradeable_target(tmp_path: Path):
    engine, current, config_ids = setup_runtime(tmp_path)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_ids["multi_factor_core"])
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        stock.limit_up_price = stock.last_price
        db.commit()
        decision = decision_for(db, config, current, {stock.symbol: 0.05})

        run = execute_quant_rebalance(db, decision, current=current, dry_run=False)

        assert run.summary["accepted"] == 0
        assert "涨停" in run.summary["reason"]
        assert db.scalar(select(func.count()).select_from(Order)) == 0


def test_full_precheck_keeps_account_unchanged_when_any_quote_is_stale(tmp_path: Path):
    engine, current, config_ids = setup_runtime(tmp_path)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_ids["multi_factor_core"])
        first = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        second = db.scalar(select(Stock).where(Stock.symbol == "000858.SZ"))
        second.quote_updated_at = current - timedelta(minutes=5)
        db.commit()
        decision = decision_for(db, config, current, {first.symbol: 0.05, second.symbol: 0.05})
        account = db.get(SimulationAccount, config.simulation_account_id)
        opening_cash = account.cash_balance

        run = execute_quant_rebalance(db, decision, current=current, dry_run=False)

        assert run.summary["accepted"] == 0
        assert "行情已过期" in run.summary["reason"]
        assert db.scalar(select(func.count()).select_from(Order)) == 0
        assert db.get(SimulationAccount, config.simulation_account_id).cash_balance == opening_cash


def test_missing_holding_quote_does_not_zero_account_valuation(tmp_path: Path):
    engine, current, config_ids = setup_runtime(tmp_path)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_ids["multi_factor_core"])
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        account = db.get(SimulationAccount, config.simulation_account_id)
        account.cash_balance = 1_800_000
        account.available_cash = 1_800_000
        account.total_asset = 2_000_000
        position = Position(
            account_id=account.id,
            mode="SIMULATION",
            stock_id=stock.id,
            quantity=20_000,
            available_quantity=20_000,
            average_cost=10,
            market_value=200_000,
            unrealized_pnl=0,
        )
        db.add(position)
        stock.last_price = None
        stock.quote_updated_at = None
        db.commit()
        decision = decision_for(db, config, current, {})

        run = execute_quant_rebalance(
            db,
            decision,
            current=current,
            dry_run=False,
        )

        db.refresh(account)
        db.refresh(position)
        assert run.summary["retryable"] is True
        assert "行情缺失" in run.summary["reason"]
        assert account.total_asset == 2_000_000
        assert position.market_value == 200_000
        assert db.scalar(select(func.count()).select_from(Order)) == 0


def test_rebalance_sells_before_buying_and_respects_t1(tmp_path: Path):
    engine, current, config_ids = setup_runtime(tmp_path)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_ids["multi_factor_core"])
        sell_stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        buy_stock = db.scalar(select(Stock).where(Stock.symbol == "000858.SZ"))
        account = db.get(SimulationAccount, config.simulation_account_id)
        account.cash_balance -= 200_000
        account.available_cash -= 200_000
        db.add(
            Position(
                account_id=account.id,
                mode="SIMULATION",
                stock_id=sell_stock.id,
                quantity=20_000,
                available_quantity=20_000,
                average_cost=10,
                market_value=200_000,
            )
        )
        db.commit()
        decision = decision_for(db, config, current, {buy_stock.symbol: 0.10})

        run = execute_quant_rebalance(db, decision, current=current, dry_run=False)
        orders = list(db.scalars(select(Order).where(Order.strategy_run_id == run.id).order_by(Order.id)))

        assert [item.side for item in orders] == ["sell", "buy"]

        next_decision = QuantPortfolioDecision(
            strategy_config_id=config.id,
            simulation_account_id=config.simulation_account_id,
            trading_date=current.date().isoformat(),
            decision_type="signal",
            status="ready",
            data_as_of=current,
            config_fingerprint=decision.config_fingerprint,
            strategy_version=decision.strategy_version,
            data_version="1",
            target_weights={},
        )
        db.add(next_decision)
        db.commit()
        blocked = execute_quant_rebalance(
            db,
            next_decision,
            current=current + timedelta(minutes=1),
            dry_run=False,
        )
        assert blocked.summary["accepted"] == 0
        assert "T+1" in blocked.summary["reason"]


def test_quant_position_bought_today_becomes_sellable_next_trading_day(
    tmp_path: Path,
):
    engine, current, config_ids = setup_runtime(tmp_path)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_ids["multi_factor_core"])
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        buy_decision = decision_for(db, config, current, {stock.symbol: 0.05})
        buy_run = execute_quant_rebalance(
            db,
            buy_decision,
            current=current,
            dry_run=False,
        )
        assert buy_run.summary["accepted"] == 1
        lot = db.scalar(
            select(StrategyPositionLot).where(
                StrategyPositionLot.strategy_config_id == config.id,
                StrategyPositionLot.stock_id == stock.id,
            )
        )
        assert lot is not None
        assert lot.remaining_quantity > 0
        assert lot.strategy_metadata["strategy_key"] == "multi_factor_core"

        next_day = current + timedelta(days=1)
        stock.quote_updated_at = next_day
        event_source = db.scalar(
            select(DataSourceState).where(
                DataSourceState.provider == "akshare_events"
            )
        )
        event_source.last_checked_at = next_day
        sell_decision = QuantPortfolioDecision(
            strategy_config_id=config.id,
            simulation_account_id=config.simulation_account_id,
            trading_date=next_day.date().isoformat(),
            decision_type="signal",
            status="ready",
            data_as_of=next_day,
            config_fingerprint=buy_decision.config_fingerprint,
            strategy_version=buy_decision.strategy_version,
            data_version="1",
            target_weights={},
        )
        db.add(sell_decision)
        db.commit()

        sell_run = execute_quant_rebalance(
            db,
            sell_decision,
            current=next_day,
            dry_run=False,
        )

        sell_orders = list(
            db.scalars(
                select(Order).where(
                    Order.strategy_run_id == sell_run.id,
                    Order.side == "sell",
                )
            )
        )
        assert sell_run.summary["accepted"] == 1
        assert len(sell_orders) == 1
        position = db.scalar(
            select(Position).where(
                Position.account_id == config.simulation_account_id,
                Position.stock_id == stock.id,
            )
        )
        assert position.quantity == 0
        db.refresh(lot)
        assert lot.remaining_quantity == 0
        assert lot.status == "closed"


def test_quant_buy_lot_uses_explicit_exchange_next_trading_day(tmp_path: Path):
    engine, current, config_ids = setup_runtime(tmp_path)
    friday = current.replace(year=2026, month=7, day=24)
    monday = "2026-07-27"
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_ids["multi_factor_core"])
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        stock.quote_updated_at = friday
        decision = decision_for(db, config, friday, {stock.symbol: 0.05})

        run = execute_quant_rebalance(
            db,
            decision,
            current=friday,
            dry_run=False,
            next_sellable_date=monday,
        )
        lot = db.scalar(
            select(StrategyPositionLot).where(
                StrategyPositionLot.strategy_config_id == config.id,
                StrategyPositionLot.stock_id == stock.id,
            )
        )

        assert run.summary["accepted"] == 1
        assert lot.available_on == monday


def test_risk_reducing_sell_is_not_blocked_by_max_buy_order_notional(
    tmp_path: Path,
):
    engine, current, config_ids = setup_runtime(tmp_path)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_ids["multi_factor_core"])
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        account = db.get(SimulationAccount, config.simulation_account_id)
        stock.last_price = 20
        stock.quote_updated_at = current
        account.cash_balance = 1_000_000
        account.available_cash = 1_000_000
        account.total_asset = 3_000_000
        db.add(
            Position(
                account_id=account.id,
                mode="SIMULATION",
                stock_id=stock.id,
                quantity=100_000,
                available_quantity=100_000,
                average_cost=10,
                market_value=2_000_000,
            )
        )
        db.commit()
        decision = decision_for(db, config, current, {})

        run = execute_quant_rebalance(db, decision, current=current, dry_run=False)

        assert run.summary["accepted"] == 1
        order = db.scalar(select(Order).where(Order.strategy_run_id == run.id))
        assert order.side == "sell"
        assert order.quantity == 100_000


def test_partial_sell_persists_remaining_settled_quantity_as_available(
    tmp_path: Path,
):
    engine, current, config_ids = setup_runtime(tmp_path)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_ids["multi_factor_core"])
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        account = db.get(SimulationAccount, config.simulation_account_id)
        account.cash_balance = 1_800_000
        account.available_cash = 1_800_000
        account.total_asset = 2_000_000
        position = Position(
            account_id=account.id,
            mode="SIMULATION",
            stock_id=stock.id,
            quantity=20_000,
            available_quantity=0,
            average_cost=10,
            market_value=200_000,
        )
        prior_order = Order(
            account_id=account.id,
            mode="SIMULATION",
            stock_id=stock.id,
            side="buy",
            quantity=20_000,
            status="filled",
            submitted_at=current - timedelta(days=1),
        )
        db.add_all([position, prior_order])
        db.flush()
        db.add(
            Fill(
                order_id=prior_order.id,
                account_id=account.id,
                stock_id=stock.id,
                mode="SIMULATION",
                quantity=20_000,
                price=10,
                filled_at=current - timedelta(days=1),
            )
        )
        db.commit()
        decision = decision_for(db, config, current, {stock.symbol: 0.05})

        run = execute_quant_rebalance(
            db,
            decision,
            current=current,
            dry_run=False,
        )

        db.refresh(position)
        sell = db.scalar(
            select(Order).where(
                Order.strategy_run_id == run.id,
                Order.side == "sell",
            )
        )
        assert sell is not None
        assert position.quantity > 0
        assert position.available_quantity == position.quantity


def test_successful_rebalance_records_daily_performance(tmp_path: Path):
    engine, current, config_ids = setup_runtime(tmp_path)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_ids["multi_factor_core"])
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        db.add(
            NotificationChannel(
                type="wecom",
                name="策略运营",
                enabled=True,
                recipient="策略群",
                secret_ref="WECOM_WEBHOOK_URL",
                event_types=[
                    "quant_strategy_trade",
                    "quant_strategy_daily_performance",
                ],
            )
        )
        db.commit()
        decision = decision_for(db, config, current, {stock.symbol: 0.05})

        execute_quant_rebalance(db, decision, current=current, dry_run=False)
        performance = db.scalar(
            select(StrategyPerformanceDaily).where(
                StrategyPerformanceDaily.strategy_config_id == config.id,
                StrategyPerformanceDaily.trading_date == current.date().isoformat(),
            )
        )

        assert performance is not None
        assert performance.total_asset > 0
        assert 0 < performance.exposure <= 0.80
        deliveries = list(
            db.scalars(
                select(NotificationDelivery).where(
                    NotificationDelivery.event_type.in_(
                        [
                            "quant_strategy_trade",
                            "quant_strategy_daily_performance",
                        ]
                    )
                )
            )
        )
        assert {item.event_type for item in deliveries} == {
            "quant_strategy_trade",
        }


def test_close_performance_replaces_morning_rebalance_valuation(tmp_path: Path):
    engine, current, config_ids = setup_runtime(tmp_path)
    close_time = current.replace(hour=16, minute=20)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_ids["multi_factor_core"])
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        db.add(
            NotificationChannel(
                type="wecom",
                name="每日绩效",
                enabled=True,
                recipient="策略群",
                secret_ref="WECOM_WEBHOOK_URL",
                event_types=["quant_strategy_daily_performance"],
            )
        )
        db.commit()
        decision = decision_for(db, config, current, {stock.symbol: 0.05})
        execute_quant_rebalance(db, decision, current=current, dry_run=False)
        morning = db.scalar(
            select(StrategyPerformanceDaily).where(
                StrategyPerformanceDaily.strategy_config_id == config.id
            )
        )
        morning_asset = morning.total_asset
        db.add(
            MarketDailyBar(
                stock_id=stock.id,
                trade_date=close_time.date().isoformat(),
                open=10,
                high=12,
                low=9,
                close=12,
                adjusted_close=12,
                adjustment_factor=1,
                volume=1,
                amount=1,
                source="test-real",
            )
        )
        db.commit()

        result = record_quant_daily_performance(db, current=close_time)
        db.refresh(morning)
        deliveries = list(
            db.scalars(
                select(NotificationDelivery).where(
                    NotificationDelivery.event_type
                    == "quant_strategy_daily_performance"
                )
            )
        )

        assert result["recorded"] == 8
        assert morning.total_asset > morning_asset
        assert morning.captured_at.replace(tzinfo=SHANGHAI) == close_time
        assert len(deliveries) == 8


def test_daily_performance_marks_to_close_and_pauses_only_breached_strategy(
    tmp_path: Path,
):
    engine, current, config_ids = setup_runtime(tmp_path)
    close_time = current.replace(hour=16, minute=20)
    with Session(engine) as db:
        target = db.get(StrategyConfig, config_ids["multi_factor_core"])
        other = db.get(StrategyConfig, config_ids["relative_strength_rotation"])
        target_account = db.get(SimulationAccount, target.simulation_account_id)
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        target_account.cash_balance = 1_000_000
        target_account.available_cash = 1_000_000
        target_account.total_asset = 2_000_000
        db.add(
            Position(
                account_id=target_account.id,
                mode="SIMULATION",
                stock_id=stock.id,
                quantity=100_000,
                available_quantity=100_000,
                average_cost=10,
                market_value=1_000_000,
            )
        )
        db.add(
            MarketDailyBar(
                stock_id=stock.id,
                trade_date=close_time.date().isoformat(),
                open=9,
                high=9,
                low=8,
                close=8,
                adjusted_close=8,
                adjustment_factor=1,
                volume=1,
                amount=1,
                source="test-real",
            )
        )
        for schedule in db.scalars(
            select(StrategySchedule)
        ):
            if schedule.strategy_config_id in {target.id, other.id}:
                schedule.enabled = True
        db.add(
            NotificationChannel(
                type="email",
                name="策略运营",
                enabled=True,
                recipient="ops@example.com",
                secret_ref="SMTP_PASSWORD",
                event_types=[
                    "quant_strategy_daily_performance",
                    "quant_strategy_auto_paused",
                ],
            )
        )
        db.commit()

        result = record_quant_daily_performance(db, current=close_time)

        performance = db.scalar(
            select(StrategyPerformanceDaily).where(
                StrategyPerformanceDaily.strategy_config_id == target.id,
                StrategyPerformanceDaily.trading_date == close_time.date().isoformat(),
            )
        )
        target_risk = db.scalar(
            select(StrategyRiskProfile).where(
                StrategyRiskProfile.strategy_config_id == target.id
            )
        )
        target_schedules = list(
            db.scalars(
                select(StrategySchedule).where(
                    StrategySchedule.strategy_config_id == target.id
                )
            )
        )
        other_schedules = list(
            db.scalars(
                select(StrategySchedule).where(
                    StrategySchedule.strategy_config_id == other.id
                )
            )
        )

        assert result["recorded"] == 8
        assert performance.total_asset == pytest.approx(1_800_000)
        assert performance.daily_return == pytest.approx(-0.10)
        assert performance.drawdown == pytest.approx(-0.10)
        assert target_risk.emergency_stop_enabled is True
        assert all(not schedule.enabled for schedule in target_schedules)
        assert all(schedule.enabled for schedule in other_schedules)


def test_daily_performance_is_idempotent_for_notifications(tmp_path: Path):
    engine, current, _config_ids = setup_runtime(tmp_path)
    close_time = current.replace(hour=16, minute=20)
    with Session(engine) as db:
        db.add(
            NotificationChannel(
                type="wecom",
                name="每日绩效",
                enabled=True,
                recipient="策略群",
                secret_ref="WECOM_WEBHOOK_URL",
                event_types=["quant_strategy_daily_performance"],
            )
        )
        db.commit()

        first = record_quant_daily_performance(db, current=close_time)
        second = record_quant_daily_performance(db, current=close_time)
        deliveries = list(
            db.scalars(
                select(NotificationDelivery).where(
                    NotificationDelivery.event_type
                    == "quant_strategy_daily_performance"
                )
            )
        )

        assert first["recorded"] == 8
        assert second["recorded"] == 0
        assert len(deliveries) == 8
