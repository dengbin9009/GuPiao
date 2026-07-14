from __future__ import annotations

import importlib.util
from importlib import metadata
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ..config import Settings


TRADING_AGENTS_VERSION = "0.3.1"
TRADING_AGENTS_COMMIT = "01477f9afb7a47b849ed4c9259d3a9a4738d9fda"


def data_root() -> Path:
    configured = os.getenv("TRADING_AGENTS_DATA_ROOT", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[3] / path
        return path.resolve()
    return Path(__file__).resolve().parents[3] / "data" / "trading-agents"

ANALYSIS_PROFILES: dict[str, dict[str, Any]] = {
    "a_share_balanced": {
        "label": "A股优化三分析师",
        "analysts": ["market", "fundamentals", "news"],
        "debate_rounds": 1,
        "risk_rounds": 1,
    },
    "full": {
        "label": "完整四分析师",
        "analysts": ["market", "social", "news", "fundamentals"],
        "debate_rounds": 1,
        "risk_rounds": 1,
    },
    "deep": {
        "label": "深度完整模式",
        "analysts": ["market", "social", "news", "fundamentals"],
        "debate_rounds": 3,
        "risk_rounds": 3,
    },
}

POSITION_MAPPINGS = {
    "fixed_rating": "固定评级权重",
    "ai_target": "AI目标仓位",
    "equal_weight": "等权买入",
}

TRADING_AGENTS_DEFAULTS: dict[str, Any] = {
    "analysis_profile": "a_share_balanced",
    "position_mapping": "fixed_rating",
    "quick_model": "gpt-5.4-mini",
    "deep_model": "gpt-5.2",
    "prefilter_size": 100,
    "top_n": 10,
    "snapshot_quote_max_age_seconds": 600,
    "daily_max_age_days": 7,
    "event_max_age_seconds": 1800,
    "enrichment_enabled": True,
    "enrichment_timeout_seconds": 45,
    "max_positions": 5,
    "max_position_pct": 0.20,
    "max_total_exposure_pct": 0.60,
    "max_llm_calls": 100,
    "max_input_tokens": 1_000_000,
    "max_output_tokens": 150_000,
    "worker_concurrency": 2,
    "candidate_timeout_seconds": 480,
    "analysis_deadline": "14:42",
    "rebalance_time": "14:45",
    "latest_rebalance_time": "14:50",
    "dry_run": True,
}


def configuration_fingerprint(
    parameters: dict[str, Any] | None,
    *,
    simulation_account_id: int | None,
) -> str:
    normalized = {**TRADING_AGENTS_DEFAULTS, **(parameters or {})}
    normalized.pop("dry_run", None)
    payload = json.dumps(
        {
            "parameters": normalized,
            "simulation_account_id": simulation_account_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def readiness(settings: Settings) -> dict[str, Any]:
    reasons: list[str] = []
    openai_configured = bool(os.getenv("OPENAI_API_KEY", "").strip())
    dependency_installed = importlib.util.find_spec("tradingagents") is not None
    dependency_version = None
    dependency_commit = None
    if dependency_installed:
        try:
            dependency_version = metadata.version("tradingagents")
            direct_url = metadata.distribution("tradingagents").read_text(
                "direct_url.json"
            )
            if direct_url:
                dependency_commit = (
                    json.loads(direct_url).get("vcs_info", {}).get("commit_id")
                )
        except metadata.PackageNotFoundError:
            dependency_installed = False
        except (json.JSONDecodeError, AttributeError, TypeError):
            dependency_commit = None
    if not openai_configured:
        reasons.append("OPENAI_API_KEY")
    if not dependency_installed:
        reasons.append("tradingagents_dependency")
    elif dependency_version != TRADING_AGENTS_VERSION:
        reasons.append("tradingagents_version")
    if dependency_installed and dependency_commit != TRADING_AGENTS_COMMIT:
        reasons.append("tradingagents_commit")
    if settings.live_enabled or settings.broker_adapter != "simulation":
        reasons.append("simulation_only_runtime_required")
    return {
        "ready": not reasons,
        "openai_configured": openai_configured,
        "dependency_installed": dependency_installed,
        "dependency_version": dependency_version,
        "dependency_version_valid": dependency_version == TRADING_AGENTS_VERSION,
        "dependency_commit": dependency_commit,
        "dependency_commit_valid": dependency_commit == TRADING_AGENTS_COMMIT,
        "simulation_only": not settings.live_enabled
        and settings.broker_adapter == "simulation",
        "reasons": reasons,
    }


def validate_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(parameters) - set(TRADING_AGENTS_DEFAULTS))
    if unknown:
        raise ValueError(f"未知 TradingAgents 参数: {', '.join(unknown)}")
    result = {**TRADING_AGENTS_DEFAULTS, **parameters}
    if result["analysis_profile"] not in ANALYSIS_PROFILES:
        raise ValueError("分析档位无效")
    if result["position_mapping"] not in POSITION_MAPPINGS:
        raise ValueError("仓位映射模式无效")
    integer_positive = (
        "prefilter_size",
        "top_n",
        "max_positions",
        "max_llm_calls",
        "max_input_tokens",
        "max_output_tokens",
        "worker_concurrency",
        "candidate_timeout_seconds",
        "snapshot_quote_max_age_seconds",
        "daily_max_age_days",
        "event_max_age_seconds",
        "enrichment_timeout_seconds",
    )
    for key in integer_positive:
        if isinstance(result[key], bool) or not isinstance(result[key], int) or result[key] < 1:
            raise ValueError(f"参数 {key} 必须是正整数")
    if result["top_n"] > result["prefilter_size"]:
        raise ValueError("Top N 不能超过预筛数量")
    if result["max_positions"] > result["top_n"]:
        raise ValueError("最大持仓数不能超过 Top N")
    for key in ("max_position_pct", "max_total_exposure_pct"):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
            raise ValueError(f"参数 {key} 必须在 0 到 1 之间")
    if result["max_position_pct"] > 0.20:
        raise ValueError("单股仓位不能超过20%")
    if result["max_total_exposure_pct"] > 0.60:
        raise ValueError("总仓位不能超过60%")
    for key in ("analysis_deadline", "rebalance_time", "latest_rebalance_time"):
        try:
            __import__("datetime").time.fromisoformat(str(result[key]))
        except ValueError as exc:
            raise ValueError(f"参数 {key} 时间格式无效") from exc
    if type(result["dry_run"]) is not bool:
        raise ValueError("参数 dry_run 必须是布尔值")
    if type(result["enrichment_enabled"]) is not bool:
        raise ValueError("参数 enrichment_enabled 必须是布尔值")
    return result
