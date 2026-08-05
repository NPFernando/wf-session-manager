"""Strict TOML configuration loading without executing shell content."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
)

from workspace_session_manager.errors import ConfigurationError
from workspace_session_manager.models import Tool
from workspace_session_manager.paths import AppPaths


class ToolProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    command: tuple[str, ...]
    enabled: bool = True

    @field_validator("command")
    @classmethod
    def safe_command(cls, command: tuple[str, ...]) -> tuple[str, ...]:
        if not command or not command[0].strip():
            raise ValueError("tool command cannot be empty")
        if any(any(character in argument for character in "\x00\r\n") for argument in command):
            raise ValueError("tool command contains control characters")
        return command


def default_tools() -> dict[Tool, ToolProfile]:
    shell = os.environ.get("SHELL", "/bin/bash")
    return {
        Tool.CLAUDE: ToolProfile(command=("claude",)),
        Tool.CODEX: ToolProfile(command=("codex",)),
        Tool.HERMES: ToolProfile(command=("hermes", "chat")),
        Tool.SHELL: ToolProfile(command=(shell, "-l")),
    }


class InterfaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    animations: Literal["off", "subtle", "full"] = "subtle"
    reduce_motion: bool = False
    environment_display: Literal["hidden", "label", "hostname"] = "hidden"
    environment_label: str = Field(default="", max_length=80)
    default_grouping: Literal["attention", "runtime", "agent", "project", "warning", "recent"] = (
        "attention"
    )
    default_density: Literal["compact", "comfortable"] = "comfortable"

    @field_validator("environment_label")
    @classmethod
    def safe_environment_label(cls, label: str) -> str:
        if any(character in label for character in "\x00\r\n"):
            raise ValueError("environment label contains control characters")
        return label


def default_health_scan_roots() -> tuple[Path, ...]:
    return (Path("/srv/projects"), Path.home() / "workspace" / "projects")


class HealthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = True
    disk_space_enabled: bool = True
    apt_updates_enabled: bool = True
    reboot_required_enabled: bool = True
    git_dirty_enabled: bool = True
    docker_enabled: bool = True
    disk_warn_percent: int = Field(default=10, ge=0, le=100)
    disk_fail_percent: int = Field(default=2, ge=0, le=100)
    disk_ttl_seconds: float = Field(default=30.0, ge=5.0, le=3600.0)
    apt_updates_ttl_seconds: float = Field(default=21_600.0, ge=60.0, le=86_400.0)
    reboot_required_ttl_seconds: float = Field(default=300.0, ge=5.0, le=3600.0)
    git_dirty_ttl_seconds: float = Field(default=60.0, ge=5.0, le=3600.0)
    docker_ttl_seconds: float = Field(default=30.0, ge=5.0, le=3600.0)
    git_scan_budget: int = Field(default=20, ge=1, le=200)
    subprocess_timeout: float = Field(default=5.0, ge=1.0, le=30.0)
    project_scan_roots: tuple[Path, ...] = Field(default_factory=default_health_scan_roots)
    zombie_sessions_enabled: bool = True
    zombie_sessions_ttl_seconds: float = Field(default=1800.0, ge=5.0, le=3600.0)
    zombie_stale_after_days: int = Field(default=14, ge=1, le=365)
    orphaned_logs_enabled: bool = True
    orphaned_logs_ttl_seconds: float = Field(default=1800.0, ge=5.0, le=3600.0)
    orphaned_logs_min_age_hours: int = Field(default=24, ge=1, le=8760)
    missing_cwd_enabled: bool = True
    missing_cwd_ttl_seconds: float = Field(default=1800.0, ge=5.0, le=3600.0)
    idle_sessions_enabled: bool = True
    idle_sessions_ttl_seconds: float = Field(default=1800.0, ge=5.0, le=3600.0)
    idle_after_days: int = Field(default=30, ge=1, le=365)

    @field_validator("project_scan_roots")
    @classmethod
    def expand_scan_roots(cls, values: tuple[Path, ...]) -> tuple[Path, ...]:
        return tuple(value.expanduser() for value in values)

    @field_validator("disk_fail_percent")
    @classmethod
    def fail_below_warn(cls, fail: int, info: ValidationInfo) -> int:
        warn = info.data.get("disk_warn_percent", 10)
        if fail > warn:
            raise ValueError("disk_fail_percent must not exceed disk_warn_percent")
        return fail


class NotificationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_api_base: str = "https://api.telegram.org"
    subprocess_timeout: float = Field(default=5.0, ge=1.0, le=30.0)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = Field(default=1, ge=1, le=1)
    refresh_interval: float = Field(default=3.0, ge=1.0, le=60.0)
    attention_scan_budget: int = Field(default=8, ge=1, le=64)
    preview_lines: int = Field(default=12, ge=8, le=500)
    preview_bytes: int = Field(default=32_768, ge=1024, le=1_048_576)
    log_lines: int = Field(default=500, ge=50, le=5000)
    log_bytes: int = Field(default=262_144, ge=4096, le=4_194_304)
    interface: InterfaceConfig = Field(default_factory=InterfaceConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    legacy_state_dirs: tuple[Path, ...] = Field(
        default_factory=lambda: (
            Path.home() / ".local" / "state" / "wf" / "sessions",
            Path.home() / ".ws-session-notes",
        )
    )
    tools: dict[Tool, ToolProfile] = Field(default_factory=default_tools)

    @field_validator("legacy_state_dirs")
    @classmethod
    def expand_legacy_paths(cls, values: tuple[Path, ...]) -> tuple[Path, ...]:
        return tuple(value.expanduser() for value in values)

    @field_validator("tools")
    @classmethod
    def all_tools_configured(cls, tools: dict[Tool, ToolProfile]) -> dict[Tool, ToolProfile]:
        missing = set(Tool) - set(tools)
        if missing:
            names = ", ".join(sorted(tool.value for tool in missing))
            raise ValueError(f"missing tool profiles: {names}")
        return tools


def load_config(paths: AppPaths, override: Path | None = None) -> AppConfig:
    """Load strict TOML, returning defaults when no config file exists."""
    env_path = os.environ.get("WS_DEV_CONFIG")
    config_path = override or (Path(env_path).expanduser() if env_path else paths.config_file)
    if not config_path.exists():
        return AppConfig()

    try:
        with config_path.open("rb") as stream:
            raw = tomllib.load(stream)
        return AppConfig.model_validate(raw)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as error:
        raise ConfigurationError(f"invalid configuration at {config_path}: {error}") from error
