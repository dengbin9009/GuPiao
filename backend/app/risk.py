from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class RiskProfile(Protocol):
    emergency_stop_enabled: bool
    max_order_notional_abs: float
    max_order_notional_pct: float
    max_position_pct: float
    max_total_exposure_pct: float
    daily_loss_limit_pct: float
    max_consecutive_errors: int


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    code: str
    message: str


def evaluate_order(
    profile: RiskProfile,
    *,
    order_notional: float,
    total_asset: float,
    position_market_value: float,
    total_market_value: float,
    daily_pnl_pct: float,
    consecutive_errors: int,
) -> RiskDecision:
    if profile.emergency_stop_enabled:
        return RiskDecision(False, "emergency_stop", "已触发紧急停止")
    if total_asset <= 0:
        return RiskDecision(False, "invalid_asset", "账户总资产无效")
    if daily_pnl_pct <= -abs(profile.daily_loss_limit_pct):
        return RiskDecision(False, "daily_loss_limit", "已触发日亏损熔断")
    if consecutive_errors >= profile.max_consecutive_errors:
        return RiskDecision(False, "consecutive_errors", "连续错误次数达到暂停阈值")

    max_order = min(profile.max_order_notional_abs, total_asset * profile.max_order_notional_pct)
    if order_notional > max_order:
        return RiskDecision(False, "max_order_notional", "单笔订单金额超过风控上限")
    if position_market_value + order_notional > total_asset * profile.max_position_pct:
        return RiskDecision(False, "max_position", "单只股票仓位超过风控上限")
    if total_market_value + order_notional > total_asset * profile.max_total_exposure_pct:
        return RiskDecision(False, "max_total_exposure", "总仓位超过风控上限")
    return RiskDecision(True, "allowed", "风控检查通过")
