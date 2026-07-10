from __future__ import annotations

import os


defaults = {
    "SIMULATION_INITIAL_CASH": "10000",
    "SIMULATION_MAX_ORDER_NOTIONAL_ABS": "2000",
    "SIMULATION_MAX_ORDER_NOTIONAL_PCT": "0.20",
    "SIMULATION_MAX_POSITION_PCT": "0.20",
    "SIMULATION_MAX_TOTAL_EXPOSURE_PCT": "0.60",
    "SIMULATION_DAILY_LOSS_LIMIT_PCT": "0.03",
    "SIMULATION_MAX_CONSECUTIVE_ERRORS": "3",
    "MARKET_DATA_STALE_AFTER_SECONDS": "15",
    "CORPORATE_EVENT_STALE_AFTER_SECONDS": "1800",
    "LIVE_TRADING_ENABLED": "false",
}
for key, value in defaults.items():
    os.environ[key] = value

try:
    from app.config import get_settings

    get_settings.cache_clear()
except Exception:
    pass
