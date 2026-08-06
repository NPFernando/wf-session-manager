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
    model_validator,
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
        Tool.COPILOT: ToolProfile(command=("copilot",)),
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
    default_text_scale: Literal["compact", "comfortable", "readable"] = "comfortable"
    performance_profile: Literal["ssh-safe", "balanced", "rich"] = "balanced"

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
    idle_auto_wait_days: int = Field(default=0, ge=0, le=365)
    custom_checks: tuple[CustomHealthCheckConfig, ...] = ()

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


class ProjectDefault(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project: str
    tool: Tool | None = None
    tags: tuple[str, ...] = ()
    task_template: str = ""
    logging_enabled: bool | None = None


class CustomHealthCheckConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    command: tuple[str, ...]
    status_on_failure: Literal["info", "warn", "fail"] = "warn"
    ttl_seconds: float = Field(default=60.0, ge=5.0, le=86_400.0)
    corrective_action: str = ""

    @field_validator("command")
    @classmethod
    def safe_command(cls, command: tuple[str, ...]) -> tuple[str, ...]:
        if not command or not command[0].strip():
            raise ValueError("custom check command cannot be empty")
        return command


class SlaRuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    severity: Literal["info", "warn", "fail"] = "warn"
    detached_hours: float = Field(default=72.0, ge=1.0, le=8760.0)
    blocked_hours: float = Field(default=24.0, ge=1.0, le=8760.0)
    input_required_hours: float = Field(default=12.0, ge=1.0, le=8760.0)


class FederationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    hosts: tuple[str, ...] = ()
    ssh_command: tuple[str, ...] = ("ssh",)
    remote_ws_command: tuple[str, ...] = ("ws", "list", "--json")
    timeout_seconds: float = Field(default=8.0, ge=1.0, le=120.0)
    action_retry_attempts: int = Field(default=2, ge=1, le=5)
    action_retry_delay_seconds: float = Field(default=0.4, ge=0.0, le=5.0)
    anomaly_spread_threshold: int = Field(default=2, ge=1, le=200)
    anomaly_delta_threshold: int = Field(default=3, ge=1, le=500)


class ArchivePolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    completed_days: int = Field(default=14, ge=1, le=3650)
    include_logs: bool = True
    include_timeline: bool = True
    dry_run_default: bool = True


class ApprovalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    code: str = ""
    guarded_actions: tuple[str, ...] = (
        "delete",
        "stop-session",
        "bulk-stop-command",
        "remove-metadata",
    )
    token_signing_secret: str = ""
    token_ttl_seconds: int = Field(default=900, ge=30, le=86_400)
    dual_control_enabled: bool = False
    dual_control_actions: tuple[str, ...] = ()


class AutomationHookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event: str
    command: tuple[str, ...]
    timeout_seconds: float = Field(default=5.0, ge=1.0, le=60.0)
    api_version: Literal[1] = 1

    @field_validator("event")
    @classmethod
    def valid_event(cls, value: str) -> str:
        allowed = {
            "session.created",
            "session.note_updated",
            "session.organized",
            "session.renamed",
            "session.deleted",
            "session.command_stopped",
            "session.stopped",
            "session.restarted",
            "session.logging_updated",
            "session.logs_cleared",
            "session.metadata_removed",
        }
        if value not in allowed:
            raise ValueError(f"unsupported automation hook event: {value}")
        return value


class OperatorProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    default_project: str = ""
    require_approvals: bool = False
    approval_code: str = ""
    allowed_actions: tuple[str, ...] = ()


class PlaybookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    match_any: tuple[str, ...] = ()
    commands: tuple[tuple[str, ...], ...] = ()
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=300.0)


class SelfHealRuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    when_checks: tuple[str, ...] = ()
    min_status: Literal["warn", "fail"] = "warn"
    action: Literal["playbook", "archive", "recover-repair", "chain"] = "playbook"
    playbook: str = ""
    chain: str = ""
    session_scope: Literal["none", "related"] = "related"
    max_sessions: int = Field(default=5, ge=1, le=200)

    @model_validator(mode="after")
    def validate_action_requirements(self) -> SelfHealRuleConfig:
        if self.action == "playbook" and not self.playbook.strip():
            raise ValueError("self-heal rule with action=playbook requires playbook")
        if self.action == "chain" and not self.chain.strip():
            raise ValueError("self-heal rule with action=chain requires chain")
        return self


class SelfHealConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    rules: tuple[SelfHealRuleConfig, ...] = ()


class RemediationChainConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    when_checks: tuple[str, ...] = ()
    min_status: Literal["warn", "fail"] = "warn"
    steps: tuple[str, ...] = ()
    rollback_playbook: str = ""

    @field_validator("steps")
    @classmethod
    def validate_steps(cls, steps: tuple[str, ...]) -> tuple[str, ...]:
        if not steps:
            raise ValueError("remediation chain requires at least one step")
        normalized: list[str] = []
        for step in steps:
            value = str(step).strip()
            if not value:
                raise ValueError("remediation chain step cannot be empty")
            if value.startswith("playbook:") and value.split(":", 1)[1].strip():
                normalized.append(value)
                continue
            if value in {"archive", "recover-repair"}:
                normalized.append(value)
                continue
            raise ValueError(
                "invalid remediation chain step; expected playbook:<name>, "
                "archive, or recover-repair"
            )
        return tuple(normalized)


class NotificationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_api_base: str = "https://api.telegram.org"
    subprocess_timeout: float = Field(default=5.0, ge=1.0, le=30.0)

    @field_validator("telegram_api_base")
    @classmethod
    def https_only(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("telegram_api_base must use https:// (the bot token is in the URL)")
        return value


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
    federation: FederationConfig = Field(default_factory=FederationConfig)
    archive_policy: ArchivePolicyConfig = Field(default_factory=ArchivePolicyConfig)
    approvals: ApprovalConfig = Field(default_factory=ApprovalConfig)
    automation_hooks: tuple[AutomationHookConfig, ...] = ()
    operator_profiles: tuple[OperatorProfileConfig, ...] = ()
    playbooks: tuple[PlaybookConfig, ...] = ()
    self_heal: SelfHealConfig = Field(default_factory=SelfHealConfig)
    remediation_chains: tuple[RemediationChainConfig, ...] = ()
    sla_rules: tuple[SlaRuleConfig, ...] = ()
    project_defaults: tuple[ProjectDefault, ...] = ()
    legacy_state_dirs: tuple[Path, ...] = Field(
        default_factory=lambda: (
            Path.home() / ".local" / "state" / "wf" / "sessions",
            Path.home() / ".ws-session-notes",
        )
    )
    tools: dict[Tool, ToolProfile] = Field(default_factory=default_tools)

    @model_validator(mode="before")
    @classmethod
    def merge_tool_defaults(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        raw_tools = value.get("tools")
        if not isinstance(raw_tools, dict):
            return value
        merged_tools: dict[Tool, ToolProfile | dict[str, object]] = dict(default_tools())
        for key, profile in raw_tools.items():
            try:
                tool = key if isinstance(key, Tool) else Tool(str(key))
            except ValueError:
                continue
            merged_tools[tool] = profile
        merged = dict(value)
        merged["tools"] = merged_tools
        return merged

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
