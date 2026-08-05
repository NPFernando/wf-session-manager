from __future__ import annotations

import stat
from pathlib import Path

import pytest

from conftest import FakeBackend
from workspace_session_manager.errors import MigrationError, StateError, TmuxError
from workspace_session_manager.legacy import LegacyMetadataReader
from workspace_session_manager.migration import MigrationJournal, MigrationManager
from workspace_session_manager.models import SessionMetadata, TaskState, Tool
from workspace_session_manager.paths import AppPaths
from workspace_session_manager.store import MetadataStore


def write_legacy(
    root: Path,
    name: str,
    *,
    tool: str = "claude",
    cwd: str = "/tmp",
    note: str = "legacy task",
    tags: tuple[str, ...] = (),
    state: str = "active",
    pinned: bool = False,
    project: str = "",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    values = {"tool": tool, "cwd": cwd, "note": note, "state": state}
    for suffix, value in values.items():
        (root / f"{name}.{suffix}").write_text(f"{value}\n", encoding="utf-8")
    if tags:
        (root / f"{name}.tags").write_text("\n".join(tags) + "\n", encoding="utf-8")
    if pinned:
        (root / f"{name}.pinned").touch()
    if project:
        (root / f"{name}.project").write_text(f"{project}\n", encoding="utf-8")


def make_manager(
    tmp_path: Path,
    fake_backend: FakeBackend,
    roots: tuple[Path, ...],
) -> MigrationManager:
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    return MigrationManager(
        backend=fake_backend,
        store=MetadataStore(paths),
        legacy=LegacyMetadataReader(roots),
        paths=paths,
    )


def test_preview_writes_private_exact_id_plan(tmp_path: Path, fake_backend: FakeBackend) -> None:
    legacy = tmp_path / "legacy"
    write_legacy(
        legacy,
        "claude-old",
        note="private note",
        tags=("backend", "urgent"),
        pinned=True,
    )
    fake_backend.add("claude-old", session_id="$17", command="claude")
    manager = make_manager(tmp_path, fake_backend, (legacy,))

    plan = manager.preview(["claude-old", "claude-old"])
    assert len(plan.items) == 1
    assert plan.items[0].tmux_session_id == "$17"
    assert plan.items[0].tool is Tool.CLAUDE
    assert plan.items[0].note == "private note"
    assert plan.items[0].tags == ["backend", "urgent"]
    assert plan.items[0].pinned
    assert plan.items[0].sources[0].hashes["note"]
    assert plan.items[0].sources[0].hashes["tags"]

    plan_path = tmp_path / "review" / "plan.json"
    manager.write_plan(plan, plan_path)
    assert stat.S_IMODE(plan_path.stat().st_mode) == 0o600
    assert manager.load_plan(plan_path) == plan
    assert manager.validate_plan(plan_path) == plan
    assert not manager.paths.migrations_dir.exists()
    assert fake_backend.get_option("claude-old", "@wf_owner") is None


def test_preview_rejects_oversized_legacy_project_name(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    legacy = tmp_path / "legacy"
    write_legacy(legacy, "claude-old", project="p" * 250)
    fake_backend.add("claude-old", session_id="$17", command="claude")
    manager = make_manager(tmp_path, fake_backend, (legacy,))

    with pytest.raises(MigrationError, match="project name exceeds 200 characters"):
        manager.preview(["claude-old"])


def test_plan_reader_rejects_non_private_permissions(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    legacy = tmp_path / "legacy"
    write_legacy(legacy, "claude-old")
    fake_backend.add("claude-old")
    manager = make_manager(tmp_path, fake_backend, (legacy,))
    plan_path = tmp_path / "plan.json"
    manager.write_plan(manager.preview(), plan_path)
    plan_path.chmod(0o640)

    with pytest.raises(MigrationError, match="owner-only permissions"):
        manager.validate_plan(plan_path)


def test_journal_reader_rejects_non_private_permissions(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    legacy = tmp_path / "legacy"
    write_legacy(legacy, "claude-old")
    fake_backend.add("claude-old")
    manager = make_manager(tmp_path, fake_backend, (legacy,))
    plan_path = tmp_path / "plan.json"
    manager.write_plan(manager.preview(), plan_path)
    journal = manager.apply(plan_path)
    manager._journal_path(journal.migration_id).chmod(0o644)

    with pytest.raises(MigrationError, match="owner-only permissions"):
        manager.load_journal(journal.migration_id)
    with pytest.raises(MigrationError, match="owner-only permissions"):
        manager.status()


def test_apply_and_rollback_never_remove_tmux_session(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    legacy = tmp_path / "legacy"
    write_legacy(legacy, "codex-old", tool="codex", state="waiting")
    session = fake_backend.add("codex-old", session_id="$18", command="codex")
    manager = make_manager(tmp_path, fake_backend, (legacy,))
    plan_path = tmp_path / "plan.json"
    manager.write_plan(manager.preview([session.name]), plan_path)

    applied = manager.apply(plan_path)
    record = manager.store.load(session.name)
    assert applied.status == "applied"
    assert record is not None
    assert record.tmux_session_id == session.session_id
    assert record.task_state is TaskState.WAITING
    assert fake_backend.get_option(session.name, "@wf_owner") == "workspace-session-manager"

    rolled_back = manager.rollback(applied.migration_id)
    assert rolled_back.status == "rolled_back"
    assert manager.store.load(session.name) is None
    assert fake_backend.get_option(session.name, "@wf_owner") is None
    assert fake_backend.get_session(session.name).session_id == session.session_id


def test_apply_rejects_changed_sidecar_snapshot(tmp_path: Path, fake_backend: FakeBackend) -> None:
    legacy = tmp_path / "legacy"
    write_legacy(legacy, "claude-old")
    fake_backend.add("claude-old", session_id="$19")
    manager = make_manager(tmp_path, fake_backend, (legacy,))
    plan_path = tmp_path / "plan.json"
    manager.write_plan(manager.preview(["claude-old"]), plan_path)
    (legacy / "claude-old.note").write_text("changed\n", encoding="utf-8")

    with pytest.raises(MigrationError, match="snapshot changed"):
        manager.validate_plan(plan_path)
    with pytest.raises(MigrationError, match="snapshot changed"):
        manager.apply(plan_path)
    assert manager.store.load("claude-old") is None
    assert fake_backend.get_option("claude-old", "@wf_owner") is None


def test_partial_apply_restores_every_owner_marker(
    tmp_path: Path,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "legacy"
    for name in ("claude-one", "codex-two"):
        write_legacy(legacy, name, tool=name.split("-", maxsplit=1)[0])
        fake_backend.add(name)
    manager = make_manager(tmp_path, fake_backend, (legacy,))
    plan_path = tmp_path / "plan.json"
    manager.write_plan(manager.preview(), plan_path)
    original_save_new = manager.store.save_new

    def fail_second(record: SessionMetadata) -> None:
        if record.name == "codex-two":
            raise StateError("simulated write failure")
        original_save_new(record)

    monkeypatch.setattr(manager.store, "save_new", fail_second)
    with pytest.raises(StateError, match="simulated"):
        manager.apply(plan_path)

    assert manager.store.load("claude-one") is None
    assert manager.store.load("codex-two") is None
    assert fake_backend.get_option("claude-one", "@wf_owner") is None
    assert fake_backend.get_option("codex-two", "@wf_owner") is None
    assert manager.status()[0].status == "rolled_back"


def test_rollback_refuses_modified_migration_record(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    legacy = tmp_path / "legacy"
    write_legacy(legacy, "claude-old")
    fake_backend.add("claude-old")
    manager = make_manager(tmp_path, fake_backend, (legacy,))
    plan_path = tmp_path / "plan.json"
    manager.write_plan(manager.preview(), plan_path)
    journal = manager.apply(plan_path)
    record = manager.store.load("claude-old")
    assert record is not None
    manager.store.save(record.model_copy(update={"note": "changed after adoption"}))

    with pytest.raises(MigrationError, match="record was modified"):
        manager.rollback(journal.migration_id)
    assert manager.store.load("claude-old") is not None
    assert fake_backend.get_option("claude-old", "@wf_owner") == "workspace-session-manager"


def test_rollback_failure_restores_entire_adoption_batch(
    tmp_path: Path,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "legacy"
    for name in ("claude-one", "codex-two"):
        write_legacy(legacy, name, tool=name.split("-", maxsplit=1)[0])
        fake_backend.add(name)
    manager = make_manager(tmp_path, fake_backend, (legacy,))
    plan_path = tmp_path / "plan.json"
    manager.write_plan(manager.preview(), plan_path)
    journal = manager.apply(plan_path)
    original_unset = fake_backend.unset_option

    def fail_last(name: str, option: str, expected_id: str | None = None) -> None:
        if name == "claude-one":
            raise StateError("simulated tmux failure")
        original_unset(name, option, expected_id=expected_id)

    monkeypatch.setattr(fake_backend, "unset_option", fail_last)
    with pytest.raises(MigrationError, match="adoption was restored"):
        manager.rollback(journal.migration_id)

    for name in ("claude-one", "codex-two"):
        assert manager.store.load(name) is not None
        assert fake_backend.get_option(name, "@wf_owner") == "workspace-session-manager"
    assert manager.load_journal(journal.migration_id).status == "applied"


def test_preview_rejects_conflicting_legacy_roots(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    first = tmp_path / "legacy-one"
    second = tmp_path / "legacy-two"
    write_legacy(first, "claude-old", note="first")
    write_legacy(second, "claude-old", note="second")
    fake_backend.add("claude-old")
    manager = make_manager(tmp_path, fake_backend, (first, second))

    with pytest.raises(MigrationError, match="conflicting"):
        manager.preview()


def test_preview_rejects_symlinked_sidecar_and_plan_parent(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    legacy = tmp_path / "legacy"
    write_legacy(legacy, "claude-old")
    target = tmp_path / "private"
    target.write_text("secret\n", encoding="utf-8")
    (legacy / "claude-old.last").symlink_to(target)
    fake_backend.add("claude-old")
    manager = make_manager(tmp_path, fake_backend, (legacy,))

    with pytest.raises(MigrationError, match="unsafe sidecar"):
        manager.preview()

    (legacy / "claude-old.last").unlink()
    plan = manager.preview()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(tmp_path / "actual", target_is_directory=True)
    with pytest.raises(MigrationError, match="symlink component"):
        manager.write_plan(plan, linked_parent / "plan.json")


def test_preview_requires_an_eligible_session(tmp_path: Path, fake_backend: FakeBackend) -> None:
    manager = make_manager(tmp_path, fake_backend, (tmp_path / "legacy",))
    with pytest.raises(MigrationError, match="no eligible"):
        manager.preview()


def test_plan_id_cannot_overwrite_an_existing_journal(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    legacy = tmp_path / "legacy"
    write_legacy(legacy, "claude-old")
    fake_backend.add("claude-old")
    manager = make_manager(tmp_path, fake_backend, (legacy,))
    plan_path = tmp_path / "plan.json"
    manager.write_plan(manager.preview(), plan_path)
    journal = manager.apply(plan_path)
    manager.rollback(journal.migration_id)

    with pytest.raises(MigrationError, match="journal already exists"):
        manager.validate_plan(plan_path)
    with pytest.raises(MigrationError, match="journal already exists"):
        manager.apply(plan_path)
    assert manager.store.load("claude-old") is None
    assert fake_backend.get_option("claude-old", "@wf_owner") is None


def test_apply_finalization_failure_rolls_adoption_back(
    tmp_path: Path,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "legacy"
    write_legacy(legacy, "claude-old")
    fake_backend.add("claude-old")
    manager = make_manager(tmp_path, fake_backend, (legacy,))
    plan_path = tmp_path / "plan.json"
    manager.write_plan(manager.preview(), plan_path)
    original_write = manager._write_journal
    failed = False

    def fail_applied_once(journal: MigrationJournal) -> None:
        nonlocal failed
        if journal.status == "applied" and not failed:
            failed = True
            raise StateError("simulated journal failure")
        original_write(journal)

    monkeypatch.setattr(manager, "_write_journal", fail_applied_once)
    with pytest.raises(MigrationError, match="adoption was rolled back"):
        manager.apply(plan_path)

    assert manager.store.load("claude-old") is None
    assert fake_backend.get_option("claude-old", "@wf_owner") is None
    assert manager.status()[0].status == "rolled_back"


def test_rollback_finalization_failure_restores_adoption(
    tmp_path: Path,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "legacy"
    write_legacy(legacy, "claude-old")
    fake_backend.add("claude-old")
    manager = make_manager(tmp_path, fake_backend, (legacy,))
    plan_path = tmp_path / "plan.json"
    manager.write_plan(manager.preview(), plan_path)
    journal = manager.apply(plan_path)
    original_write = manager._write_journal
    failed = False

    def fail_rolled_back_once(value: MigrationJournal) -> None:
        nonlocal failed
        if value.status == "rolled_back" and not failed:
            failed = True
            raise StateError("simulated journal failure")
        original_write(value)

    monkeypatch.setattr(manager, "_write_journal", fail_rolled_back_once)
    with pytest.raises(MigrationError, match="adoption was restored"):
        manager.rollback(journal.migration_id)

    assert manager.store.load("claude-old") is not None
    assert fake_backend.get_option("claude-old", "@wf_owner") == "workspace-session-manager"
    assert manager.load_journal(journal.migration_id).status == "applied"


def test_apply_refuses_name_reused_before_owner_marker(
    tmp_path: Path,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "legacy"
    write_legacy(legacy, "claude-old")
    fake_backend.add("claude-old", session_id="$original")
    manager = make_manager(tmp_path, fake_backend, (legacy,))
    plan_path = tmp_path / "plan.json"
    manager.write_plan(manager.preview(), plan_path)
    original_set_option = fake_backend.set_option
    replaced = False

    def replace_before_set(
        name: str,
        option: str,
        value: str,
        expected_id: str | None = None,
    ) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            fake_backend.sessions[name] = fake_backend.sessions[name].model_copy(
                update={"session_id": "$replacement"}
            )
        original_set_option(name, option, value, expected_id=expected_id)

    monkeypatch.setattr(fake_backend, "set_option", replace_before_set)
    with pytest.raises(TmuxError, match="ID mismatch"):
        manager.apply(plan_path)

    assert fake_backend.get_session("claude-old").session_id == "$replacement"
    assert fake_backend.get_option("claude-old", "@wf_owner") is None
    assert manager.store.load("claude-old") is None
    assert manager.status()[0].status == "rolled_back"
