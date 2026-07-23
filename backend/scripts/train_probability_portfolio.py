from __future__ import annotations

import argparse
import json
from datetime import date

from app.config import get_settings
from app.database import Base, SessionLocal, apply_runtime_migrations, engine
from app.probability_portfolio.config import PROBABILITY_PORTFOLIO_DEFAULTS
from app.probability_portfolio.runtime import seed_probability_portfolio_runtime
from app.probability_portfolio.training import train_and_store_probability_model
from app.services import seed_database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="训练并保存一夜持股概率组合模型，不启用自动计划"
    )
    parser.add_argument(
        "--through",
        required=True,
        help="只使用退出窗口已在该日期前完成的样本，格式 YYYY-MM-DD",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        through = date.fromisoformat(args.through)
    except ValueError as exc:
        raise SystemExit("--through 必须使用 YYYY-MM-DD 格式") from exc

    Base.metadata.create_all(bind=engine)
    apply_runtime_migrations()
    settings = get_settings()
    with SessionLocal() as db:
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        parameters = {
            **PROBABILITY_PORTFOLIO_DEFAULTS,
            **(config.parameters or {}),
        }
        artifact = train_and_store_probability_model(
            db,
            through=through,
            feature_version=str(parameters["feature_version"]),
            min_training_samples=int(parameters["min_training_samples"]),
            min_calibration_samples=int(parameters["min_calibration_samples"]),
            max_brier_score=float(parameters["max_brier_score"]),
        )
    print(
        json.dumps(
            {
                "model_version": artifact.model_version,
                "status": artifact.status,
                "trained_through": artifact.trained_through,
                "training_sample_count": artifact.training_sample_count,
                "calibration_sample_count": artifact.calibration_sample_count,
                "brier_score": artifact.brier_score,
                "reasons": (
                    artifact.error_message.split(", ")
                    if artifact.error_message
                    else []
                ),
                "schedules_enabled": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
