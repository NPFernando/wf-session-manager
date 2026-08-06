"""XDG-compatible application path discovery."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_path, user_config_path, user_state_path


@dataclass(frozen=True, slots=True)
class AppPaths:
    config_dir: Path
    state_dir: Path
    cache_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def sessions_dir(self) -> Path:
        return self.state_dir / "sessions"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / ".state.lock"

    @property
    def presets_file(self) -> Path:
        return self.state_dir / "presets.json"

    @property
    def filter_presets_file(self) -> Path:
        return self.state_dir / "filter-presets.json"

    @property
    def templates_file(self) -> Path:
        return self.state_dir / "templates.json"

    @property
    def undo_file(self) -> Path:
        return self.state_dir / "undo.json"

    @property
    def dependencies_file(self) -> Path:
        return self.state_dir / "dependencies.json"

    @property
    def search_index_file(self) -> Path:
        return self.cache_dir / "search-index.json"

    @property
    def search_queries_file(self) -> Path:
        return self.state_dir / "search-queries.json"

    @property
    def federation_dashboards_file(self) -> Path:
        return self.state_dir / "federation-dashboards.json"

    @property
    def federation_fleet_snapshots_file(self) -> Path:
        return self.state_dir / "federation-fleet-snapshots.json"

    @property
    def incidents_file(self) -> Path:
        return self.state_dir / "incidents.json"

    @property
    def audit_log_file(self) -> Path:
        return self.state_dir / "audit.log"

    @property
    def timeline_dir(self) -> Path:
        return self.state_dir / "timeline"

    @property
    def interface_preferences_file(self) -> Path:
        return self.state_dir / "interface.json"

    @property
    def migrations_dir(self) -> Path:
        return self.state_dir / "migrations"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def onboarding_file(self) -> Path:
        return self.state_dir / ".onboarding-v1"

    @property
    def diagnostics_dir(self) -> Path:
        return self.cache_dir / "diagnostics"

    @property
    def backups_dir(self) -> Path:
        return self.cache_dir / "backups"

    @property
    def health_dir(self) -> Path:
        return self.cache_dir / "health"

    @property
    def migration_lock_file(self) -> Path:
        return self.state_dir / ".migration.lock"

    @classmethod
    def discover(cls, namespace: str = "workspace-session-manager") -> AppPaths:
        """Use an isolated namespace; WS_DEV_ROOT makes tests fully hermetic."""
        isolated_root = os.environ.get("WS_DEV_ROOT")
        if isolated_root:
            root = Path(isolated_root).expanduser().resolve()
            return cls(root / "config", root / "state", root / "cache")

        return cls(
            config_dir=Path(user_config_path(namespace, appauthor=False)),
            state_dir=Path(user_state_path(namespace, appauthor=False)),
            cache_dir=Path(user_cache_path(namespace, appauthor=False)),
        )
