from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
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


class AKShareProvider(MarketDataProvider):
    name = "akshare"
    capabilities = frozenset({"stock_master", "daily", "minute", "realtime", "trading_calendar"})

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
                return _records(self.client.stock_zh_a_hist_min_em(symbol=code, period="1", adjust="qfq"))
            if timeframe == "1d":
                return _records(
                    self.client.stock_zh_a_hist(
                        symbol=code,
                        period="daily",
                        start_date=(start or "19900101").replace("-", ""),
                        end_date=(end or datetime.now(SHANGHAI).date().isoformat()).replace("-", ""),
                        adjust="qfq",
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


class TushareProvider(MarketDataProvider):
    name = "tushare"
    capabilities = frozenset({"stock_master", "daily", "minute", "trading_calendar", "corporate_events"})

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
                return _records(self.client.daily(**params))
            if timeframe == "1m" and hasattr(self.client, "stk_mins"):
                return _records(self.client.stk_mins(freq="1min", **params))
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


class MootdxProvider(MarketDataProvider):
    name = "mootdx"
    capabilities = frozenset({"minute", "hour", "realtime"})

    def __init__(self):
        try:
            from mootdx.quotes import Quotes

            self.Quotes = Quotes
            self.client = None
            self.import_error = None
        except Exception as exc:
            self.Quotes = None
            self.client = None
            self.import_error = str(exc)

    def health(self) -> tuple[bool, str | None]:
        return self.Quotes is not None, self.import_error

    def stock_master(self) -> list[dict[str, Any]]:
        raise MarketDataError("mootdx 不提供股票主数据")

    def _quotes(self):
        if not self.Quotes:
            raise MarketDataError("mootdx 未安装")
        if self.client is None:
            try:
                self.client = self.Quotes.factory(market="std")
            except Exception as exc:
                raise MarketDataError(f"mootdx 连接失败: {exc}") from exc
        return self.client

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
                            "quote_at": quote_at,
                        }
                    )
        return result

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
