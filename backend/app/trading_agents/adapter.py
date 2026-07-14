from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .batches import AnalysisResult
from .config import data_root


RATINGS = {
    "buy": "Buy",
    "overweight": "Overweight",
    "hold": "Hold",
    "underweight": "Underweight",
    "sell": "Sell",
}

CHILD_ENV_KEYS = {
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "SYSTEMROOT",
}


def to_yahoo_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if normalized.endswith(".SH"):
        return f"{normalized[:-3]}.SS"
    return normalized


def normalize_rating(value: Any) -> str:
    rating = RATINGS.get(str(value).strip().lower())
    if not rating:
        raise ValueError("TradingAgents 输出不是有效的五级评级")
    return rating


def _child_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in CHILD_ENV_KEYS and value
    }


def _redact(message: str) -> str:
    sensitive_values = (
        os.getenv("OPENAI_API_KEY", "").strip(),
        os.getenv("OPENAI_BASE_URL", "").strip(),
        os.getenv("OPENAI_API_BASE", "").strip(),
    )
    for value in sensitive_values:
        if value:
            message = message.replace(value, "[REDACTED]")
    return message


class TradingAgentsAnalyzer:
    def __init__(
        self,
        *,
        python_executable: str | None = None,
        result_root: Path | None = None,
    ) -> None:
        self.python_executable = python_executable or sys.executable
        self.result_root = result_root or data_root() / "upstream"

    def _run_child(self, request: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
        command = [
            self.python_executable,
            "-m",
            "app.trading_agents.agent_subprocess",
        ]
        request = {**request, "result_root": str(self.result_root)}
        try:
            with tempfile.TemporaryDirectory(prefix="gupiao-tradingagents-") as child_cwd:
                completed = subprocess.run(
                    command,
                    input=json.dumps(request, ensure_ascii=False),
                    text=True,
                    capture_output=True,
                    timeout=timeout_seconds,
                    env=_child_environment(),
                    cwd=child_cwd,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"TradingAgents 分析超时（{timeout_seconds}秒）") from exc
        if completed.returncode != 0:
            detail = _redact((completed.stderr or "子进程无错误输出")[-2000:])
            raise RuntimeError(f"TradingAgents 子进程失败: {detail}")
        try:
            result = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("TradingAgents 子进程返回了无效 JSON") from exc
        if not isinstance(result, dict):
            raise RuntimeError("TradingAgents 子进程返回结构无效")
        return result

    def analyze(
        self,
        *,
        symbol: str,
        trading_date: str,
        snapshot: dict[str, Any],
        profile: str,
        quick_model: str,
        deep_model: str,
        timeout_seconds: int,
    ) -> AnalysisResult:
        result = self._run_child(
            {
                "mode": "candidate",
                "symbol": to_yahoo_symbol(symbol),
                "gupiao_symbol": symbol,
                "trading_date": trading_date,
                "snapshot": snapshot,
                "profile": profile,
                "quick_model": quick_model,
                "deep_model": deep_model,
            },
            timeout_seconds=timeout_seconds,
        )
        rating = normalize_rating(result.get("rating"))
        target = result.get("ai_target_weight")
        if target is not None and (
            isinstance(target, bool) or not isinstance(target, (int, float))
        ):
            raise ValueError("TradingAgents AI 目标仓位格式无效")
        return AnalysisResult(
            rating=rating,
            ai_target_weight=float(target) if target is not None else None,
            report=str(result.get("report", "")),
            reasoning=str(result.get("reasoning", "")),
            llm_calls=int(result.get("llm_calls", 0)),
            tokens_in=int(result.get("tokens_in", 0)),
            tokens_out=int(result.get("tokens_out", 0)),
            report_uri=result.get("report_uri"),
        )

    def decide_portfolio(
        self,
        *,
        analyses: list[dict[str, Any]],
        trading_date: str,
        profile: str,
        quick_model: str,
        deep_model: str,
        timeout_seconds: int = 240,
    ) -> dict[str, Any]:
        result = self._run_child(
            {
                "mode": "portfolio",
                "analyses": analyses,
                "trading_date": trading_date,
                "profile": profile,
                "quick_model": quick_model,
                "deep_model": deep_model,
            },
            timeout_seconds=timeout_seconds,
        )
        expected = {str(item["symbol"]) for item in analyses}
        rankings = result.get("rankings")
        if not isinstance(rankings, list) or len(rankings) != len(expected):
            raise ValueError("跨股票组合决策未返回完整排名")
        returned = {str(item.get("symbol")) for item in rankings if isinstance(item, dict)}
        ranks = {int(item.get("rank", 0)) for item in rankings if isinstance(item, dict)}
        if returned != expected or ranks != set(range(1, len(expected) + 1)):
            raise ValueError("跨股票组合决策排名无法校验")
        return {
            "rankings": rankings,
            "rationale": str(result.get("rationale", "")),
            "llm_calls": int(result.get("llm_calls", 0)),
            "tokens_in": int(result.get("tokens_in", 0)),
            "tokens_out": int(result.get("tokens_out", 0)),
        }
