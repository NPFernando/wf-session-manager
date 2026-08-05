from pathlib import Path

import pytest

from workspace_session_manager.config import (
    AppConfig,
    HealthConfig,
    NotificationConfig,
    load_config,
)
from workspace_session_manager.errors import ConfigurationError
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
    configured = AppConfig.model_validate(
        {
            "interface": {
                "environment_display": "label",
                "environment_label": "Staging",
                "default_grouping": "project",
                "default_density": "compact",
            }
        }
    ).interface
    assert configured.environment_label == "Staging"
    with pytest.raises(ValueError):
        AppConfig.model_validate({"interface": {"environment_display": "always"}})
    with pytest.raises(ValueError):
        AppConfig.model_validate({"interface": {"environment_label": "line\nbreak"}})


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
