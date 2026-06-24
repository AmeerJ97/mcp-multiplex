import tomllib
from collections.abc import Iterable
from pathlib import Path

import pytest

import mcp_multiplex

REPO_ROOT = Path(__file__).resolve().parents[1]


def required_paths() -> Iterable[Path]:
    yield from [
        REPO_ROOT / "src/mcp_multiplex/daemon",
        REPO_ROOT / "src/mcp_multiplex/cli",
        REPO_ROOT / "src/mcp_multiplex/control_mcp",
        REPO_ROOT / "src/mcp_multiplex/adapters",
        REPO_ROOT / "src/mcp_multiplex/catalog",
        REPO_ROOT / "src/mcp_multiplex/runtime",
        REPO_ROOT / "src/mcp_multiplex/credentials",
        REPO_ROOT / "src/mcp_multiplex/storage",
        REPO_ROOT / "src/mcp_multiplex/storage/migrations",
        REPO_ROOT / "src/mcp_multiplex/approvals",
        REPO_ROOT / "src/mcp_multiplex/observability",
        REPO_ROOT / "src/mcp_multiplex/tui",
        REPO_ROOT / "tests/fixtures/agents",
        REPO_ROOT / "tests/fixtures/catalog",
        REPO_ROOT / "tests/fixtures/runtime",
        REPO_ROOT / "tests/acceptance",
    ]


def test_package_imports() -> None:
    assert mcp_multiplex.__version__ == "0.1.0"


def test_cli_help_entrypoint_returns_success(capsys: pytest.CaptureFixture[str]) -> None:
    from mcp_multiplex.cli import main as cli_main

    assert cli_main([]) == 0
    assert "query daemon health" in capsys.readouterr().out


def test_mxp_console_script_aliases_are_declared() -> None:
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)

    scripts = pyproject["project"]["scripts"]
    assert scripts["mxp"] == "mcp_multiplex.cli:main"
    assert scripts["mcp-multiplex"] == "mcp_multiplex.cli:main"


def test_required_scaffold_paths_exist() -> None:
    missing = [str(path.relative_to(REPO_ROOT)) for path in required_paths() if not path.exists()]
    assert missing == []
