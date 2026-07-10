from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
from pathlib import Path


def _limit_resources() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
        memory = 256 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    except (ImportError, OSError, ValueError):
        pass


def main() -> int:
    _limit_resources()
    if len(sys.argv) != 2:
        print("缺少插件路径", file=sys.stderr)
        return 2
    path = Path(sys.argv[1]).resolve()
    spec = importlib.util.spec_from_file_location("gupiao_user_plugin", path)
    if not spec or not spec.loader:
        print("无法加载插件", file=sys.stderr)
        return 2
    module = importlib.util.module_from_spec(spec)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            spec.loader.exec_module(module)
            metadata = getattr(module, "STRATEGY_METADATA", None)
            if not isinstance(metadata, dict) or "parameter_schema" not in metadata:
                raise ValueError("插件元数据无效")
            generator = getattr(module, "generate_signals", None)
            if not callable(generator):
                raise ValueError("插件缺少 generate_signals(context)")
            context = json.loads(sys.stdin.read() or "{}")
            result = generator(context)
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
