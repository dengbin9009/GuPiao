from __future__ import annotations

from .config import get_settings
from .market_data import (
    AKShareEventProvider,
    AKShareProvider,
    install_default_requests_timeout,
    MootdxProvider,
    ProviderRouter,
    TushareProvider,
)
from .trading_calendar import TradingCalendarService


def market_router() -> ProviderRouter:
    settings = get_settings()
    install_default_requests_timeout(
        connect_seconds=settings.market_http_connect_timeout_seconds,
        read_seconds=settings.market_http_read_timeout_seconds,
    )
    providers = [AKShareProvider(), TushareProvider(settings.tushare_token), MootdxProvider()]
    if settings.market_provider == "tushare":
        providers.reverse()
    return ProviderRouter(providers)


def trading_calendar_service() -> TradingCalendarService:
    return TradingCalendarService(market_router().providers)


def corporate_event_router() -> ProviderRouter:
    return ProviderRouter([AKShareEventProvider()])
