"""Configuration and environment layout loading."""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NotRequired, TypedDict

APP_DIR_NAME = "mcp-multiplex"
DEFAULT_POLICY_FILENAME = "policy.toml"


class PolicyConfig(TypedDict):
    """Initial declarative policy surface."""

    schema_version: int
    profiles: dict[str, Any]
    packs: dict[str, Any]
    workspace_policy: dict[str, Any]


class ConfigInspectPayload(TypedDict):
    """CLI payload for environment and policy inspection."""

    schema_version: int
    kind: str
    paths: dict[str, str]
    policy: PolicyConfig
    policy_source: str | None
    policy_exists: bool
    warnings: list[str]
    errors: NotRequired[list[dict[str, str]]]


@dataclass(frozen=True)
class EnvironmentLayout:
    """Resolved MCP Multiplex environment paths."""

    config_dir: Path
    state_dir: Path
    cache_dir: Path
    policy_path: Path


class ConfigLoadError(Exception):
    """Actionable config loading failure."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"Failed to load policy config at {path}: {message}")


def default_policy_config() -> PolicyConfig:
    """Return the non-mutating default policy."""
    return {
        "schema_version": 1,
        "profiles": {},
        "packs": {},
        "workspace_policy": {},
    }


def _home_path(home: Path | None) -> Path:
    if home is not None:
        return home.expanduser()
    return Path.home()


def _env_path(env: Mapping[str, str], key: str) -> Path | None:
    value = env.get(key)
    if not value:
        return None
    return Path(value).expanduser()


def resolve_environment_layout(
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> EnvironmentLayout:
    """Resolve config, state, and cache directories without creating them."""
    source_env = os.environ if env is None else env
    resolved_home = _home_path(home)

    config_dir = _env_path(source_env, "MCP_MULTIPLEX_CONFIG_DIR")
    if config_dir is None:
        xdg_config_home = _env_path(source_env, "XDG_CONFIG_HOME")
        config_dir = (xdg_config_home or (resolved_home / ".config")) / APP_DIR_NAME

    state_dir = _env_path(source_env, "MCP_MULTIPLEX_STATE_DIR")
    if state_dir is None:
        xdg_state_home = _env_path(source_env, "XDG_STATE_HOME")
        state_dir = (xdg_state_home or (resolved_home / ".local" / "state")) / APP_DIR_NAME

    cache_dir = _env_path(source_env, "MCP_MULTIPLEX_CACHE_DIR")
    if cache_dir is None:
        xdg_cache_home = _env_path(source_env, "XDG_CACHE_HOME")
        cache_dir = (xdg_cache_home or (resolved_home / ".cache")) / APP_DIR_NAME

    return EnvironmentLayout(
        config_dir=config_dir,
        state_dir=state_dir,
        cache_dir=cache_dir,
        policy_path=config_dir / DEFAULT_POLICY_FILENAME,
    )


def load_policy_config(path: Path) -> tuple[PolicyConfig, str | None]:
    """Load policy TOML or return the default policy when absent."""
    if not path.exists():
        return default_policy_config(), None

    try:
        with path.open("rb") as file:
            raw = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise ConfigLoadError(path, str(error)) from error
    except OSError as error:
        raise ConfigLoadError(path, str(error)) from error

    return validate_policy_config(path, raw), str(path)


def validate_policy_config(path: Path, raw: Mapping[str, Any]) -> PolicyConfig:
    """Validate the initial declarative policy structure."""
    schema_version = raw.get("schema_version", 1)
    if schema_version != 1:
        raise ConfigLoadError(path, "schema_version must be 1")

    profiles = _optional_table(path, raw, "profiles")
    packs = _optional_table(path, raw, "packs")
    workspace_policy = _optional_table(path, raw, "workspace_policy")
    return {
        "schema_version": 1,
        "profiles": profiles,
        "packs": packs,
        "workspace_policy": workspace_policy,
    }


def _optional_table(path: Path, raw: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigLoadError(path, f"{key} must be a TOML table")
    return value


def inspect_config(
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> ConfigInspectPayload:
    """Return environment and policy details without mutating disk."""
    layout = resolve_environment_layout(home=home, env=env)
    policy, source = load_policy_config(layout.policy_path)
    return {
        "schema_version": 1,
        "kind": "MCPMultiplexConfigInspect",
        "paths": {
            "config_dir": str(layout.config_dir),
            "state_dir": str(layout.state_dir),
            "cache_dir": str(layout.cache_dir),
            "policy_path": str(layout.policy_path),
        },
        "policy": policy,
        "policy_source": source,
        "policy_exists": source is not None,
        "warnings": [],
    }


def inspect_config_json(
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return deterministic JSON for config inspection."""
    return json.dumps(inspect_config(home=home, env=env), sort_keys=True)
