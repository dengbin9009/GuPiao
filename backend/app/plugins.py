from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


class PluginExecutionError(RuntimeError):
    pass


def run_plugin(plugin_path: Path, context: dict[str, Any], *, timeout_seconds: int = 5) -> list[dict[str, Any]]:
    plugin_path = plugin_path.resolve()
    if not plugin_path.is_file() or plugin_path.suffix != ".py":
        raise PluginExecutionError("插件文件无效")
    worker = Path(__file__).with_name("plugin_worker.py")
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(worker), str(plugin_path)],
            input=json.dumps(context, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=max(1, timeout_seconds),
            check=False,
            env={"PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired as exc:
        raise PluginExecutionError("插件执行超时") from exc
    if completed.returncode != 0:
        message = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "插件执行失败"
        raise PluginExecutionError(message)
    try:
        signals = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise PluginExecutionError("插件返回了无效 JSON") from exc
    if not isinstance(signals, list):
        raise PluginExecutionError("插件结果必须是信号列表")
    required = {"symbol", "side", "quantity", "reason"}
    for signal in signals:
        if not isinstance(signal, dict) or not required <= signal.keys():
            raise PluginExecutionError("插件信号缺少必填字段")
        if signal["side"] not in {"buy", "sell"} or type(signal["quantity"]) is not int or signal["quantity"] <= 0:
            raise PluginExecutionError("插件信号方向或数量无效")
    return signals
