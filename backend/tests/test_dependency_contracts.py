from pathlib import Path
import tomllib

from packaging.requirements import Requirement
from packaging.version import Version


def test_dev_httpx_range_supports_tradingagents_dependency() -> None:
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    dev_dependencies = project["project"]["optional-dependencies"]["dev"]
    httpx = next(
        Requirement(dependency)
        for dependency in dev_dependencies
        if Requirement(dependency).name == "httpx"
    )

    assert Version("0.28.1") in httpx.specifier
    assert project["tool"]["uv"]["override-dependencies"] == ["httpx==0.28.1"]
