from __future__ import annotations

from datetime import date
from typing import Any, Sequence

from .algorithms import CandidateInput, TargetPortfolio, _regime_allocator


class RegimeAllocatorStrategy:
    key = "regime_allocator"

    def build_target(
        self,
        candidates: Sequence[CandidateInput],
        *,
        as_of: date,
        benchmark: CandidateInput | None,
        parameters: dict[str, Any],
    ) -> TargetPortfolio:
        return _regime_allocator(
            list(candidates),
            benchmark,
            as_of,
            parameters=parameters,
        )
