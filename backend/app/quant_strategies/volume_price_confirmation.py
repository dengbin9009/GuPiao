from __future__ import annotations

from datetime import date
from typing import Any, Sequence

from .algorithms import (
    CandidateInput,
    TargetPortfolio,
    _volume_price_confirmation,
)


class VolumePriceConfirmationStrategy:
    key = "volume_price_confirmation"

    def build_target(
        self,
        candidates: Sequence[CandidateInput],
        *,
        as_of: date,
        benchmark: CandidateInput | None,
        parameters: dict[str, Any],
    ) -> TargetPortfolio:
        return _volume_price_confirmation(
            list(candidates),
            as_of,
            parameters=parameters,
        )
