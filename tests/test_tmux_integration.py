from __future__ import annotations

import os
from pathlib import Path

import pytest

from workspace_session_manager.legacy import LegacyMetadataReader
from workspace_session_manager.migration import MigrationManager
from workspace_session_manager.paths import AppPaths
from workspace_session_manager.store import MetadataStore
from workspace_session_manager.tmux import TmuxBackend

RUN_INTEGRATION = os.environ.get("WS_RUN_TMUX_INTEGRATION") == "1"


def isolated_backend(tmp_path: Path) -> TmuxBackend:
    return TmuxBackend(socket_path=tmp_path / "tmux.sock")


@pytest.mark.integration
@pytest.mark.skipif(
    not RUN_INTEGRATION,
    reason="set WS_RUN_TMUX_INTEGRATION=1 to use a disposable isolated tmux socket",
)
def test_real_tmux_create_capture_and_guarded_delete(tmp_path: Path) -> None:
    backend = isolated_backend(tmp_path)
    name = "managed"
    created = backend.create_session(
        name=name,
        cwd=tmp_path,
        shell_command=("/bin/bash", "--noprofile", "--norc"),
        agent_command=None,
    )
    try:
        assert created.name == name
        assert backend.get_session(name).session_id == created.session_id
        assert (
            backend.get_option(name, "@wf_owner", expected_id=created.session_id)
            == "workspace-session-manager"
        )
        assert backend.get_option(name, "mouse", expected_id=created.session_id) == "on"
        assert backend.get_option(name, "set-clipboard", expected_id=created.session_id) == "on"
        backend.capture_pane(name, 10, expected_id=created.session_id)
    finally:
        live = next((item for item in backend.list_sessions() if item.name == name), None)
        if live is not None and live.session_id == created.session_id:
            backend.kill_session(name, expected_id=created.session_id)
    assert not backend.session_exists(name)


@pytest.mark.integration
@pytest.mark.skipif(
    not RUN_INTEGRATION,
    reason="set WS_RUN_TMUX_INTEGRATION=1 to use a disposable isolated tmux socket",
)
def test_real_tmux_exact_id_adoption_and_rollback(tmp_path: Path) -> None:
    backend = isolated_backend(tmp_path)
    name = "adopted"
    created = backend.create_session(
        name=name,
        cwd=tmp_path,
        shell_command=("/bin/bash", "--noprofile", "--norc"),
        agent_command=None,
    )
    backend.unset_option(name, "@wf_owner", expected_id=created.session_id)
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / f"{name}.tool").write_text("shell\n", encoding="utf-8")
    (legacy / f"{name}.cwd").write_text(f"{tmp_path}\n", encoding="utf-8")
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    manager = MigrationManager(
        backend=backend,
        store=MetadataStore(paths),
        legacy=LegacyMetadataReader((legacy,)),
        paths=paths,
    )
    plan_path = tmp_path / "adoption-plan.json"
    try:
        manager.write_plan(manager.preview([name]), plan_path)
        journal = manager.apply(plan_path)
        assert backend.get_session(name).session_id == created.session_id
        manager.rollback(journal.migration_id)
        assert backend.get_session(name).session_id == created.session_id
        assert backend.get_option(name, "@wf_owner", expected_id=created.session_id) is None
    finally:
        live = next((item for item in backend.list_sessions() if item.name == name), None)
        if live is not None and live.session_id == created.session_id:
            backend.kill_session(name, expected_id=created.session_id)
    assert not backend.session_exists(name)
