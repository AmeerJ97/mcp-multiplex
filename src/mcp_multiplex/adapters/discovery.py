"""Read-only config path discovery for first-wave agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_multiplex.adapters.registry import SUPPORTED_AGENT_KINDS, AgentConfigPath

FIRST_WAVE_AGENT_ORDER = ("codex", "claude_code", "gemini", "cline", "opencode")


@dataclass(frozen=True)
class ExpectedConfigPath:
    """Expected config path candidate for an agent."""

    agent_kind: str
    relative_path: str
    format: str
    precedence: int
    is_project_shared: bool = False


@dataclass(frozen=True)
class DiscoveredConfigPath:
    """Existing config path discovered without parsing."""

    agent_kind: str
    path: str
    format: str
    precedence: int
    is_project_shared: bool = False

    def to_agent_config_path(self) -> AgentConfigPath:
        return AgentConfigPath(
            path=self.path,
            format=self.format,
            precedence=self.precedence,
            is_project_shared=self.is_project_shared,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_kind": self.agent_kind,
            "path": self.path,
            "format": self.format,
            "precedence": self.precedence,
            "is_project_shared": self.is_project_shared,
        }


@dataclass(frozen=True)
class DiscoveryNotice:
    """Non-error discovery notice."""

    agent_kind: str
    code: str
    path: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "agent_kind": self.agent_kind,
            "code": self.code,
            "path": self.path,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DiscoveryResult:
    """Config path discovery result."""

    schema_version: int = 1
    kind: str = "MCPMultiplexConfigDiscovery"
    config_paths: list[DiscoveredConfigPath] = field(default_factory=list)
    notices: list[DiscoveryNotice] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "config_paths": [path.to_dict() for path in self.config_paths],
            "notices": [notice.to_dict() for notice in self.notices],
        }


EXPECTED_CONFIG_PATHS = (
    ExpectedConfigPath("codex", ".codex/config.toml", "toml", 10),
    ExpectedConfigPath("claude_code", ".claude.json", "json", 10),
    ExpectedConfigPath("claude_code", ".claude/settings.json", "json", 20),
    ExpectedConfigPath("gemini", ".gemini/settings.json", "json", 10),
    ExpectedConfigPath("cline", ".cline/data/settings/cline_mcp_settings.json", "json", 10),
    ExpectedConfigPath(
        "cline",
        ".config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json",
        "json",
        20,
    ),
    ExpectedConfigPath(
        "cline",
        ".config/Cursor/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json",
        "json",
        30,
    ),
    ExpectedConfigPath("opencode", ".config/opencode/opencode.json", "json", 10),
    ExpectedConfigPath("opencode", ".config/opencode/opencode.jsonc", "json", 20),
)


def discover_config_paths(
    *,
    home: Path | None = None,
    agent_kinds: list[str] | None = None,
) -> DiscoveryResult:
    """Discover existing first-wave config files without parsing or mutation."""
    resolved_home = (home or Path.home()).expanduser()
    requested = tuple(agent_kinds or FIRST_WAVE_AGENT_ORDER)
    for agent_kind in requested:
        if agent_kind not in SUPPORTED_AGENT_KINDS:
            raise ValueError(f"unsupported agent_kind: {agent_kind}")

    found: list[DiscoveredConfigPath] = []
    notices: list[DiscoveryNotice] = []
    for expected in EXPECTED_CONFIG_PATHS:
        if expected.agent_kind not in requested:
            continue
        path = resolved_home / expected.relative_path
        if path.is_file():
            found.append(
                DiscoveredConfigPath(
                    agent_kind=expected.agent_kind,
                    path=str(path),
                    format=expected.format,
                    precedence=expected.precedence,
                    is_project_shared=expected.is_project_shared,
                )
            )
        else:
            notices.append(
                DiscoveryNotice(
                    agent_kind=expected.agent_kind,
                    code="expected_config_missing",
                    path=str(path),
                    detail="Expected config file was not present; discovery did not create it.",
                )
            )

    return DiscoveryResult(
        config_paths=sorted(found, key=lambda item: (item.agent_kind, item.precedence, item.path)),
        notices=sorted(notices, key=lambda item: (item.agent_kind, item.path)),
    )
