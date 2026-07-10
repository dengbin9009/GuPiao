from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class BacktestRequest:
    symbol: str
    bars: list[dict[str, Any]]
    initial_cash: float
    commission_rate: float
    min_commission: float
    stamp_tax_rate: float
    transfer_fee_rate: float
    slippage_bps: float
    buy_index: int
    sell_index: int
    quantity: int


@dataclass(frozen=True)
class BacktestResult:
    trades: list[dict[str, Any]]
    metrics: dict[str, Any]
    equity_curve: list[dict[str, Any]]


class BacktestEngine:
    def run(self, request: BacktestRequest) -> BacktestResult:
        bars = request.bars
        if len(bars) <= max(request.buy_index, request.sell_index):
            raise ValueError("回测数据不足")
        quantity = math.floor(request.quantity / 100) * 100
        if quantity <= 0:
            raise ValueError("A股回测数量必须至少为一手")
        buy_bar = bars[request.buy_index]
        sell_bar = bars[request.sell_index]
        buy_price = float(buy_bar["close"]) * (1 + request.slippage_bps / 10_000)
        sell_price = float(sell_bar["close"]) * (1 - request.slippage_bps / 10_000)
        buy_notional = buy_price * quantity
        sell_notional = sell_price * quantity
        buy_commission = max(buy_notional * request.commission_rate, request.min_commission)
        sell_commission = max(sell_notional * request.commission_rate, request.min_commission)
        sell_stamp_tax = sell_notional * request.stamp_tax_rate
        sell_transfer_fee = sell_notional * request.transfer_fee_rate
        realized_pnl = sell_notional - sell_commission - sell_stamp_tax - sell_transfer_fee - buy_notional - buy_commission
        final_equity = request.initial_cash + realized_pnl

        trades = [
            {
                "side": "buy",
                "symbol": request.symbol,
                "quantity": quantity,
                "fill_price": round(buy_price, 4),
                "filled_at": buy_bar["timestamp"],
                "commission": round(buy_commission, 4),
                "reason": "尾盘候选入场",
            },
            {
                "side": "sell",
                "symbol": request.symbol,
                "quantity": quantity,
                "fill_price": round(sell_price, 4),
                "filled_at": sell_bar["timestamp"],
                "commission": round(sell_commission, 4),
                "stamp_tax": round(sell_stamp_tax, 4),
                "transfer_fee": round(sell_transfer_fee, 4),
                "realized_pnl": round(realized_pnl, 4),
                "reason": "次日退出",
            },
        ]
        equity_curve = [
            {"timestamp": bars[0]["timestamp"], "equity": request.initial_cash},
            {"timestamp": sell_bar["timestamp"], "equity": round(final_equity, 4)},
        ]
        metrics = {
            "cumulative_return": round(realized_pnl / request.initial_cash, 4),
            "annualized_return": round((realized_pnl / request.initial_cash) * 12, 4),
            "benchmark_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe_ratio": 0.0,
            "win_rate": 1.0 if realized_pnl > 0 else 0.0,
            "profit_factor": 0.0 if realized_pnl <= 0 else round(realized_pnl / max(buy_commission + sell_commission, 1), 4),
            "average_win_loss": 0.0 if realized_pnl <= 0 else 1.0,
            "turnover": round((buy_notional + sell_notional) / request.initial_cash, 4),
            "exposure": round(buy_notional / request.initial_cash, 4),
            "trade_count": 2,
        }
        return BacktestResult(trades=trades, metrics=metrics, equity_curve=equity_curve)
