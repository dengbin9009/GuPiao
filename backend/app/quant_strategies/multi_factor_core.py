from __future__ import annotations

from datetime import date
from typing import Any, Sequence

from .algorithms import CandidateInput, TargetPortfolio, _multi_factor


class MultiFactorCoreStrategy:
    key = "multi_factor_core"

    def build_target(
        self,
        candidates: Sequence[CandidateInput],
        *,
        as_of: date,
        benchmark: CandidateInput | None,
        parameters: dict[str, Any],
    ) -> TargetPortfolio:
        return _multi_factor(list(candidates), as_of, parameters=parameters)
