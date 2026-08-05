import stat
from pathlib import Path

import pytest

from workspace_session_manager.errors import StateError
from workspace_session_manager.models import InterfacePreferences, Preset, SessionMetadata, Tool
from workspace_session_manager.paths import AppPaths
from workspace_session_manager.store import InterfacePreferencesStore, MetadataStore, PresetStore


def make_record(name: str = "claude-test") -> SessionMetadata:
    return SessionMetadata(
        tmux_session_id="$1",
        name=name,
        tool=Tool.CLAUDE,
        cwd=Path("/tmp"),
    )


def test_store_writes_owner_only_atomic_json(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    store = MetadataStore(paths)
    record = make_record()
    store.save(record)
    stored_path = paths.sessions_dir / "claude-test.json"
    assert store.load("claude-test") == record
    assert stat.S_IMODE(stored_path.stat().st_mode) == 0o600
    assert not list(paths.sessions_dir.glob(".claude-test.json.*"))


def test_store_rejects_symlinked_metadata(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    paths.sessions_dir.mkdir(parents=True)
    target = tmp_path / "outside"
    target.write_text(make_record().model_dump_json(), encoding="utf-8")
    (paths.sessions_dir / "claude-test.json").symlink_to(target)
    with pytest.raises(StateError, match="symlinked"):
        MetadataStore(paths).load("claude-test")


def make_preset(name: str = "backend-dev") -> Preset:
    return Preset(name=name, tool=Tool.SHELL, cwd=Path("/tmp"), tags=["backend"])


def test_preset_store_writes_owner_only_atomic_json(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    store = PresetStore(paths)
    preset = make_preset()
    store.save(preset)
    assert store.load("backend-dev") == preset
    assert stat.S_IMODE(paths.presets_file.stat().st_mode) == 0o600
    assert not list(paths.state_dir.glob(".presets.json.*"))


def test_preset_store_load_all_returns_every_saved_preset(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    store = PresetStore(paths)
    store.save(make_preset("backend-dev"))
    store.save(make_preset("frontend-dev"))
    presets = store.load_all()
    assert set(presets) == {"backend-dev", "frontend-dev"}


def test_preset_store_save_overwrites_existing_preset(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    store = PresetStore(paths)
    store.save(make_preset())
    updated = Preset(name="backend-dev", tool=Tool.CODEX, cwd=Path("/srv"))
    store.save(updated)
    assert store.load("backend-dev") == updated
    assert len(store.load_all()) == 1


def test_preset_store_delete_removes_preset(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    store = PresetStore(paths)
    store.save(make_preset())
    store.delete("backend-dev")
    assert store.load("backend-dev") is None


def test_preset_store_delete_missing_is_idempotent(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    store = PresetStore(paths)
    store.delete("does-not-exist")
    assert store.load_all() == {}


def test_preset_store_load_returns_none_when_missing(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    store = PresetStore(paths)
    assert store.load("nothing-here") is None


def test_preset_store_rejects_symlinked_presets_file(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    paths.state_dir.mkdir(parents=True)
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    paths.presets_file.symlink_to(target)
    with pytest.raises(StateError, match="symlinked"):
        PresetStore(paths).load_all()


def test_preset_store_write_unlocked_rejects_symlinked_presets_file(tmp_path: Path) -> None:
    """`_write_unlocked` guards symlinks on its own, independent of the
    `_read_all_unlocked` check `save()` happens to run first."""
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    paths.state_dir.mkdir(parents=True)
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    paths.presets_file.symlink_to(target)
    store = PresetStore(paths)
    preset = Preset(name="backend-dev", tool=Tool.SHELL, cwd=tmp_path)
    with pytest.raises(StateError, match="symlinked"):
        store._write_unlocked({"backend-dev": preset})


def test_interface_preferences_store_returns_defaults_without_creating_state(
    tmp_path: Path,
) -> None:
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    preferences = InterfacePreferencesStore(paths).load()
    assert preferences == InterfacePreferences()
    assert not paths.interface_preferences_file.exists()


def test_interface_preferences_store_writes_owner_only_atomic_json(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    store = InterfacePreferencesStore(paths)
    preferences = InterfacePreferences(grouping="project", density="compact")
    store.save(preferences)
    assert store.load() == preferences
    assert stat.S_IMODE(paths.interface_preferences_file.stat().st_mode) == 0o600
    assert not list(paths.state_dir.glob(".interface.json.*"))


def test_interface_preferences_store_rejects_symlinked_file(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    paths.state_dir.mkdir(parents=True)
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    paths.interface_preferences_file.symlink_to(target)
    store = InterfacePreferencesStore(paths)
    with pytest.raises(StateError, match="symlinked"):
        store.load()
    with pytest.raises(StateError, match="symlinked"):
        store.save(InterfacePreferences())
