from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.market_data import MarketDataError
from app.providers import market_router
from app.recent_overnight_backtest import run_recent_overnight_backtest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行最近两日隔夜分钟线回测")
    parser.add_argument("--symbol", required=True, help="股票代码，例如 000001.SZ")
    parser.add_argument("--entry-date", required=True, help="入场日期，格式 YYYY-MM-DD")
    parser.add_argument("--exit-date", required=True, help="出场日期，格式 YYYY-MM-DD")
    parser.add_argument("--initial-cash", type=float, default=10000, help="初始资金，默认 10000")
    parser.add_argument("--commission-rate", type=float, default=0.0003, help="佣金费率，默认万三")
    parser.add_argument("--min-commission", type=float, default=5, help="最低佣金，默认 5 元")
    parser.add_argument("--stamp-tax-rate", type=float, default=0.0005, help="印花税费率，默认万五")
    parser.add_argument("--transfer-fee-rate", type=float, default=0.0, help="过户费费率，默认 0")
    parser.add_argument("--slippage-bps", type=float, default=5, help="滑点基点，默认 5bp")
    parser.add_argument("--entry-time", default="14:45", help="入场时间，默认 14:45")
    parser.add_argument("--exit-time", default="09:35", help="出场时间，默认 09:35")
    parser.add_argument("--preferred-timeframe", default="1m", choices=["1m", "60m"], help="优先使用的时间粒度，默认 1m")
    parser.add_argument("--cache-root", default="../data/market", help="分钟线缓存目录")
    parser.add_argument("--no-fetch", action="store_true", help="不允许在线补拉，只使用本地缓存")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    provider = None if args.no_fetch else market_router()
    try:
        result = run_recent_overnight_backtest(
            symbol=args.symbol,
            entry_date=args.entry_date,
            exit_date=args.exit_date,
            cache_root=Path(args.cache_root),
            provider=provider,
            initial_cash=args.initial_cash,
            commission_rate=args.commission_rate,
            min_commission=args.min_commission,
            stamp_tax_rate=args.stamp_tax_rate,
            transfer_fee_rate=args.transfer_fee_rate,
            slippage_bps=args.slippage_bps,
            entry_time=args.entry_time,
            exit_time=args.exit_time,
            preferred_timeframe=args.preferred_timeframe,
        )
    except (ValueError, MarketDataError) as exc:
        print(f"回测失败: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        f"回测完成: {result['symbol']} "
        f"入场 {result['entry']['timestamp']} @ {result['entry']['price']}, "
        f"出场 {result['exit']['timestamp']} @ {result['exit']['price']}, "
        f"净收益 {result['net_pnl']}, 收益率 {result['return_pct']:.4%}"
    )


if __name__ == "__main__":
    main()
