from __future__ import annotations

from datetime import date
from typing import Any, Sequence

from .algorithms import CandidateInput, TargetPortfolio, _breakout


class BreakoutTrendStrategy:
    key = "breakout_trend"

    def build_target(
        self,
        candidates: Sequence[CandidateInput],
        *,
        as_of: date,
        benchmark: CandidateInput | None,
        parameters: dict[str, Any],
    ) -> TargetPortfolio:
        return _breakout(list(candidates), as_of, parameters=parameters)
