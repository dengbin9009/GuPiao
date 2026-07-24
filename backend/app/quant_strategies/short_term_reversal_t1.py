from __future__ import annotations

from datetime import date
from typing import Any, Sequence

from .algorithms import CandidateInput, TargetPortfolio, _short_term_reversal


class ShortTermReversalT1Strategy:
    key = "short_term_reversal_t1"

    def build_target(
        self,
        candidates: Sequence[CandidateInput],
        *,
        as_of: date,
        benchmark: CandidateInput | None,
        parameters: dict[str, Any],
    ) -> TargetPortfolio:
        return _short_term_reversal(
            list(candidates),
            benchmark,
            as_of,
            parameters=parameters,
        )
