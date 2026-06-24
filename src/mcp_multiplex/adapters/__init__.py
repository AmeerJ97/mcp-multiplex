"""Supported agent config adapter package."""

from mcp_multiplex.adapters.claude_code import (
    CLAUDE_CODE_AGENT_KIND,
    CLAUDE_CODE_RAW_SHAPE,
    ClaudeCodeAdapterError,
    ParsedClaudeCodeConfig,
    parse_claude_code_config,
)
from mcp_multiplex.adapters.cline import (
    CLINE_AGENT_KIND,
    CLINE_RAW_SHAPE,
    ClineAdapterError,
    ParsedClineConfig,
    parse_cline_config,
)
from mcp_multiplex.adapters.codex import (
    CODEX_AGENT_KIND,
    CODEX_RAW_SHAPE,
    CodexAdapterError,
    ParsedCodexConfig,
    parse_codex_config,
)
from mcp_multiplex.adapters.discovery import (
    DiscoveredConfigPath,
    DiscoveryNotice,
    DiscoveryResult,
    ExpectedConfigPath,
    discover_config_paths,
)
from mcp_multiplex.adapters.gemini import (
    GEMINI_AGENT_KIND,
    GEMINI_RAW_SHAPE,
    GeminiAdapterError,
    ParsedGeminiConfig,
    parse_gemini_config,
)
from mcp_multiplex.adapters.opencode import (
    OPENCODE_AGENT_KIND,
    OPENCODE_RAW_SHAPE,
    OpenCodeAdapterError,
    ParsedOpenCodeConfig,
    parse_opencode_config,
)
from mcp_multiplex.adapters.registry import (
    CERTIFICATION_LEVELS,
    SUPPORTED_AGENT_KINDS,
    AgentConfigPath,
    AgentRegistration,
    AgentRegistry,
    AgentRegistryError,
)

__all__ = [
    "CERTIFICATION_LEVELS",
    "CLAUDE_CODE_AGENT_KIND",
    "CLAUDE_CODE_RAW_SHAPE",
    "CLINE_AGENT_KIND",
    "CLINE_RAW_SHAPE",
    "CODEX_AGENT_KIND",
    "CODEX_RAW_SHAPE",
    "GEMINI_AGENT_KIND",
    "GEMINI_RAW_SHAPE",
    "OPENCODE_AGENT_KIND",
    "OPENCODE_RAW_SHAPE",
    "ClaudeCodeAdapterError",
    "ClineAdapterError",
    "CodexAdapterError",
    "DiscoveryNotice",
    "DiscoveryResult",
    "DiscoveredConfigPath",
    "ExpectedConfigPath",
    "OpenCodeAdapterError",
    "SUPPORTED_AGENT_KINDS",
    "AgentConfigPath",
    "AgentRegistration",
    "AgentRegistry",
    "AgentRegistryError",
    "ParsedClaudeCodeConfig",
    "ParsedClineConfig",
    "ParsedCodexConfig",
    "ParsedGeminiConfig",
    "ParsedOpenCodeConfig",
    "parse_claude_code_config",
    "parse_cline_config",
    "discover_config_paths",
    "GeminiAdapterError",
    "parse_gemini_config",
    "parse_opencode_config",
    "parse_codex_config",
]
