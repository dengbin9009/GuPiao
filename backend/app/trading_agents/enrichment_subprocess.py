from __future__ import annotations

import contextlib
import json
import sys
from datetime import date, timedelta


def _to_yahoo_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    return f"{normalized[:-3]}.SS" if normalized.endswith(".SH") else normalized


def _symbol_data(request: dict[str, str]) -> dict[str, str]:
    from tradingagents.dataflows.y_finance import (
        get_balance_sheet,
        get_cashflow,
        get_fundamentals,
        get_income_statement,
        get_insider_transactions,
    )
    from tradingagents.dataflows.yfinance_news import get_news_yfinance

    symbol = _to_yahoo_symbol(request["symbol"])
    trading_date = request["trading_date"]
    news_start = (date.fromisoformat(trading_date) - timedelta(days=7)).isoformat()
    fundamentals = get_fundamentals(symbol, trading_date)
    price_labels = (
        "52 Week High:",
        "52 Week Low:",
        "50 Day Average:",
        "200 Day Average:",
    )
    fundamentals = "\n".join(
        line for line in fundamentals.splitlines() if not line.startswith(price_labels)
    )
    return {
        "source": "yahoo",
        "status": "available",
        "fundamentals": fundamentals,
        "balance_sheet": get_balance_sheet(symbol, "quarterly", trading_date),
        "cashflow": get_cashflow(symbol, "quarterly", trading_date),
        "income_statement": get_income_statement(symbol, "quarterly", trading_date),
        "insider_transactions": get_insider_transactions(symbol),
        "company_news": get_news_yfinance(symbol, news_start, trading_date),
    }


def _global_data(request: dict[str, str]) -> dict[str, str]:
    from tradingagents.dataflows.yfinance_news import get_global_news_yfinance

    return {
        "source": "yahoo",
        "status": "available",
        "global_news": get_global_news_yfinance(request["trading_date"], 7, 20),
    }


def main() -> None:
    request = json.loads(sys.stdin.read())
    with contextlib.redirect_stdout(sys.stderr):
        if request.get("mode") == "symbol":
            result = _symbol_data(request)
        elif request.get("mode") == "global":
            result = _global_data(request)
        else:
            raise ValueError("未知补充数据模式")
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
