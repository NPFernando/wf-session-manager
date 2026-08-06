from pathlib import Path

import pytest

from workspace_session_manager.config import (
    AppConfig,
    AutomationHookConfig,
    HealthConfig,
    NotificationConfig,
    OperatorProfileConfig,
    PlaybookConfig,
    load_config,
)
from workspace_session_manager.errors import ConfigurationError
from workspace_session_manager.models import Tool
from workspace_session_manager.paths import AppPaths


def test_defaults_use_isolated_namespace_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WS_DEV_ROOT", str(tmp_path))
    paths = AppPaths.discover()
    config = load_config(paths)
    assert paths.state_dir == tmp_path / "state"
    assert config.schema_version == 1
    assert not paths.state_dir.exists()


def test_config_is_strict_toml(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    paths.config_dir.mkdir()
    paths.config_file.write_text("unknown = true\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="unknown"):
        load_config(paths)


def test_tools_config_backfills_missing_profiles_for_forward_compatibility() -> None:
    config = AppConfig.model_validate(
        {
            "tools": {
                "claude": {"command": ["claude"], "enabled": True},
                "codex": {"command": ["codex"], "enabled": True},
                "hermes": {"command": ["hermes", "chat"], "enabled": True},
                "shell": {"command": ["/bin/bash", "-l"], "enabled": True},
            }
        }
    )
    assert Tool.COPILOT in config.tools
    assert config.tools[Tool.COPILOT].command == ("copilot",)


def test_tilde_paths_expand() -> None:
    config = AppConfig(legacy_state_dirs=(Path("~/.legacy-wf"),))
    assert config.legacy_state_dirs[0].is_absolute()


def test_motion_configuration_is_strict() -> None:
    config = AppConfig.model_validate({"interface": {"animations": "full", "reduce_motion": True}})
    assert config.interface.animations == "full"
    assert config.interface.reduce_motion
    with pytest.raises(ValueError):
        AppConfig.model_validate({"interface": {"animations": "constant"}})


def test_interface_configuration_has_strict_display_and_view_defaults() -> None:
    interface = AppConfig().interface
    assert interface.environment_display == "hidden"
    assert interface.environment_label == ""
    assert interface.default_grouping == "attention"
    assert interface.default_density == "comfortable"
    assert interface.default_text_scale == "comfortable"
    configured = AppConfig.model_validate(
        {
            "interface": {
                "environment_display": "label",
                "environment_label": "Staging",
                "default_grouping": "project",
                "default_density": "compact",
                "default_text_scale": "readable",
            }
        }
    ).interface
    assert configured.environment_label == "Staging"
    assert configured.default_text_scale == "readable"
    with pytest.raises(ValueError):
        AppConfig.model_validate({"interface": {"environment_display": "always"}})
    with pytest.raises(ValueError):
        AppConfig.model_validate({"interface": {"environment_label": "line\nbreak"}})
    with pytest.raises(ValueError):
        AppConfig.model_validate({"interface": {"default_text_scale": "huge"}})


def test_attention_scan_budget_is_bounded() -> None:
    assert AppConfig().attention_scan_budget == 8
    assert AppConfig(attention_scan_budget=1).attention_scan_budget == 1
    assert AppConfig(attention_scan_budget=64).attention_scan_budget == 64
    with pytest.raises(ValueError):
        AppConfig(attention_scan_budget=0)
    with pytest.raises(ValueError):
        AppConfig(attention_scan_budget=65)


def test_health_config_defaults_are_enabled_and_scan_common_roots() -> None:
    health = AppConfig().health
    assert health.enabled
    assert health.disk_space_enabled
    assert health.apt_updates_enabled
    assert health.reboot_required_enabled
    assert health.git_dirty_enabled
    assert health.docker_enabled
    assert Path("/srv/projects") in health.project_scan_roots
    assert (Path.home() / "workspace" / "projects") in health.project_scan_roots


def test_health_config_scan_roots_expand_tilde() -> None:
    health = HealthConfig(project_scan_roots=(Path("~/some-projects"),))
    assert health.project_scan_roots[0].is_absolute()
    assert health.project_scan_roots[0] == Path.home() / "some-projects"


def test_health_config_rejects_fail_threshold_above_warn_threshold() -> None:
    HealthConfig(disk_warn_percent=10, disk_fail_percent=2)
    with pytest.raises(ValueError, match="disk_fail_percent"):
        HealthConfig(disk_warn_percent=5, disk_fail_percent=10)


def test_health_config_ttls_are_bounded() -> None:
    with pytest.raises(ValueError):
        HealthConfig(disk_ttl_seconds=1.0)
    with pytest.raises(ValueError):
        HealthConfig(git_scan_budget=0)


def test_notification_config_rejects_plaintext_telegram_api_base() -> None:
    NotificationConfig(telegram_api_base="https://relay.example.com")
    with pytest.raises(ValueError, match="https://"):
        NotificationConfig(telegram_api_base="http://relay.example.com")


def test_automation_hook_event_validation() -> None:
    hook = AutomationHookConfig(
        event="session.created",
        command=("echo", "ok"),
    )
    assert hook.api_version == 1
    with pytest.raises(ValueError, match="unsupported automation hook event"):
        AutomationHookConfig(event="session.unknown", command=("echo",))


def test_operator_profiles_and_playbooks_load() -> None:
    config = AppConfig.model_validate(
        {
            "operator_profiles": [
                {
                    "name": "ops",
                    "default_project": "platform",
                    "require_approvals": True,
                    "approval_code": "1234",
                    "allowed_actions": ["delete"],
                }
            ],
            "playbooks": [
                {
                    "name": "diag",
                    "match_any": ["timeout"],
                    "commands": [["echo", "ok"]],
                    "timeout_seconds": 5.0,
                }
            ],
            "self_heal": {
                "enabled": True,
                "rules": [
                    {
                        "name": "quota-recovery",
                        "when_checks": ["zombie-sessions"],
                        "action": "playbook",
                        "playbook": "diag",
                        "session_scope": "related",
                    }
                ],
            },
        }
    )
    assert isinstance(config.operator_profiles[0], OperatorProfileConfig)
    assert config.operator_profiles[0].name == "ops"
    assert isinstance(config.playbooks[0], PlaybookConfig)
    assert config.playbooks[0].name == "diag"
    assert config.self_heal.enabled is True
    assert config.self_heal.rules[0].name == "quota-recovery"


def test_approvals_hardening_and_remediation_chain_config_load() -> None:
    config = AppConfig.model_validate(
        {
            "approvals": {
                "enabled": True,
                "guarded_actions": ["delete", "federation-action:resume:vm-a"],
                "token_signing_secret": "secret",
                "token_ttl_seconds": 600,
                "dual_control_enabled": True,
                "dual_control_actions": ["federation-action:resume:vm-a"],
            },
            "remediation_chains": [
                {
                    "name": "stability-recovery",
                    "when_checks": ["disk-space"],
                    "steps": ["playbook:quota-hit", "archive"],
                    "rollback_playbook": "quota-hit",
                }
            ],
            "self_heal": {
                "enabled": True,
                "rules": [
                    {
                        "name": "chain-runner",
                        "when_checks": ["disk-space"],
                        "action": "chain",
                        "chain": "stability-recovery",
                    }
                ],
            },
        }
    )
    assert config.approvals.dual_control_enabled is True
    assert config.approvals.token_ttl_seconds == 600
    assert config.remediation_chains[0].name == "stability-recovery"
    assert config.self_heal.rules[0].action == "chain"
