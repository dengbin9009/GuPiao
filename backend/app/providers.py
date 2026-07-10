from __future__ import annotations

from .config import get_settings
from .market_data import AKShareProvider, MootdxProvider, ProviderRouter, TushareProvider
from .trading_calendar import TradingCalendarService


def market_router() -> ProviderRouter:
    settings = get_settings()
    providers = [AKShareProvider(), TushareProvider(settings.tushare_token), MootdxProvider()]
    if settings.market_provider == "tushare":
        providers.reverse()
    return ProviderRouter(providers)


def trading_calendar_service() -> TradingCalendarService:
    return TradingCalendarService(market_router().providers)
