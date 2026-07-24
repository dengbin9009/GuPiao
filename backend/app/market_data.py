from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from threading import local
from urllib.parse import parse_qs, urlparse
from typing import Any, Iterable
from zoneinfo import ZoneInfo
import re


class MarketDataError(RuntimeError):
    pass


SHANGHAI = ZoneInfo("Asia/Shanghai")


class StaleDataError(MarketDataError):
    pass


@dataclass(frozen=True)
class ProviderResult:
    provider: str
    data: Any


class MarketDataProvider(ABC):
    name: str
    capabilities: frozenset[str]

    @abstractmethod
    def health(self) -> tuple[bool, str | None]:
        raise NotImplementedError

    @abstractmethod
    def stock_master(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def bars(self, *, symbol: str, timeframe: str, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def trading_days(self, *, start: str, end: str) -> list[str]:
        raise NotImplementedError


class CorporateEventProvider(ABC):
    name: str
    capabilities = frozenset({"corporate_events"})

    @abstractmethod
    def health(self) -> tuple[bool, str | None]:
        raise NotImplementedError

    @abstractmethod
    def events(self, *, symbols: list[str], start: str, end: str) -> list[dict[str, Any]]:
        raise NotImplementedError


class ProviderRouter:
    def __init__(self, providers: Iterable[Any]):
        self.providers = list(providers)

    def call(self, capability: str, method: str, **kwargs: Any) -> ProviderResult:
        failures = []
        for provider in self.providers:
            if capability not in provider.capabilities:
                continue
            healthy, error = provider.health()
            if not healthy:
                failures.append(f"{provider.name}: {error or 'unhealthy'}")
                continue
            try:
                return ProviderResult(provider.name, getattr(provider, method)(**kwargs))
            except Exception as exc:
                failures.append(f"{provider.name}: {exc}")
        detail = "; ".join(failures) or "没有提供对应能力的数据源"
        raise MarketDataError(f"数据源不可用: {detail}")

    def select(self, capability: str) -> Any:
        failures = []
        for provider in self.providers:
            if capability not in provider.capabilities:
                continue
            healthy, error = provider.health()
            if not healthy:
                failures.append(f"{provider.name}: {error or 'unhealthy'}")
                continue
            return provider
        detail = "; ".join(failures) or "没有提供对应能力的数据源"
        raise MarketDataError(f"数据源不可用: {detail}")


def ensure_fresh(
    label: str,
    *,
    updated_at: datetime | None,
    stale_after_seconds: int,
    current: datetime | None = None,
) -> None:
    current = current or datetime.now(SHANGHAI)
    if updated_at is None:
        raise StaleDataError(f"{label}数据缺失")
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=current.tzinfo)
    age_seconds = (current - updated_at).total_seconds()
    if age_seconds < 0:
        raise StaleDataError(f"{label}数据时间在未来")
    if age_seconds > stale_after_seconds:
        raise StaleDataError(f"{label}数据已过期")


def normalize_events(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    severity_by_type = {
        "suspension": "critical",
        "regulatory_investigation": "critical",
        "material_litigation": "critical",
        "major_announcement": "critical",
        "shareholder_reduction": "warning",
        "unlock": "warning",
        "earnings_warning": "warning",
        "resumption": "info",
    }
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in rows:
        row = dict(source)
        key = (str(row.get("source", "unknown")), str(row.get("source_event_id", "")))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        row["severity"] = row.get("severity") or severity_by_type.get(str(row.get("event_type")), "info")
        result.append(row)
    return result


def _records(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        return list(frame.to_dict(orient="records"))
    return list(frame)


def _date_text(value: Any) -> str:
    text = str(value or "").strip().replace("/", "-")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10]


def _optional_number(value: Any) -> float | None:
    return float(value) if value not in {None, ""} else None


def _normalize_bars(
    rows: list[dict[str, Any]],
    *,
    symbol: str,
    provider: str,
    volume_multiplier: float = 1.0,
    amount_multiplier: float = 1.0,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        timestamp = (
            row.get("timestamp")
            or row.get("trade_time")
            or row.get("datetime")
            or row.get("时间")
            or row.get("trade_date")
            or row.get("日期")
        )
        close = row.get("close", row.get("收盘"))
        if timestamp in {None, ""} or close in {None, ""} or float(close) <= 0:
            continue
        text = (
            timestamp.isoformat()
            if isinstance(timestamp, datetime)
            else str(timestamp).replace(" ", "T")
        )
        result.append(
            {
                "symbol": symbol,
                "timestamp": text,
                "open": float(row.get("open", row.get("开盘", close)) or close),
                "high": float(row.get("high", row.get("最高", close)) or close),
                "low": float(row.get("low", row.get("最低", close)) or close),
                "close": float(close),
                "volume": float(
                    row.get("volume", row.get("vol", row.get("成交量", 0))) or 0
                )
                * volume_multiplier,
                "amount": float(row.get("amount", row.get("成交额", 0)) or 0)
                * amount_multiplier,
                "provider": provider,
            }
        )
    if not result:
        raise MarketDataError(f"{provider} K 线返回缺少可用标准字段")
    return result


class AKShareProvider(MarketDataProvider):
    name = "akshare"
    capabilities = frozenset({
        "stock_master",
        "daily",
        "minute",
        "realtime",
        "trading_calendar",
        "etf_master",
        "etf_daily",
    })

    def __init__(self):
        try:
            import akshare as ak

            self.client = ak
            self.import_error = None
        except ImportError as exc:
            self.client = None
            self.import_error = str(exc)

    def health(self) -> tuple[bool, str | None]:
        return self.client is not None, self.import_error

    def stock_master(self) -> list[dict[str, Any]]:
        if not self.client:
            raise MarketDataError("AKShare 未安装")
        try:
            return _records(self.client.stock_info_a_code_name())
        except Exception as exc:
            raise MarketDataError(f"AKShare 股票主数据获取失败: {exc}") from exc

    def bars(self, *, symbol: str, timeframe: str, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        if not self.client:
            raise MarketDataError("AKShare 未安装")
        code = symbol.split(".")[0]
        try:
            if timeframe == "1m":
                return _normalize_bars(
                    _records(
                        self.client.stock_zh_a_hist_min_em(
                            symbol=code,
                            period="1",
                            adjust="qfq",
                        )
                    ),
                    symbol=symbol,
                    provider=self.name,
                    volume_multiplier=100,
                )
            if timeframe == "1d":
                if symbol == "000300.SH":
                    return _records(
                        self.client.index_zh_a_hist(
                            symbol="000300",
                            period="daily",
                            start_date=(start or "19900101").replace("-", ""),
                            end_date=(
                                end or datetime.now(SHANGHAI).date().isoformat()
                            ).replace("-", ""),
                        )
                    )
                return _records(
                    self.client.stock_zh_a_hist(
                        symbol=code,
                        period="daily",
                        start_date=(start or "19900101").replace("-", ""),
                        end_date=(end or datetime.now(SHANGHAI).date().isoformat()).replace("-", ""),
                        adjust="",
                    )
                )
        except Exception as exc:
            raise MarketDataError(f"AKShare K 线获取失败: {exc}") from exc
        raise MarketDataError(f"AKShare 不支持时间粒度 {timeframe}")

    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        if not self.client:
            raise MarketDataError("AKShare 未安装")
        requested = {symbol.split(".")[0] for symbol in symbols}
        try:
            return [row for row in _records(self.client.stock_zh_a_spot_em()) if str(row.get("代码")) in requested]
        except Exception as exc:
            raise MarketDataError(f"AKShare 实时行情获取失败: {exc}") from exc

    def trading_days(self, *, start: str, end: str) -> list[str]:
        if not self.client:
            raise MarketDataError("AKShare 未安装")
        try:
            rows = _records(self.client.tool_trade_date_hist_sina())
        except Exception as exc:
            raise MarketDataError(f"AKShare 交易日历获取失败: {exc}") from exc
        return [str(row.get("trade_date")) for row in rows if start <= str(row.get("trade_date")) <= end]

    def etf_master(self) -> list[dict[str, Any]]:
        if not self.client:
            raise MarketDataError("AKShare 未安装")
        try:
            rows = _records(self.client.fund_etf_spot_em())
        except Exception as exc:
            raise MarketDataError(f"AKShare ETF 主数据获取失败: {exc}") from exc
        result = []
        for row in rows:
            code = str(row.get("代码") or row.get("code") or "")
            name = str(row.get("名称") or row.get("name") or "")
            if not code or not name:
                continue
            suffix = "SH" if code.startswith(("5", "6")) else "SZ"
            result.append(
                {
                    "ts_code": f"{code}.{suffix}",
                    "name": name,
                    "instrument_type": "ETF",
                    "lot_size": 100,
                    "settlement_days": 1,
                }
            )
        return result

    def etf_bars(
        self,
        symbol: str,
        *,
        start: str,
        end: str,
    ) -> list[dict[str, Any]]:
        if not self.client:
            raise MarketDataError("AKShare 未安装")
        try:
            rows = _records(
                self.client.fund_etf_hist_em(
                    symbol=symbol.split(".")[0],
                    period="daily",
                    start_date=start.replace("-", ""),
                    end_date=end.replace("-", ""),
                    adjust="",
                )
            )
        except Exception as exc:
            raise MarketDataError(f"AKShare ETF 日线获取失败: {exc}") from exc
        return [
            {
                "trade_date": _date_text(row.get("日期") or row.get("trade_date")),
                "open": float(row.get("开盘") or row.get("open") or 0),
                "high": float(row.get("最高") or row.get("high") or 0),
                "low": float(row.get("最低") or row.get("low") or 0),
                "close": float(row.get("收盘") or row.get("close") or 0),
                "volume": float(row.get("成交量") or row.get("volume") or 0),
                "amount": float(row.get("成交额") or row.get("amount") or 0),
            }
            for row in rows
        ]


class TushareProvider(MarketDataProvider):
    name = "tushare"
    capabilities = frozenset({
        "stock_master",
        "daily",
        "minute",
        "trading_calendar",
        "corporate_events",
        "adjustment",
        "daily_metric",
        "financial",
        "etf_master",
        "etf_daily",
    })

    def __init__(self, token: str):
        self.token = token
        try:
            import tushare as ts

            self.client = ts.pro_api(token) if token else None
            self.import_error = None if token else "未配置 Tushare Token"
        except ImportError as exc:
            self.client = None
            self.import_error = str(exc)

    def health(self) -> tuple[bool, str | None]:
        return self.client is not None, self.import_error

    def stock_master(self) -> list[dict[str, Any]]:
        if not self.client:
            raise MarketDataError("Tushare 未配置")
        try:
            return _records(self.client.stock_basic(exchange="", list_status="L"))
        except Exception as exc:
            raise MarketDataError(f"Tushare 股票主数据获取失败: {exc}") from exc

    def bars(self, *, symbol: str, timeframe: str, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        if not self.client:
            raise MarketDataError("Tushare 未配置")
        params = {"ts_code": symbol, "start_date": (start or "").replace("-", ""), "end_date": (end or "").replace("-", "")}
        try:
            if timeframe == "1d":
                if symbol == "000300.SH":
                    return _records(self.client.index_daily(**params))
                return _records(self.client.daily(**params))
            if timeframe == "1m" and hasattr(self.client, "stk_mins"):
                return _normalize_bars(
                    _records(self.client.stk_mins(freq="1min", **params)),
                    symbol=symbol,
                    provider=self.name,
                    amount_multiplier=1000,
                )
        except Exception as exc:
            raise MarketDataError(f"Tushare K 线获取失败: {exc}") from exc
        raise MarketDataError(f"当前 Tushare 权限不支持时间粒度 {timeframe}")

    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        raise MarketDataError("Tushare 实时行情能力未启用")

    def trading_days(self, *, start: str, end: str) -> list[str]:
        if not self.client:
            raise MarketDataError("Tushare 未配置")
        try:
            rows = _records(
                self.client.trade_cal(
                    exchange="SSE",
                    start_date=start.replace("-", ""),
                    end_date=end.replace("-", ""),
                    is_open="1",
                )
            )
        except Exception as exc:
            raise MarketDataError(f"Tushare 交易日历获取失败: {exc}") from exc
        return [str(row.get("cal_date")) for row in rows]

    def adjustment_factors(
        self,
        symbol: str,
        *,
        start: str,
        end: str,
    ) -> list[dict[str, Any]]:
        rows = _records(
            self.client.adj_factor(
                ts_code=symbol,
                start_date=start.replace("-", ""),
                end_date=end.replace("-", ""),
            )
        )
        return [
            {
                "trade_date": _date_text(row.get("trade_date")),
                "adjustment_factor": float(row.get("adj_factor") or 0),
            }
            for row in rows
            if row.get("adj_factor") not in {None, ""}
        ]

    def daily_cross_section(self, trade_date: str) -> list[dict[str, Any]]:
        rows = _records(
            self.client.daily(trade_date=trade_date.replace("-", ""))
        )
        return [
            {
                "symbol": str(row.get("ts_code") or "").upper(),
                "trade_date": _date_text(row.get("trade_date")),
                "open": float(row.get("open") or 0),
                "high": float(row.get("high") or 0),
                "low": float(row.get("low") or 0),
                "close": float(row.get("close") or 0),
                "volume": float(row.get("vol") or 0) * 100,
                "amount": float(row.get("amount") or 0) * 1000,
            }
            for row in rows
            if row.get("ts_code") and row.get("close") not in {None, ""}
        ]

    def adjustment_cross_section(
        self,
        trade_date: str,
    ) -> list[dict[str, Any]]:
        rows = _records(
            self.client.adj_factor(trade_date=trade_date.replace("-", ""))
        )
        return [
            {
                "symbol": str(row.get("ts_code") or "").upper(),
                "trade_date": _date_text(row.get("trade_date")),
                "adjustment_factor": float(row.get("adj_factor") or 0),
            }
            for row in rows
            if row.get("ts_code") and row.get("adj_factor") not in {None, ""}
        ]

    def daily_metrics(
        self,
        symbol: str,
        *,
        start: str,
        end: str,
    ) -> list[dict[str, Any]]:
        rows = _records(
            self.client.daily_basic(
                ts_code=symbol,
                start_date=start.replace("-", ""),
                end_date=end.replace("-", ""),
            )
        )
        return [
            {
                "trade_date": _date_text(row.get("trade_date")),
                "pe_ttm": _optional_number(row.get("pe_ttm")),
                "pb": _optional_number(row.get("pb")),
                "dividend_yield": (
                    float(row["dv_ttm"]) / 100
                    if row.get("dv_ttm") not in {None, ""}
                    else None
                ),
                "total_market_value": (
                    float(row["total_mv"]) * 10_000
                    if row.get("total_mv") not in {None, ""}
                    else None
                ),
                "float_market_value": (
                    float(row["circ_mv"]) * 10_000
                    if row.get("circ_mv") not in {None, ""}
                    else None
                ),
            }
            for row in rows
        ]

    def daily_metric_cross_section(
        self,
        trade_date: str,
    ) -> list[dict[str, Any]]:
        rows = _records(
            self.client.daily_basic(
                ts_code="",
                trade_date=trade_date.replace("-", ""),
            )
        )
        return [
            {
                "symbol": str(row.get("ts_code") or "").upper(),
                "trade_date": _date_text(row.get("trade_date")),
                "pe_ttm": _optional_number(row.get("pe_ttm")),
                "pb": _optional_number(row.get("pb")),
                "dividend_yield": (
                    float(row["dv_ttm"]) / 100
                    if row.get("dv_ttm") not in {None, ""}
                    else None
                ),
                "total_market_value": (
                    float(row["total_mv"]) * 10_000
                    if row.get("total_mv") not in {None, ""}
                    else None
                ),
                "float_market_value": (
                    float(row["circ_mv"]) * 10_000
                    if row.get("circ_mv") not in {None, ""}
                    else None
                ),
            }
            for row in rows
            if row.get("ts_code")
        ]

    def financial_reports(self, symbol: str) -> list[dict[str, Any]]:
        indicator = _records(self.client.fina_indicator(ts_code=symbol))
        cashflow = _records(self.client.cashflow(ts_code=symbol))
        income = _records(self.client.income(ts_code=symbol))
        balance = _records(self.client.balancesheet(ts_code=symbol))
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for rows in (indicator, cashflow, income, balance):
            for row in rows:
                period = _date_text(row.get("end_date"))
                if not period:
                    continue
                announcement = _date_text(row.get("f_ann_date") or row.get("ann_date"))
                if not announcement:
                    continue
                value = grouped.setdefault(
                    (period, announcement),
                    {
                        "report_period": period,
                        "announcement_date": announcement,
                        "actual_announcement_date": announcement,
                    },
                )
                if announcement:
                    value["announcement_date"] = announcement
                    value["actual_announcement_date"] = announcement
                mappings = {
                    "eps": ("eps", 1),
                    "roe": ("roe", 0.01),
                    "gross_margin": ("grossprofit_margin", 0.01),
                    "operating_cash_flow": ("n_cashflow_act", 1),
                    "net_profit": ("n_income", 1),
                    "revenue": ("revenue", 1),
                    "total_assets": ("total_assets", 1),
                    "total_liabilities": ("total_liab", 1),
                }
                for target, (source, multiplier) in mappings.items():
                    if row.get(source) not in {None, ""}:
                        value[target] = float(row[source]) * multiplier
        return [
            value
            for _, value in sorted(grouped.items())
            if value.get("actual_announcement_date")
        ]

    def financial_report_cross_sections(
        self,
        periods: Iterable[str],
    ) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for period in periods:
            compact_period = str(period).replace("-", "")
            datasets = (
                _records(self.client.fina_indicator_vip(period=compact_period)),
                _records(self.client.cashflow_vip(period=compact_period)),
                _records(self.client.income_vip(period=compact_period)),
                _records(self.client.balancesheet_vip(period=compact_period)),
            )
            for rows in datasets:
                for row in rows:
                    symbol = str(row.get("ts_code") or "").upper()
                    report_period = _date_text(row.get("end_date"))
                    announcement = _date_text(
                        row.get("f_ann_date") or row.get("ann_date")
                    )
                    if not symbol or not report_period or not announcement:
                        continue
                    value = grouped.setdefault(
                        (symbol, report_period, announcement),
                        {
                            "symbol": symbol,
                            "report_period": report_period,
                            "announcement_date": announcement,
                            "actual_announcement_date": announcement,
                        },
                    )
                    mappings = {
                        "eps": ("eps", 1),
                        "roe": ("roe", 0.01),
                        "gross_margin": ("grossprofit_margin", 0.01),
                        "operating_cash_flow": ("n_cashflow_act", 1),
                        "net_profit": ("n_income", 1),
                        "revenue": ("revenue", 1),
                        "total_assets": ("total_assets", 1),
                        "total_liabilities": ("total_liab", 1),
                    }
                    for target, (field, multiplier) in mappings.items():
                        if row.get(field) not in {None, ""}:
                            value[target] = float(row[field]) * multiplier
        return [grouped[key] for key in sorted(grouped)]

    def etf_master(self) -> list[dict[str, Any]]:
        rows = _records(self.client.fund_basic(market="E", status="L"))
        return [
            {
                "ts_code": row.get("ts_code"),
                "name": row.get("name"),
                "list_date": row.get("list_date"),
                "instrument_type": "ETF",
                "lot_size": 100,
                "settlement_days": 1,
            }
            for row in rows
        ]

    def etf_bars(
        self,
        symbol: str,
        *,
        start: str,
        end: str,
    ) -> list[dict[str, Any]]:
        rows = _records(
            self.client.fund_daily(
                ts_code=symbol,
                start_date=start.replace("-", ""),
                end_date=end.replace("-", ""),
            )
        )
        return [
            {
                "trade_date": _date_text(row.get("trade_date")),
                "open": float(row.get("open") or 0),
                "high": float(row.get("high") or 0),
                "low": float(row.get("low") or 0),
                "close": float(row.get("close") or 0),
                "volume": float(row.get("vol") or 0) * 100,
                "amount": float(row.get("amount") or 0) * 1000,
            }
            for row in rows
        ]


class MootdxProvider(MarketDataProvider):
    name = "mootdx"
    capabilities = frozenset({"minute", "hour", "realtime", "finance"})

    def __init__(self):
        try:
            from mootdx.quotes import Quotes

            self.Quotes = Quotes
            self.client = None
            self._thread_clients = local()
            self.import_error = None
        except Exception as exc:
            self.Quotes = None
            self.client = None
            self._thread_clients = local()
            self.import_error = str(exc)

    def health(self) -> tuple[bool, str | None]:
        return self.Quotes is not None, self.import_error

    def stock_master(self) -> list[dict[str, Any]]:
        raise MarketDataError("mootdx 不提供股票主数据")

    def _quotes(self):
        if not self.Quotes:
            raise MarketDataError("mootdx 未安装")
        if self.client is not None:
            return self.client
        client = getattr(self._thread_clients, "client", None)
        if client is None:
            try:
                client = self.Quotes.factory(market="std")
                self._thread_clients.client = client
            except Exception as exc:
                raise MarketDataError(f"mootdx 连接失败: {exc}") from exc
        return client

    def bars(self, *, symbol: str, timeframe: str, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        if timeframe not in {"1m", "60m"}:
            raise MarketDataError(f"mootdx 不支持时间粒度 {timeframe}")
        quotes = self._quotes()
        code, _, suffix = symbol.partition(".")
        market = 1 if suffix.upper() == "SH" else 0
        frequency = 8 if timeframe == "1m" else 3
        try:
            rows = quotes.bars(symbol=code, market=market, frequency=frequency)
        except Exception as exc:
            label = "分钟线" if timeframe == "1m" else "小时线"
            raise MarketDataError(f"mootdx {label}获取失败: {exc}") from exc
        records = _records(rows)
        result: list[dict[str, Any]] = []
        for row in records:
            timestamp = row.get("datetime") or row.get("date") or row.get("time")
            if not timestamp:
                continue
            if isinstance(timestamp, datetime):
                ts = timestamp.isoformat()
            else:
                text = str(timestamp)
                if len(text) == 16:
                    text = f"{text}:00"
                ts = text.replace(" ", "T")
            if start and ts[:10] < start:
                continue
            if end and ts[:10] > end:
                continue
            result.append(
                {
                    "symbol": symbol,
                    "timestamp": ts,
                    "open": float(row.get("open", 0)),
                    "high": float(row.get("high", 0)),
                    "low": float(row.get("low", 0)),
                    "close": float(row.get("close", 0)),
                    "volume": int(float(row.get("volume", 0) or 0)),
                    "amount": float(row.get("amount", row.get("money", 0)) or 0),
                    "provider": "mootdx",
                }
            )
        return result

    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        quotes = self._quotes()
        grouped: dict[int, list[str]] = {0: [], 1: []}
        for symbol in symbols:
            code, _, suffix = symbol.partition(".")
            if suffix.upper() == "BJ":
                continue
            grouped[1 if suffix.upper() == "SH" else 0].append(code)
        result: list[dict[str, Any]] = []
        for market, codes in grouped.items():
            for offset in range(0, len(codes), 80):
                try:
                    records = _records(
                        quotes.quotes(symbol=codes[offset : offset + 80], market=market)
                    )
                except Exception as exc:
                    raise MarketDataError(f"mootdx 实时行情获取失败: {exc}") from exc
                for row in records:
                    price = float(row.get("price", 0) or 0)
                    previous_close = float(row.get("last_close", 0) or 0)
                    server_time = str(row.get("servertime", "")).split(".")[0]
                    quote_at = None
                    if server_time:
                        try:
                            quote_at = datetime.combine(
                                datetime.now(SHANGHAI).date(),
                                datetime.strptime(server_time, "%H:%M:%S").time(),
                                tzinfo=SHANGHAI,
                            )
                        except ValueError:
                            quote_at = None
                    change_pct = (
                        (price - previous_close) / previous_close * 100
                        if previous_close
                        else 0.0
                    )
                    result.append(
                        {
                            "代码": str(row.get("code", "")),
                            "最新价": price,
                            "涨跌幅": change_pct,
                            "成交额": float(row.get("amount", 0) or 0),
                            "open_price": float(row.get("open", 0) or 0),
                            "high_price": float(row.get("high", 0) or 0),
                            "low_price": float(row.get("low", 0) or 0),
                            "volume": float(
                                row.get("volume", row.get("vol", 0)) or 0
                            )
                            * 100,
                            "previous_close": previous_close,
                            "quote_at": quote_at,
                        }
                    )
        return result

    def finance(self, symbol: str) -> dict[str, Any]:
        code = symbol.split(".")[0]
        try:
            records = _records(self._quotes().finance(symbol=code))
        except Exception as exc:
            raise MarketDataError(f"mootdx 财务信息获取失败: {exc}") from exc
        if not records:
            raise MarketDataError(f"mootdx 财务信息缺失: {symbol}")
        row = records[0]
        ipo_date = str(row.get("ipo_date", "")).split(".")[0]
        listing_date = None
        if len(ipo_date) == 8 and ipo_date.isdigit():
            listing_date = f"{ipo_date[:4]}-{ipo_date[4:6]}-{ipo_date[6:]}"
        return {
            "float_shares": float(row.get("liutongguben", 0) or 0),
            "listing_date": listing_date,
        }

    def trading_days(self, *, start: str, end: str) -> list[str]:
        raise MarketDataError("mootdx 不提供交易日历")


class CNInfoEventProvider(CorporateEventProvider):
    name = "cninfo"

    def __init__(self, fetcher=None):
        self.fetcher = fetcher

    def health(self) -> tuple[bool, str | None]:
        return self.fetcher is not None, None if self.fetcher else "未配置 CNINFO 抓取器"

    def events(self, *, symbols: list[str], start: str, end: str) -> list[dict[str, Any]]:
        if not self.fetcher:
            raise MarketDataError("CNINFO 抓取器未配置")
        return normalize_events(self.fetcher(symbols=symbols, start=start, end=end))


class AKShareEventProvider(CorporateEventProvider):
    name = "akshare_events"

    def __init__(self, client=None):
        if client is not None:
            self.client = client
            self.import_error = None
            return
        try:
            import akshare as ak

            self.client = ak
            self.import_error = None
        except ImportError as exc:
            self.client = None
            self.import_error = str(exc)

    def health(self) -> tuple[bool, str | None]:
        return self.client is not None, self.import_error

    def events(
        self,
        *,
        symbols: list[str],
        start: str,
        end: str,
    ) -> list[dict[str, Any]]:
        if not self.client:
            raise MarketDataError("AKShare 未安装")
        requested = set(symbols)
        rows: list[dict[str, Any]] = []
        current = datetime.fromisoformat(start).date()
        end_date = datetime.fromisoformat(end).date()
        while current <= end_date:
            try:
                records = _records(
                    self.client.stock_notice_report(
                        symbol="全部",
                        date=current.strftime("%Y%m%d"),
                    )
                )
            except Exception as exc:
                raise MarketDataError(f"AKShare 公司公告获取失败: {exc}") from exc
            for row in records:
                code = str(row.get("代码", "")).zfill(6)
                suffix = "SH" if code.startswith(("5", "6", "7")) else "SZ"
                symbol = f"{code}.{suffix}"
                if symbol not in requested:
                    continue
                title = str(row.get("公告标题", "")).strip()
                raw_uri = str(row.get("网址", "")).strip()
                event_id = _announcement_id(raw_uri) or f"{code}-{current:%Y%m%d}-{title}"
                rows.append(
                    {
                        "source": "akshare",
                        "source_event_id": event_id[:128],
                        "symbol": symbol,
                        "title": title,
                        "event_type": _announcement_event_type(title),
                        "unlock_free_float_pct": _unlock_percentage(title),
                        "published_at": _announcement_datetime(
                            row.get("公告日期"),
                            current,
                        ),
                        "raw_uri": raw_uri or None,
                    }
                )
            current = current.fromordinal(current.toordinal() + 1)
        return normalize_events(rows)


def _announcement_id(raw_uri: str) -> str:
    if not raw_uri:
        return ""
    query_id = parse_qs(urlparse(raw_uri).query).get("announcementId", [""])[0]
    if query_id:
        return query_id
    filename = urlparse(raw_uri).path.rsplit("/", 1)[-1].split(".", 1)[0]
    return filename


def _announcement_event_type(title: str) -> str:
    rules = (
        (("停牌",), "suspension"),
        (("复牌",), "resumption"),
        (("立案", "调查"), "regulatory_investigation"),
        (("诉讼", "仲裁"), "material_litigation"),
        (("减持",), "shareholder_reduction"),
        (("解禁", "限售股上市"), "unlock"),
        (("业绩预告", "业绩预警"), "earnings_warning"),
        (("重大事项", "重大资产", "重大合同", "重大投资", "重大交易"), "major_announcement"),
    )
    for keywords, event_type in rules:
        if any(keyword in title for keyword in keywords):
            return event_type
    return "announcement"


def _unlock_percentage(title: str) -> float | None:
    if _announcement_event_type(title) != "unlock":
        return None
    if not any(keyword in title for keyword in ("流通股", "流通股份")):
        return None
    values = [float(value) / 100 for value in re.findall(r"(\d+(?:\.\d+)?)%", title)]
    return max(values) if values else None


def _announcement_datetime(value: Any, fallback: date) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(SHANGHAI) if value.tzinfo else value.replace(tzinfo=SHANGHAI)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=SHANGHAI)
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.astimezone(SHANGHAI) if parsed.tzinfo else parsed.replace(tzinfo=SHANGHAI)
    except ValueError:
        return datetime.combine(fallback, datetime.min.time(), tzinfo=SHANGHAI)
