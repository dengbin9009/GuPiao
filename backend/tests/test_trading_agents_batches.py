from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from time import sleep
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import (
    MarketDailyBar,
    DataSourceState,
    Position,
    Stock,
    StrategyConfig,
    StrategyDefinition,
    TradingAgentCandidateAnalysis,
    TradingAgentPortfolioDecision,
    SimulationAccount,
)
from app.services import seed_database
from app.trading_agents.batches import (
    AnalysisResult,
    claim_pending_batch,
    create_batch,
    process_batch,
)
from app.trading_agents.runtime import seed_trading_agents_runtime


SHANGHAI = ZoneInfo("Asia/Shanghai")


def setup_batch_db(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'batch.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed_database(db, Settings(database_url=str(engine.url)))
        seed_trading_agents_runtime(db, Settings(database_url=str(engine.url)))
        event_source = db.scalar(
            select(DataSourceState).where(DataSourceState.provider == "akshare_events")
        )
        event_source.healthy = True
        event_source.last_checked_at = datetime(2026, 7, 13, 13, 25, tzinfo=SHANGHAI)
        config = db.scalar(
            select(StrategyConfig)
            .join(StrategyDefinition)
            .where(StrategyDefinition.key == "trading_agents_auto")
        )
        config.parameters = {**config.parameters, "enrichment_enabled": False}
        account_id = config.simulation_account_id
        current = datetime(2026, 7, 13, 13, 30, tzinfo=SHANGHAI)
        start = current - timedelta(days=60)
        for index in range(12):
            symbol = f"6001{index:02d}.SH"
            stock = Stock(
                code=symbol[:6],
                exchange="SSE",
                symbol=symbol,
                name=f"候选{index}",
                status="active",
                last_price=10 + index,
                turnover_amount=500_000_000 + index,
                change_pct=2,
                quote_updated_at=datetime(2026, 7, 13, 13, 25, tzinfo=SHANGHAI),
            )
            db.add(stock)
            db.flush()
            price = 10.0
            for day in range(60):
                price *= 1.003 + index / 100_000
                db.add(
                    MarketDailyBar(
                        stock_id=stock.id,
                        trade_date=(start + timedelta(days=day)).date().isoformat(),
                        open=price * 0.99,
                        high=price * 1.01,
                        low=price * 0.98,
                        close=price,
                        volume=20_000_000,
                        amount=200_000_000 + index,
                        source="test",
                    )
                )
        holding = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        db.add(
            Position(
                account_id=account_id,
                mode="SIMULATION",
                stock_id=holding.id,
                quantity=100,
                available_quantity=100,
                average_cost=10,
                market_value=1000,
                unrealized_pnl=0,
            )
        )
        holding.last_price = 10
        holding.quote_updated_at = datetime(2026, 7, 13, 13, 25, tzinfo=SHANGHAI)
        price = 8.0
        for day in range(60):
            price *= 1.002
            db.add(
                MarketDailyBar(
                    stock_id=holding.id,
                    trade_date=(start + timedelta(days=day)).date().isoformat(),
                    open=price * 0.99,
                    high=price * 1.01,
                    low=price * 0.98,
                    close=price,
                    volume=20_000_000,
                    amount=200_000_000,
                    source="test",
                )
            )
        db.commit()
    return engine


class FakeAnalyzer:
    def analyze(self, *, symbol, **_):
        return AnalysisResult(
            rating="Buy" if symbol != "000001.SZ" else "Hold",
            ai_target_weight=0.1,
            report=f"{symbol} report",
            reasoning="test",
            llm_calls=2,
            tokens_in=100,
            tokens_out=20,
        )

    def decide_portfolio(self, *, analyses, **_):
        return {
            "rankings": [
                {"symbol": item["symbol"], "rank": index + 1}
                for index, item in enumerate(analyses)
            ],
            "rationale": "all complete",
            "llm_calls": 1,
            "tokens_in": 50,
            "tokens_out": 10,
        }


def test_create_batch_persists_snapshot_top_ten_and_holding(tmp_path):
    engine = setup_batch_db(tmp_path)
    current = datetime(2026, 7, 13, 13, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        batch = create_batch(db, config, current=current, snapshot_root=tmp_path)
        analyses = list(
            db.scalars(
                select(TradingAgentCandidateAnalysis)
                .where(TradingAgentCandidateAnalysis.batch_id == batch.id)
            )
        )

        assert batch.status == "pending"
        assert len(batch.candidate_symbols) == 10
        assert batch.holding_symbols == ["000001.SZ"]
        assert len(batch.required_symbols) == 11
        assert len(analyses) == 11
        assert Path(batch.snapshot_uri).is_file()
        assert len(batch.snapshot_sha256) == 64


def test_claim_and_process_batch_are_all_or_nothing(tmp_path):
    engine = setup_batch_db(tmp_path)
    current = datetime(2026, 7, 13, 13, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        batch = create_batch(db, config, current=current, snapshot_root=tmp_path)

    with Session(engine) as db:
        claimed = claim_pending_batch(db, worker_id="worker-1", current=current)
        assert claimed.id == batch.id
        assert claim_pending_batch(db, worker_id="worker-2", current=current) is None
        processed = process_batch(db, claimed, analyzer=FakeAnalyzer(), current=current)
        assert processed.status == "ready"
        assert processed.llm_calls == 23
        assert db.scalar(
            select(func.count(TradingAgentPortfolioDecision.id)).where(
                TradingAgentPortfolioDecision.batch_id == processed.id
            )
        ) == 1
        assert all(
            item.status == "completed"
            for item in db.scalars(
                select(TradingAgentCandidateAnalysis).where(
                    TradingAgentCandidateAnalysis.batch_id == processed.id
                )
            )
        )


def test_any_candidate_failure_blocks_portfolio_decision(tmp_path):
    engine = setup_batch_db(tmp_path)
    current = datetime(2026, 7, 13, 13, 30, tzinfo=SHANGHAI)

    class BrokenAnalyzer(FakeAnalyzer):
        def analyze(self, *, symbol, **kwargs):
            if symbol.endswith("05.SH"):
                raise RuntimeError("provider timeout")
            return super().analyze(symbol=symbol, **kwargs)

    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        batch = create_batch(db, config, current=current, snapshot_root=tmp_path)
        batch = claim_pending_batch(db, worker_id="worker-1", current=current)
        processed = process_batch(db, batch, analyzer=BrokenAnalyzer(), current=current)

        assert processed.status == "failed"
        assert "provider timeout" in processed.error_message
        assert db.scalar(
            select(func.count(TradingAgentPortfolioDecision.id)).where(
                TradingAgentPortfolioDecision.batch_id == processed.id
            )
        ) == 0


def test_budget_overrun_blocks_portfolio_decision(tmp_path):
    engine = setup_batch_db(tmp_path)
    current = datetime(2026, 7, 13, 13, 30, tzinfo=SHANGHAI)

    class ExpensiveAnalyzer(FakeAnalyzer):
        def analyze(self, *, symbol, **kwargs):
            result = super().analyze(symbol=symbol, **kwargs)
            return AnalysisResult(**{**result.__dict__, "llm_calls": 101})

    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        batch = create_batch(db, config, current=current, snapshot_root=tmp_path)
        batch = claim_pending_batch(db, worker_id="worker-1", current=current)
        processed = process_batch(db, batch, analyzer=ExpensiveAnalyzer(), current=current)

        assert processed.status == "failed"
        assert "预算" in processed.error_message
        assert db.scalar(
            select(func.count(TradingAgentPortfolioDecision.id)).where(
                TradingAgentPortfolioDecision.batch_id == processed.id
            )
        ) == 0


def test_candidate_analysis_respects_configured_concurrency(tmp_path):
    engine = setup_batch_db(tmp_path)
    current = datetime(2026, 7, 13, 13, 30, tzinfo=SHANGHAI)
    active = 0
    maximum = 0
    lock = Lock()

    class ConcurrencyAnalyzer(FakeAnalyzer):
        def analyze(self, *, symbol, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            sleep(0.01)
            try:
                return super().analyze(symbol=symbol, **kwargs)
            finally:
                with lock:
                    active -= 1

    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        config.parameters = {**config.parameters, "worker_concurrency": 2}
        db.commit()
        batch = create_batch(db, config, current=current, snapshot_root=tmp_path)
        batch = claim_pending_batch(db, worker_id="worker-1", current=current)
        processed = process_batch(db, batch, analyzer=ConcurrencyAnalyzer(), current=current)
        processed_status = processed.status

    assert processed_status == "ready"
    assert maximum == 2


def test_ai_target_mapping_uses_cross_stock_portfolio_targets(tmp_path):
    engine = setup_batch_db(tmp_path)
    current = datetime(2026, 7, 13, 13, 30, tzinfo=SHANGHAI)

    class TargetAnalyzer(FakeAnalyzer):
        def decide_portfolio(self, *, analyses, **_):
            return {
                "rankings": [
                    {
                        "symbol": item["symbol"],
                        "rank": index + 1,
                        "ai_target_weight": 0.12 if index < 5 else 0,
                    }
                    for index, item in enumerate(analyses)
                ],
                "rationale": "structured targets",
                "llm_calls": 1,
                "tokens_in": 50,
                "tokens_out": 10,
            }

    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        config.parameters = {**config.parameters, "position_mapping": "ai_target"}
        db.commit()
        batch = create_batch(db, config, current=current, snapshot_root=tmp_path)
        batch = claim_pending_batch(db, worker_id="worker-1", current=current)
        processed = process_batch(db, batch, analyzer=TargetAnalyzer(), current=current)
        decision = db.scalar(
            select(TradingAgentPortfolioDecision).where(
                TradingAgentPortfolioDecision.batch_id == processed.id
            )
        )

    assert processed.status == "ready", processed.error_message
    assert len(decision.target_weights) <= 5
    assert sum(decision.target_weights.values()) <= 0.60 + 1e-9
    assert set(decision.target_weights.values()) == {0.12}


def test_portfolio_mapping_revalues_holdings_from_latest_quote(tmp_path):
    engine = setup_batch_db(tmp_path)
    current = datetime(2026, 7, 13, 13, 30, tzinfo=SHANGHAI)
    class RevalueAnalyzer(FakeAnalyzer):
        def analyze(self, *, symbol, **_):
            return AnalysisResult(
                rating="Hold" if symbol == "000001.SZ" else "Sell",
                ai_target_weight=0,
                report=f"{symbol} report",
                reasoning="test",
                llm_calls=1,
                tokens_in=10,
                tokens_out=2,
            )

    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        account = db.get(SimulationAccount, config.simulation_account_id)
        holding = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        holding.last_price = 20
        batch = create_batch(db, config, current=current, snapshot_root=tmp_path)
        batch = claim_pending_batch(db, worker_id="worker-1", current=current)
        processed = process_batch(db, batch, analyzer=RevalueAnalyzer(), current=current)
        decision = db.scalar(
            select(TradingAgentPortfolioDecision).where(
                TradingAgentPortfolioDecision.batch_id == processed.id
            )
        )
        position = db.scalar(
            select(Position).where(
                Position.account_id == account.id,
                Position.stock_id == holding.id,
            )
        )

    assert position.market_value == 2_000
    assert account.total_asset == 102_000
    assert decision.target_weights["000001.SZ"] == pytest.approx(2_000 / 102_000)


def test_batch_passes_remaining_overall_deadline_to_both_child_stages(tmp_path):
    engine = setup_batch_db(tmp_path)
    current = datetime(2026, 7, 13, 13, 30, tzinfo=SHANGHAI)
    candidate_timeouts = []
    portfolio_timeouts = []

    class DeadlineAnalyzer(FakeAnalyzer):
        def analyze(self, *, timeout_seconds, symbol, **kwargs):
            candidate_timeouts.append(timeout_seconds)
            return super().analyze(symbol=symbol, **kwargs)

        def decide_portfolio(self, *, timeout_seconds, analyses, **kwargs):
            portfolio_timeouts.append(timeout_seconds)
            return super().decide_portfolio(analyses=analyses, **kwargs)

    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        batch = create_batch(db, config, current=current, snapshot_root=tmp_path)
        batch.analysis_deadline = current + timedelta(seconds=30)
        db.commit()
        batch = claim_pending_batch(db, worker_id="worker-1", current=current)
        processed = process_batch(db, batch, analyzer=DeadlineAnalyzer(), current=current)
        processed_status = processed.status
        processed_error = processed.error_message

    assert processed_status == "ready", processed_error
    assert candidate_timeouts and max(candidate_timeouts) <= 30
    assert portfolio_timeouts and portfolio_timeouts[0] <= 30


def test_stale_quote_blocks_batch_before_llm_work(tmp_path):
    engine = setup_batch_db(tmp_path)
    current = datetime(2026, 7, 13, 13, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        stock = db.scalar(select(Stock).where(Stock.symbol == "600111.SH"))
        stock.quote_updated_at = current - timedelta(minutes=20)
        db.commit()

        with pytest.raises(ValueError, match="实时行情已过期"):
            create_batch(db, config, current=current, snapshot_root=tmp_path)


def test_stale_event_source_blocks_batch_before_llm_work(tmp_path):
    engine = setup_batch_db(tmp_path)
    current = datetime(2026, 7, 13, 13, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        source = db.scalar(
            select(DataSourceState).where(DataSourceState.provider == "akshare_events")
        )
        source.last_checked_at = current - timedelta(hours=1)
        db.commit()

        with pytest.raises(ValueError, match="公司公告数据已过期"):
            create_batch(db, config, current=current, snapshot_root=tmp_path)


def test_worker_freezes_optional_enrichment_before_any_llm_call(tmp_path):
    engine = setup_batch_db(tmp_path)
    current = datetime(2026, 7, 13, 13, 30, tzinfo=SHANGHAI)
    calls = []

    def fake_enrichment(symbols, **kwargs):
        calls.append((list(symbols), kwargs))
        return {
            "source": "yahoo",
            "captured_at": current.isoformat(),
            "symbols": {
                symbol: {"status": "unavailable", "error": "test offline"}
                for symbol in symbols
            },
            "global": {"status": "unavailable", "error": "test offline"},
        }

    class SnapshotAnalyzer(FakeAnalyzer):
        def analyze(self, *, snapshot, symbol, **kwargs):
            assert snapshot["enrichment"]["source"] == "yahoo"
            assert snapshot["enrichment"]["symbol"]["status"] == "unavailable"
            return super().analyze(symbol=symbol, **kwargs)

    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        config.parameters = {**config.parameters, "enrichment_enabled": True}
        db.commit()
        batch = create_batch(db, config, current=current, snapshot_root=tmp_path)
        original_uri = batch.snapshot_uri
        original_hash = batch.snapshot_sha256
        batch = claim_pending_batch(db, worker_id="worker-1", current=current)
        processed = process_batch(
            db,
            batch,
            analyzer=SnapshotAnalyzer(),
            enrichment_collector=fake_enrichment,
            current=current,
        )
        frozen = json.loads(Path(processed.snapshot_uri).read_text(encoding="utf-8"))

    assert processed.status == "ready"
    assert calls and set(calls[0][0]) == set(processed.required_symbols)
    assert frozen["enrichment"]["source"] == "yahoo"
    assert processed.snapshot_sha256 != original_hash
    assert processed.snapshot_uri != original_uri
    assert Path(original_uri).is_file()


def test_worker_rejects_tampered_snapshot_before_any_llm_call(tmp_path):
    engine = setup_batch_db(tmp_path)
    current = datetime(2026, 7, 13, 13, 30, tzinfo=SHANGHAI)

    class MustNotRun(FakeAnalyzer):
        def analyze(self, **kwargs):
            raise AssertionError("tampered snapshot must never reach LLM")

    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        batch = create_batch(db, config, current=current, snapshot_root=tmp_path)
        path = Path(batch.snapshot_uri)
        value = json.loads(path.read_text(encoding="utf-8"))
        value["candidates"][0]["last_price"] = 999
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        batch = claim_pending_batch(db, worker_id="worker-1", current=current)
        processed = process_batch(db, batch, analyzer=MustNotRun(), current=current)
        processed_status = processed.status
        processed_error = processed.error_message

    assert processed_status == "failed"
    assert "哈希" in processed_error


def test_batch_creation_rejects_disabled_configuration(tmp_path):
    engine = setup_batch_db(tmp_path)
    current = datetime(2026, 7, 13, 13, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        config.enabled = False
        db.commit()

        with pytest.raises(ValueError, match="停用"):
            create_batch(db, config, current=current, snapshot_root=tmp_path)


def test_worker_rejects_configuration_change_before_any_llm_call(tmp_path):
    engine = setup_batch_db(tmp_path)
    current = datetime(2026, 7, 13, 13, 30, tzinfo=SHANGHAI)

    class MustNotRun(FakeAnalyzer):
        def analyze(self, **kwargs):
            raise AssertionError("changed configuration must never reach LLM")

    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        batch = create_batch(db, config, current=current, snapshot_root=tmp_path)
        config.parameters = {**config.parameters, "deep_model": "gpt-5.4"}
        db.commit()
        batch = claim_pending_batch(db, worker_id="worker-1", current=current)
        processed = process_batch(db, batch, analyzer=MustNotRun(), current=current)
        status = processed.status
        error = processed.error_message

    assert status == "failed"
    assert "配置" in error
