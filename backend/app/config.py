from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    environment: str = "development"
    secret_key: str = "development-only-change-me"
    admin_username: str = "admin"
    admin_password: str = "admin123"
    allowed_ips: str = ""
    database_url: str = "sqlite:///./gupiao.db"
    cors_origins: str = "http://localhost:5173,http://localhost:8080"
    trusted_plugin_dir: str = "../data/plugins"
    market_provider: str = "akshare"
    tushare_token: str = ""
    realtime_poll_seconds: int = 5
    market_stale_seconds: int = 15
    market_http_connect_timeout_seconds: int = 5
    market_http_read_timeout_seconds: int = 20
    corporate_event_sync_seconds: int = 300
    corporate_event_stale_seconds: int = 1800
    simulation_initial_cash: float = 10000
    simulation_commission_rate: float = 0.0003
    simulation_min_commission: float = 5
    simulation_stamp_tax_rate: float = 0.0005
    simulation_transfer_fee_rate: float = 0
    simulation_slippage_bps: float = 5
    simulation_max_order_notional_abs: float = 2000
    simulation_max_order_notional_pct: float = 0.20
    simulation_max_position_pct: float = 0.20
    simulation_max_total_exposure_pct: float = 0.60
    simulation_daily_loss_limit_pct: float = 0.03
    simulation_max_consecutive_errors: int = 3
    live_enabled: bool = False
    live_max_order_notional_abs: float = 5000
    live_max_order_notional_pct: float = 0.05
    live_max_position_pct: float = 0.10
    live_max_total_exposure_pct: float = 0.30
    live_daily_loss_limit_pct: float = 0.01
    live_max_consecutive_errors: int = 3
    live_max_daily_orders: int = 5
    broker_adapter: str = "simulation"
    qmt_url: str = ""
    qmt_token: str = ""
    ptrade_url: str = ""
    ptrade_token: str = ""
    futu_host: str = "127.0.0.1"
    futu_port: int = 11111
    futu_trd_market: str = "HK"
    futu_security_firm: str = "FUTUSECURITIES"
    futu_trd_env: str = "SIMULATE"
    futu_unlock_password: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    notification_email_to: str = ""
    wecom_webhook_url: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", os.getenv("GUPIAO_ENV", self.environment))
        object.__setattr__(self, "secret_key", os.getenv("GUPIAO_SECRET_KEY", self.secret_key))
        object.__setattr__(self, "admin_username", os.getenv("GUPIAO_ADMIN_USERNAME", self.admin_username))
        object.__setattr__(self, "admin_password", os.getenv("GUPIAO_ADMIN_PASSWORD", self.admin_password))
        object.__setattr__(self, "allowed_ips", os.getenv("GUPIAO_ALLOWED_IPS", self.allowed_ips))
        object.__setattr__(self, "database_url", os.getenv("DATABASE_URL", self.database_url))
        object.__setattr__(self, "cors_origins", os.getenv("CORS_ORIGINS", self.cors_origins))
        object.__setattr__(self, "trusted_plugin_dir", os.getenv("TRUSTED_PLUGIN_DIR", self.trusted_plugin_dir))
        object.__setattr__(self, "market_provider", os.getenv("MARKET_DATA_PROVIDER", self.market_provider))
        object.__setattr__(self, "tushare_token", os.getenv("TUSHARE_TOKEN", self.tushare_token))
        object.__setattr__(self, "realtime_poll_seconds", _int("REALTIME_POLL_INTERVAL_SECONDS", self.realtime_poll_seconds))
        object.__setattr__(self, "market_stale_seconds", _int("MARKET_DATA_STALE_AFTER_SECONDS", self.market_stale_seconds))
        object.__setattr__(self, "market_http_connect_timeout_seconds", _int("MARKET_DATA_HTTP_CONNECT_TIMEOUT_SECONDS", self.market_http_connect_timeout_seconds))
        object.__setattr__(self, "market_http_read_timeout_seconds", _int("MARKET_DATA_HTTP_READ_TIMEOUT_SECONDS", self.market_http_read_timeout_seconds))
        object.__setattr__(self, "corporate_event_sync_seconds", _int("CORPORATE_EVENT_SYNC_INTERVAL_SECONDS", self.corporate_event_sync_seconds))
        object.__setattr__(self, "corporate_event_stale_seconds", _int("CORPORATE_EVENT_STALE_AFTER_SECONDS", self.corporate_event_stale_seconds))
        object.__setattr__(self, "simulation_initial_cash", _float("SIMULATION_INITIAL_CASH", self.simulation_initial_cash))
        object.__setattr__(self, "simulation_commission_rate", _float("SIMULATION_COMMISSION_RATE", self.simulation_commission_rate))
        object.__setattr__(self, "simulation_min_commission", _float("SIMULATION_MIN_COMMISSION", self.simulation_min_commission))
        object.__setattr__(self, "simulation_stamp_tax_rate", _float("SIMULATION_STAMP_TAX_RATE", self.simulation_stamp_tax_rate))
        object.__setattr__(self, "simulation_transfer_fee_rate", _float("SIMULATION_TRANSFER_FEE_RATE", self.simulation_transfer_fee_rate))
        object.__setattr__(self, "simulation_slippage_bps", _float("SIMULATION_SLIPPAGE_BPS", self.simulation_slippage_bps))
        object.__setattr__(self, "simulation_max_order_notional_abs", _float("SIMULATION_MAX_ORDER_NOTIONAL_ABS", self.simulation_max_order_notional_abs))
        object.__setattr__(self, "simulation_max_order_notional_pct", _float("SIMULATION_MAX_ORDER_NOTIONAL_PCT", self.simulation_max_order_notional_pct))
        object.__setattr__(self, "simulation_max_position_pct", _float("SIMULATION_MAX_POSITION_PCT", self.simulation_max_position_pct))
        object.__setattr__(self, "simulation_max_total_exposure_pct", _float("SIMULATION_MAX_TOTAL_EXPOSURE_PCT", self.simulation_max_total_exposure_pct))
        object.__setattr__(self, "simulation_daily_loss_limit_pct", _float("SIMULATION_DAILY_LOSS_LIMIT_PCT", self.simulation_daily_loss_limit_pct))
        object.__setattr__(self, "simulation_max_consecutive_errors", _int("SIMULATION_MAX_CONSECUTIVE_ERRORS", self.simulation_max_consecutive_errors))
        object.__setattr__(self, "live_enabled", _bool("LIVE_TRADING_ENABLED", self.live_enabled))
        object.__setattr__(self, "live_max_order_notional_abs", _float("LIVE_MAX_ORDER_NOTIONAL_ABS", self.live_max_order_notional_abs))
        object.__setattr__(self, "live_max_order_notional_pct", _float("LIVE_MAX_ORDER_NOTIONAL_PCT", self.live_max_order_notional_pct))
        object.__setattr__(self, "live_max_position_pct", _float("LIVE_MAX_POSITION_PCT", self.live_max_position_pct))
        object.__setattr__(self, "live_max_total_exposure_pct", _float("LIVE_MAX_TOTAL_EXPOSURE_PCT", self.live_max_total_exposure_pct))
        object.__setattr__(self, "live_daily_loss_limit_pct", _float("LIVE_DAILY_LOSS_LIMIT_PCT", self.live_daily_loss_limit_pct))
        object.__setattr__(self, "live_max_consecutive_errors", _int("LIVE_MAX_CONSECUTIVE_ERRORS", self.live_max_consecutive_errors))
        object.__setattr__(self, "live_max_daily_orders", _int("LIVE_MAX_DAILY_ORDERS", self.live_max_daily_orders))
        object.__setattr__(self, "broker_adapter", os.getenv("BROKER_ADAPTER", self.broker_adapter))
        object.__setattr__(self, "qmt_url", os.getenv("QMT_GATEWAY_URL", self.qmt_url))
        object.__setattr__(self, "qmt_token", os.getenv("QMT_GATEWAY_TOKEN", self.qmt_token))
        object.__setattr__(self, "ptrade_url", os.getenv("PTRADE_GATEWAY_URL", self.ptrade_url))
        object.__setattr__(self, "ptrade_token", os.getenv("PTRADE_GATEWAY_TOKEN", self.ptrade_token))
        object.__setattr__(self, "futu_host", os.getenv("FUTU_OPEND_HOST", self.futu_host))
        object.__setattr__(self, "futu_port", _int("FUTU_OPEND_PORT", self.futu_port))
        object.__setattr__(self, "futu_trd_market", os.getenv("FUTU_TRD_MARKET", self.futu_trd_market))
        object.__setattr__(self, "futu_security_firm", os.getenv("FUTU_SECURITY_FIRM", self.futu_security_firm))
        object.__setattr__(self, "futu_trd_env", os.getenv("FUTU_TRD_ENV", self.futu_trd_env))
        object.__setattr__(self, "futu_unlock_password", os.getenv("FUTU_UNLOCK_PASSWORD", self.futu_unlock_password))
        object.__setattr__(self, "smtp_host", os.getenv("SMTP_HOST", self.smtp_host))
        object.__setattr__(self, "smtp_port", _int("SMTP_PORT", self.smtp_port))
        object.__setattr__(self, "smtp_username", os.getenv("SMTP_USERNAME", self.smtp_username))
        object.__setattr__(self, "smtp_password", os.getenv("SMTP_PASSWORD", self.smtp_password))
        object.__setattr__(self, "smtp_from", os.getenv("SMTP_FROM", self.smtp_from))
        object.__setattr__(self, "notification_email_to", os.getenv("NOTIFICATION_EMAIL_TO", self.notification_email_to))
        object.__setattr__(self, "wecom_webhook_url", os.getenv("WECOM_WEBHOOK_URL", self.wecom_webhook_url))


def live_runtime_is_open(settings: Settings) -> bool:
    return settings.live_enabled and settings.broker_adapter != "simulation"


@lru_cache
def get_settings() -> Settings:
    return Settings()
