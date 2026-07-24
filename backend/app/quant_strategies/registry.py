from __future__ import annotations

from .breakout_trend import BreakoutTrendStrategy
from .earnings_drift import EarningsDriftStrategy
from .low_vol_quality import LowVolQualityStrategy
from .multi_factor_core import MultiFactorCoreStrategy
from .protocol import QuantStrategyModule
from .regime_allocator import RegimeAllocatorStrategy
from .relative_strength_rotation import RelativeStrengthRotationStrategy
from .risk_parity_overlay import RiskParityOverlayStrategy
from .short_term_reversal_t1 import ShortTermReversalT1Strategy


STRATEGY_MODULES: dict[str, QuantStrategyModule] = {
    module.key: module
    for module in (
        MultiFactorCoreStrategy(),
        RelativeStrengthRotationStrategy(),
        BreakoutTrendStrategy(),
        ShortTermReversalT1Strategy(),
        LowVolQualityStrategy(),
        EarningsDriftStrategy(),
        RegimeAllocatorStrategy(),
        RiskParityOverlayStrategy(),
    )
}
