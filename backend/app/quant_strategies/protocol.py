from __future__ import annotations

from datetime import date
from typing import Any, Protocol, Sequence

from .algorithms import CandidateInput, TargetPortfolio


class QuantStrategyModule(Protocol):
    key: str

    def build_target(
        self,
        candidates: Sequence[CandidateInput],
        *,
        as_of: date,
        benchmark: CandidateInput | None,
        parameters: dict[str, Any],
    ) -> TargetPortfolio: ...
