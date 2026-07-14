from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any


MAX_FIELD_CHARS = 30_000

ENRICHMENT_ENV_KEYS = {
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


def _environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in ENRICHMENT_ENV_KEYS and value
    }


def _unavailable(message: str) -> dict[str, Any]:
    return {
        "source": "yahoo",
        "status": "unavailable",
        "error": message[-1000:],
    }


def _bounded(value: Any) -> Any:
    if isinstance(value, str):
        return value[:MAX_FIELD_CHARS]
    if isinstance(value, dict):
        return {str(key): _bounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_bounded(item) for item in value]
    return value


def _run(request: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
    command = [sys.executable, "-m", "app.trading_agents.enrichment_subprocess"]
    try:
        with tempfile.TemporaryDirectory(prefix="gupiao-enrichment-") as child_cwd:
            completed = subprocess.run(
                command,
                input=json.dumps(request, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                env=_environment(),
                cwd=child_cwd,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return _unavailable(f"Yahoo 补充数据抓取超时（{timeout_seconds}秒）")
    except OSError as exc:
        return _unavailable(f"Yahoo 补充数据子进程无法启动: {exc}")
    if completed.returncode != 0:
        return _unavailable(completed.stderr or "Yahoo 补充数据子进程失败")
    try:
        result = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        return _unavailable("Yahoo 补充数据返回无效 JSON")
    return _bounded(result) if isinstance(result, dict) else _unavailable("Yahoo 补充数据结构无效")


def collect_enrichment(
    symbols: list[str],
    *,
    trading_date: str,
    concurrency: int = 2,
    timeout_seconds: int = 90,
) -> dict[str, Any]:
    ordered_symbols = sorted(set(symbols))
    symbol_results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(ordered_symbols) or 1))) as executor:
        futures = {
            executor.submit(
                _run,
                {"mode": "symbol", "symbol": symbol, "trading_date": trading_date},
                timeout_seconds=timeout_seconds,
            ): symbol
            for symbol in ordered_symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                symbol_results[symbol] = future.result()
            except Exception as exc:
                symbol_results[symbol] = _unavailable(
                    f"Yahoo 补充数据任务异常: {exc}"
                )
    global_result = _run(
        {"mode": "global", "trading_date": trading_date},
        timeout_seconds=timeout_seconds,
    )
    return {
        "source": "yahoo",
        "captured_at": datetime.now().astimezone().isoformat(),
        "symbols": {
            symbol: symbol_results.get(symbol, _unavailable("未返回补充数据"))
            for symbol in ordered_symbols
        },
        "global": global_result,
    }
