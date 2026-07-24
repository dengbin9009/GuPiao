from __future__ import annotations

from datetime import date
from typing import Any, Sequence

from .algorithms import CandidateInput, TargetPortfolio, _risk_parity


class RiskParityOverlayStrategy:
    key = "risk_parity_overlay"

    def build_target(
        self,
        candidates: Sequence[CandidateInput],
        *,
        as_of: date,
        benchmark: CandidateInput | None,
        parameters: dict[str, Any],
    ) -> TargetPortfolio:
        return _risk_parity(list(candidates), as_of, parameters=parameters)
