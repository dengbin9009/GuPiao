from __future__ import annotations

import importlib
import os
from pathlib import Path


def main() -> None:
    project_root = Path(__file__).resolve().parents[2]
    env_path = project_root / ".env"
    original = env_path.read_text(encoding="utf-8") if env_path.exists() else None
    marker = "MARKET_DATA_STALE_AFTER_SECONDS=43210\n"
    try:
        if original is None:
            env_path.write_text(marker, encoding="utf-8")
        elif "MARKET_DATA_STALE_AFTER_SECONDS=" in original:
            env_path.write_text(
                "\n".join(
                    marker.rstrip("\n") if line.startswith("MARKET_DATA_STALE_AFTER_SECONDS=") else line
                    for line in original.splitlines()
                ) + "\n",
                encoding="utf-8",
            )
        else:
            env_path.write_text(original + ("\n" if not original.endswith("\n") else "") + marker, encoding="utf-8")

        os.environ.pop("MARKET_DATA_STALE_AFTER_SECONDS", None)
        import app.config as config
        importlib.reload(config)
        settings = config.get_settings()
        assert settings.market_stale_seconds == 43210, settings.market_stale_seconds
        print("env_loading_ok")
    finally:
        if original is None:
            env_path.unlink(missing_ok=True)
        else:
            env_path.write_text(original, encoding="utf-8")


if __name__ == "__main__":
    main()
