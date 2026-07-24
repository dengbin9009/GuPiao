from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


DEFAULT_ETF_UNIVERSE = (
    "510300.SH",
    "510500.SH",
    "159915.SZ",
    "510880.SH",
    "511010.SH",
    "518880.SH",
)


@dataclass(frozen=True)
class QuantStrategySpec:
    key: str
    name: str
    signal_time: str
    execution_time: str
    rebalance_frequency: str
    asset_type: str
    max_positions: int
    max_total_exposure_pct: float
    max_position_pct: float
    defaults: dict[str, Any]
    required_datasets: tuple[str, ...]
    simulation_only: bool = True
    version: str = "1.0.0"


def _defaults(**values: Any) -> dict[str, Any]:
    return {
        "timezone": "Asia/Shanghai",
        "prefilter_size": 800,
        "min_listing_days": 120,
        "min_average_turnover": 100_000_000.0,
        "data_version": "1",
        "dry_run": True,
        **values,
    }


QUANT_STRATEGY_SPECS: dict[str, QuantStrategySpec] = {
    "multi_factor_core": QuantStrategySpec(
        "multi_factor_core", "多因子核心组合", "16:30", "09:35", "monthly",
        "STOCK", 10, 0.80, 0.15,
        _defaults(min_position_pct=0.02, value_weight=0.25, quality_weight=0.25,
                  momentum_weight=0.30, low_vol_weight=0.20),
        ("daily", "adjustment", "daily_metric", "financial", "events"),
    ),
    "relative_strength_rotation": QuantStrategySpec(
        "relative_strength_rotation", "相对强弱轮动", "16:31", "09:36", "weekly",
        "STOCK", 5, 0.70, 0.20,
        _defaults(momentum_12_1_weight=0.60, momentum_6_1_weight=0.40),
        ("daily", "adjustment", "events"),
    ),
    "breakout_trend": QuantStrategySpec(
        "breakout_trend", "突破趋势", "16:32", "09:37", "daily",
        "STOCK", 8, 0.60, 0.15,
        _defaults(breakout_days=55, exit_days=20, atr_multiple=3.0,
                  volume_confirmation=1.5),
        ("daily", "adjustment", "events"),
    ),
    "short_term_reversal_t1": QuantStrategySpec(
        "short_term_reversal_t1", "短期反转 T+1", "16:33", "09:38", "daily",
        "STOCK", 5, 0.40, 0.10,
        _defaults(one_day_residual=-0.02, five_day_residual=-0.04,
                  benchmark_symbol="000300.SH", holding_days=1),
        ("daily", "adjustment", "events"),
    ),
    "low_vol_quality": QuantStrategySpec(
        "low_vol_quality", "低波质量", "16:34", "09:39", "monthly",
        "STOCK", 10, 0.80, 0.15,
        _defaults(quality_weight=0.60, low_vol_weight=0.40),
        ("daily", "adjustment", "financial", "events"),
    ),
    "earnings_drift": QuantStrategySpec(
        "earnings_drift", "业绩公告漂移", "16:35", "09:40", "event",
        "STOCK", 10, 0.70, 0.10,
        _defaults(min_sue=1.0, holding_days=20),
        ("daily", "adjustment", "financial", "events"),
    ),
    "regime_allocator": QuantStrategySpec(
        "regime_allocator", "市场状态配置", "16:36", "09:41", "weekly",
        "ETF", 6, 0.80, 0.50,
        _defaults(etf_universe=list(DEFAULT_ETF_UNIVERSE), benchmark_symbol="510300.SH"),
        ("daily", "adjustment", "realtime"),
    ),
    "risk_parity_overlay": QuantStrategySpec(
        "risk_parity_overlay", "风险平价组合", "16:37", "09:42", "monthly",
        "ETF", 6, 0.80, 0.35,
        _defaults(etf_universe=list(DEFAULT_ETF_UNIVERSE), lookback_days=120,
                  target_volatility=0.10, min_weight=0.01),
        ("daily", "adjustment", "realtime"),
    ),
}


def validate_quant_parameters(
    strategy_key: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    spec = QUANT_STRATEGY_SPECS.get(strategy_key)
    if spec is None:
        raise ValueError("未知独立量化策略")
    unknown = sorted(set(parameters) - set(spec.defaults))
    if unknown:
        raise ValueError(f"未知策略参数: {', '.join(unknown)}")
    result: dict[str, Any] = {}
    for name, default in spec.defaults.items():
        value = parameters.get(name, default)
        if isinstance(default, float):
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
            value = float(value) if valid else value
        else:
            valid = type(value) is type(default)
        if not valid:
            raise ValueError(f"参数 {name} 类型无效")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(float(value)):
                raise ValueError(f"参数 {name} 必须是有限数值")
        result[name] = value
    if not 1 <= int(result["prefilter_size"]) <= 800:
        raise ValueError("预筛数量必须在1到800之间")
    if int(result["min_listing_days"]) < 120:
        raise ValueError("最短上市天数不能低于120日")
    if float(result["min_average_turnover"]) < 100_000_000:
        raise ValueError("最低20日平均成交额不能低于1亿元")
    if result.get("etf_universe") is not None:
        universe = result["etf_universe"]
        if len(universe) != 6 or len(set(universe)) != len(universe):
            raise ValueError("ETF池必须按角色包含6个不重复标的")
        if any(
            not isinstance(symbol, str)
            or not symbol.endswith((".SH", ".SZ"))
            for symbol in universe
        ):
            raise ValueError("ETF池证券代码格式无效")
    factor_weight_names = [
        name
        for name in result
        if name.endswith("_weight") and name != "min_weight"
    ]
    if factor_weight_names:
        if any(not 0 <= float(result[name]) <= 1 for name in factor_weight_names):
            raise ValueError("策略因子权重必须在0到1之间")
        if abs(
            sum(float(result[name]) for name in factor_weight_names) - 1
        ) > 1e-9:
            raise ValueError("策略因子权重之和必须等于1")
    if "min_position_pct" in result and not 0.02 <= float(result["min_position_pct"]) <= spec.max_position_pct:
        raise ValueError("最小单股仓位必须在策略安全范围内")
    if "target_volatility" in result and not 0 < float(result["target_volatility"]) <= 0.10:
        raise ValueError("目标波动率必须大于0且不超过10%")
    if "lookback_days" in result and int(result["lookback_days"]) < 120:
        raise ValueError("风险平价观察期不能少于120日")
    if "min_weight" in result and not 0 < float(result["min_weight"]) <= spec.max_position_pct:
        raise ValueError("最小ETF仓位必须大于0且不超过单标的上限")
    for name in ("breakout_days", "exit_days", "holding_days"):
        if name in result and int(result[name]) < 1:
            raise ValueError(f"参数 {name} 必须至少为1个交易日")
    for name in ("atr_multiple", "volume_confirmation", "min_sue"):
        if name in result and float(result[name]) <= 0:
            raise ValueError(f"参数 {name} 必须大于0")
    return result
