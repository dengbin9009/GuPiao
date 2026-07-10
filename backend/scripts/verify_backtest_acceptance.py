from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.backtest_engine import BacktestEngine, BacktestRequest
from app.config import Settings
from app.database import Base
from app.models import StrategyDefinition, Stock
from app.services import create_backtest, generated_bars, seed_database


def main() -> None:
    db_path = Path(__file__).resolve().parents[1] / ".tmp-backtest-acceptance.db"
    if db_path.exists():
        db_path.unlink()
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        seed_database(db, Settings(database_url=f"sqlite:///{db_path}"))
        definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        run = create_backtest(db, definition, {"timeframe": "1m", "initial_cash": 10000, "parameters": {}})
        assert run.status == "completed"
        assert run.metrics["trade_count"] == 2
        assert run.initial_cash == 10000

        engine_core = BacktestEngine()
        bars = generated_bars(stock, 12)
        result = engine_core.run(
            BacktestRequest(
                symbol=stock.symbol,
                bars=bars,
                initial_cash=10000,
                commission_rate=0.0003,
                min_commission=5,
                stamp_tax_rate=0.0005,
                transfer_fee_rate=0.0,
                slippage_bps=5,
                buy_index=2,
                sell_index=8,
                quantity=150,
            )
        )
        assert result.trades[0]["quantity"] == 100
        assert len(result.equity_curve) == 2

    print("backtest_acceptance_ok")


if __name__ == "__main__":
    main()
