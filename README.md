# Workspace Session Manager

ws is a terminal application for creating, resuming, inspecting, and managing persistent Claude
Code, GitHub Copilot CLI, Codex CLI, Hermes Agent, and shell sessions on Linux. Textual provides the
default interface, Typer provides automation-friendly commands, and tmux keeps work alive across SSH
disconnects.

Repository development does not replace an installed `ws`, change login hooks, or adopt existing
tmux sessions. Release installation and cutover remain separate approval-gated operations.

## Highlights

- Grouped Textual dashboard with exclusive interaction modes, structured filters, configurable grouping
  and density, and responsive layouts
- Distinct Overview, Status, Activity, Recent Output, and protected Manage workflows
- Persistent detached sessions through tmux
- Claude, Copilot, Codex, Hermes, and shell profiles with strict TOML validation
- Separate runtime, task, agent, input, and alert states with notes, projects, tags, and pinning
- Background Attention checks with bounded exact-ID pane reads and a restorable warning-only view
- ANSI/OSC sanitization, secret redaction, and byte-and-line bounded pane and log views
- Source-aware Logs workspace with Live/Saved switching, follow/pause, find navigation, and copy
- Optional owner-only sanitized logging, usage-limit warnings, diagnostics export, and onboarding
- Session-aware command palette with categorized commands, shortcuts, and availability details
- Twelve built-in themes, explicit high-contrast mode, plus monochrome (`NO_COLOR`) and ASCII-compatible (`WS_ASCII=1`) rendering
- Subtle SSH-friendly motion with config, `--no-animation`, and `WS_MOTION=off` overrides
- Read-only discovery and preview of legacy ws sidecar metadata
- Exact-ID, snapshot-validated, reversible session adoption
- Ownership checks tied to both tmux's unique session ID and a tmux owner marker
- Managed-only default views with `list --all` for diagnostics
- JSON output for session discovery, inspection, and diagnostics
- XDG-compatible, permission-restricted, atomic state storage

## Requirements

- Linux
- Python 3.11 or newer
- tmux
- One or more optional agent commands: `claude`, `copilot`, `codex`, or `hermes`

No command in ws invokes `sudo`.

## Development setup

```bash
git clone https://github.com/NPFernando/workspace-session-manager.git
cd workspace-session-manager
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ws-dev doctor
.venv/bin/ws-dev
```

With `uv`:

```bash
uv sync --extra dev
uv run ws-dev doctor
uv run ws-dev
```

Run the development checks with the locked environment:

```bash
uv sync --locked --extra dev
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -m "not integration"
```

For GitHub Copilot cloud agent, repository-specific guidance is in
`.github/copilot-instructions.md`, and environment bootstrapping is defined in
`.github/workflows/copilot-setup-steps.yml`.

Development data lives under the `workspace-session-manager` XDG namespace. Operational legacy ws paths are
read only unless a reviewed adoption plan is explicitly applied; adoption does not change those
paths or restart a tmux session.

## CLI

```bash
ws-dev                         # Open the Textual interface
ws-dev --no-animation          # Open with all optional motion disabled
ws-dev --profile default       # Start with an operator profile selected
ws-dev setup                   # First-run wizard: detect tools and write config.toml
ws-dev quickstart              # Bootstrap config if needed, create first session, attach
ws-dev list
ws-dev list --all              # Include unmanaged sessions for diagnostics
ws-dev list --json
ws-dev inspect claude-api
ws-dev timeline claude-api
ws-dev handoff claude-api
ws-dev create --tool claude --name api --cwd ~/projects/api
ws-dev create --tool copilot --name support --cwd ~/projects/api
ws-dev create --tool codex --name review --cwd ~/projects/api --logging
ws-dev create --tool hermes --name planner --cwd ~/projects/api
ws-dev create --tool shell --name diagnostics --cwd ~
ws-dev create --from-session claude-api --name api-follow-up
ws-dev preset save backend --tool codex --cwd ~/projects/api --tag backend
ws-dev preset list
ws-dev preset validate --actionable
ws-dev filter-preset save blocked-hermes --tool hermes --task blocked --quick-filter blocked --grouping warning --density compact
ws-dev filter-preset list
ws-dev template save feature --tool shell --name-template "{project}-{ticket}" --cwd-template "~/workspace/projects/{project}"
ws-dev template list
ws-dev bulk claude-api codex-review --pin
ws-dev report
ws-dev report --output ~/ws-ops-report.txt
ws-dev handoff-auto --project api
ws-dev incident-bundle --project api --output ~/incident-api.tar.gz
ws-dev incident-bundle --include-federation --host vm-a --host vm-b
ws-dev incident start "api-timeout" --severity fail --session claude-api --host vm-a
ws-dev incident status --status open
ws-dev correlate
ws-dev timeline-global --action restarted
ws-dev suggest claude-api
ws-dev board --project api
ws-dev archive --dry-run
ws-dev federation-action health --host vm-a
ws-dev federation-action resume --host vm-a --approval 1234
ws-dev federation-action health --host vm-a --retries 3 --retry-delay 0.5
ws-dev federation-action-plan resume --host vm-a --json
ws-dev federation-capabilities --host vm-a --json
ws-dev federation-dashboard save ops-view --host vm-a --host vm-b --scope-all-hosts --safe-mode
ws-dev federation-dashboard open ops-view --json
ws-dev fleet-snapshot save baseline --host vm-a --host vm-b
ws-dev fleet-diff --left baseline --json
ws-dev backup --output ~/ws-backup.tar.gz
ws-dev restore ~/ws-backup.tar.gz
ws-dev undo
ws-dev federation --host vm-a --host vm-b
ws-dev profile list
ws-dev profile select default
ws-dev profile active
ws-dev policy-simulate
ws-dev approval issue federation-action:resume:vm-a --ttl 600
ws-dev dependency add api-worker api-db
ws-dev dependency critical-path
ws-dev search timeout
ws-dev search "tool:copilot project:api state:blocked timeout"
ws-dev search-reindex
ws-dev search-save blocked-api "project:api state:blocked timeout"
ws-dev search-saved
ws-dev ssh-profiler --latency-ms 900
ws-dev drill claude-api --action restarted
ws-dev playbook quick-diagnose --session claude-api
ws-dev playbook-list
ws-dev playbook quota-hit --preview
ws-dev snapshot-diff /tmp/report-a.json /tmp/report-b.json
ws-dev audit --limit 50
ws-dev chaos-check --apply
ws-dev self-heal --preview
ws-dev remediation-chain stability-recovery --preview
ws-dev recover --repair
ws-dev hooks
ws-dev create --from-preset backend --name api-review
ws-dev create --from-template feature --var project=api --var ticket=123
ws-dev note-structured claude-api --goal "Ship auth fix" --next-step "Open PR"
ws-dev edit claude-api --tag backend --state in_progress --input none --pin
ws-dev note claude-api "Refactor authentication flow"
ws-dev rename claude-api api-refactor
ws-dev resume
ws-dev attach claude-api
ws-dev delete claude-api       # Exact-name confirmation required
ws-dev doctor
ws-dev doctor --actionable
ws-dev migrate preview --all --output adoption-plan.json
ws-dev migrate validate adoption-plan.json
ws-dev migrate status
ws-dev onboarding reset
```

`create` defaults to the first enabled tool profile (`claude`, `copilot`, `codex`, `hermes`,
then `shell`) when `--tool` is omitted.
`doctor` and `health` include a **Fix** column with corrective actions when checks fail or warn.
`quickstart --preset <name>` creates the first session from a saved preset in one step.

Normal commands and the Textual dashboard operate only on managed sessions. A session is managed only
when its validated metadata, exact live tmux ID, and tmux owner marker agree. Unmanaged sessions are
hidden unless `list --all` is requested.

## Keyboard controls

| Key | Action |
| --- | --- |
| `Enter` | Attach, or open the in-place inspector at 80-99 columns |
| `Up`/`Down`, `j`/`k` | Navigate sessions; scroll an open narrow inspector |
| `c` | Create a session |
| `m` | Manage selected session |
| `i` | Show selected session timeline |
| `e` | Edit identity and organization |
| `n` | Edit the task description |
| `l` | Open the sanitized log view |
| `*` | Toggle pin |
| `d` | Open protected session actions |
| `Ctrl+A` | Mark all visible sessions for bulk actions |
| `Ctrl+U` | Clear all bulk marks |
| `Alt+I` | Invert bulk marks for visible sessions |
| `/` | Enter on-demand search mode |
| `f` | Filter by tool, runtime, task state, warning, or recent activity |
| `g` | Cycle the dashboard grouping |
| `z` | Toggle compact and comfortable row density |
| `w` | Cycle text scale (compact, comfortable, readable) |
| `M` | Cycle motion preset (auto, off, subtle, full) |
| `C` | Toggle high-contrast mode |
| `A` | Cycle accent style (default, vivid, calm, safe) |
| `K` | Toggle compact vs verbose action-bar hints |
| `I` | Open interface controls panel (theme, density, text, motion, contrast, accent, hints) |
| `h` | Open system health details |
| `v` | Open policy sandbox (what-if preview) |
| `D` | Open dependency graph view |
| `F` | Open federation control center |
| `1`..`6` | Quick filters: All, Active, Detached, Warnings, Stopped, Blocked |
| `7`..`9` | Set selected progress to Todo/Doing/Done |
| `0` | Macro: apply warnings filter and open logs for first warning session |
| `W` | Open warning triage wizard (select warning → inspect evidence → choose remediation) |
| `s` | Search saved session output |
| `o` | Open preset launcher |
| `U` | Undo last supported risky action |
| `p` | Open the command palette |
| `x` | Export an operations snapshot report |
| `B` | Open project board lanes (todo/doing/blocked/done) |
| `?` | Open contextual help |
| `r` | Refresh |
| `t` | Cycle built-in themes |
| `Esc` | Cancel search or return from a narrow detail screen |
| `q` | Quit |

The command palette includes one-step create actions for each enabled tool profile and saved presets.
It also includes saved dashboard filter presets (from `ws filter-preset save`) so named views can be reapplied quickly.
Alias and typo-tolerant matching is enabled for common intents (for example, "reload", "settings", or misspelled command names).
The action bar also shows which tool `c` will create by default (based on enabled profiles).
The Create dialog includes tool-specific readiness hints in the final summary before session launch.
It also shows explicit command readiness status (executable ready vs. profile misconfiguration).
Unsubmitted Create/Edit forms are restored after accidental cancel/close so draft inputs are not lost.

Open **Attention** from Quick Actions or the command palette to temporarily show sessions with known
warnings. `Esc` restores the prior query, filters, selection, focus, and list position. The header
shows scan progress until every eligible agent session has been checked; new warnings discovered
after that initial baseline are reported in one aggregated notification per scan batch.

At 80-99 columns, the first `Enter` opens the selected session's existing inspector at full width;
the next `Enter` attaches. `Esc` returns to the session list. Opening Edit, Task, Logs, Manage, or Help
from that view preserves its inspector position, output position, Summary/Raw mode, selection, and
filters. Starting Search or Filter intentionally returns to the list first.

Inside Logs, `f` pauses or resumes polling, `r` performs a manual read, and `/` opens literal,
case-insensitive find. `Enter` and `Shift+Enter` move between matches, `Ctrl+U` clears the query,
`t` switches between relative and absolute capture times, and `c` copies the selection or the loaded
sanitized output. Active sessions open on the Live pane; stopped sessions open on Saved output when
available. Pausing preserves the current selection and scroll position independently for each source.

The create dialog validates names and directories, detects duplicate sessions and Git projects,
suggests the most recently used enabled tool for a detected project (unless you choose one
manually), shows the exact command, and keeps Create disabled until required fields are valid. `d` opens a
searchable Manage action browser grouped into General, Runtime, and Danger sections. Identity,
task, and status edits use focused forms; stop, metadata removal, log deletion, and complete deletion
always require a separate confirmation with Cancel focused by default. Canceling a nested form or
confirmation returns to the originating Manage action. Modal workflows suspend Search and restore
its query, selection, focus, and scroll state when they close.

Use compact density (`z`) for one-line session rows in small terminals (including 80x24 SSH
clients) with reduced clutter and preserved keyboard-first navigation.

`ws timeline <session>` shows recent operational events (create/attach/restart/logging/status updates).
`ws handoff <session>` prints a handoff snapshot with task state, timeline, and recent output.
`ws report` emits a daily operations summary (text or JSON), and `ws report --output <path>`
writes a plaintext handoff report.
`ws timeline-global` shows a cross-session operations feed; `ws board` renders project lanes.
`ws archive` applies stale-completed archival policy; `ws suggest` maps known failure patterns to fixes.
`ws federation-action` runs supported remote actions on selected hosts. Guarded remote actions
(`resume`, `attach`) support host/action-specific approval contexts such as
`federation-action:resume:vm-a`. Remote actions use bounded retries (`federation.action_retry_*`)
and can be overridden per call with `--retries` and `--retry-delay`, including partial-success
failure summaries when only some hosts succeed.
`ws federation-action-plan` provides a dry-run blast-radius plan (targets, impacted session counts,
risk level, and approval readiness) before running remote actions.
`ws federation-capabilities` displays a host capability matrix for remote commands and tool support,
including unavailable capability reasons.
`ws federation-dashboard` saves and reopens named multi-host federation views for recurring operations.
`ws fleet-snapshot` and `ws fleet-diff` provide fleet drift mode by comparing host-state snapshots
across time (snapshot vs snapshot/live), highlighting operational differences and anomaly signals.
Federation safe mode can be toggled in the `F` view (`9`) or persisted per pinned view (`--safe-mode`)
to use lower scan budgets and slower refresh pacing for weak SSH links.
`ws incident start|status|update|close` adds an incident commander workflow for lifecycle tracking.
`ws recover` checks integrity and can repair corrupt auxiliary state files.
`ws policy-simulate` previews approval/SLA/archive policy impact.
`ws approval issue` issues signed and expiring approval tokens (`--approval-token`) for guarded actions.
`ws dependency` manages inter-session dependency edges and critical path.
`D` in the dashboard opens a dependency graph modal showing blocked-by/unblocks relations and critical path.
`F` in the dashboard opens a dedicated federation operations view with host cards, per-host health/status summaries, an SLA-breach panel (aging + ownership hints), fleet-diff panel (`8`), safe-mode toggle (`9`), pinned view controls (`5` save, `6` apply, `7` delete), one-key bulk host actions (`list`, `health`, `report`, `resume`), and drill-through keys (`w` warning focus, `m` manage, `l` logs) to jump directly into local session workflows.
`ws search` / `ws search-reindex` provide unified cross-session search with faceted syntax (`tool:`, `project:`, `state:`, `tag:`).
`ws search-save`, `ws search-saved`, and `ws search-delete` manage reusable saved search queries (`ws search --saved <name>`).
`ws ssh-profiler` scores SSH/terminal/load conditions and recommends `ssh-safe`, `balanced`, or `rich` refresh posture.
`ws drill` shows a targeted timeline+output drill-through for one session.
`ws playbook-list` lists configured and built-in remediation playbooks.
`ws playbook` supports preview-first execution (`--preview`) before running commands.
`ws snapshot-diff` compares report or snapshot artifacts.
`ws profile` lists/selects active operator profiles.
When an active profile sets `allowed_actions`, mutating operations are blocked unless the action is explicitly allowed.
`ws audit` prints recent audit lines.
`ws ux-audit` runs a lightweight UI consistency check (themes, controls, hint layering, and contrast hooks).
`ws ux-a11y-audit` runs accessibility-focused checks (contrast, motion, readable text scale, and keyboard discoverability).
`ws chaos-check` optionally injects synthetic artifacts for diagnostics validation.
`ws self-heal` runs optional remediation policies with dry-run preview or `--apply`, and logs audit entries.
`ws remediation-chain` runs policy-driven conditional remediation chains with optional rollback playbook hooks.
`ws handoff-auto` generates a multi-session handoff pack; `ws correlate` groups repeated recent-output lines across sessions.
`ws incident-bundle` exports a postmortem-ready archive (audit, health, timeline, recent output), and can include cross-host federation status/health/report evidence via `--include-federation`.
`ws backup` / `ws restore` handle portable metadata+preset+timeline snapshots.
`ws undo` rolls back the most recent supported risky action (short time window).
`ws federation` aggregates `ws list --json` from remote hosts over SSH.

## Configuration

Copy `config.example.toml` to:

```text
${XDG_CONFIG_HOME:-~/.config}/workspace-session-manager/config.toml
```

Or run `ws-dev setup` to detect available agent CLIs and generate the tool profile section
automatically (`--yes` for non-interactive defaults, `--force` to replace an existing file).

Configuration is parsed as TOML and validated by Pydantic. It is never evaluated as shell code.
Agent commands are argument arrays, which avoids shell interpolation in configuration parsing.
The `[interface]` table accepts `animations = "off" | "subtle" | "full"` and
`reduce_motion = true | false`. It also supports `environment_display = "hidden" | "label" |
"hostname"` (default: `hidden`), `environment_label` (shown only with `label`),
`default_grouping = "attention" | "runtime" | "agent" | "project" | "warning" | "recent"`
(default: `attention`), and `default_density = "compact" | "comfortable"` (default:
`comfortable`), plus `default_text_scale = "compact" | "comfortable" | "readable"` (default:
`comfortable`), and `performance_profile = "ssh-safe" | "balanced" | "rich"` (default:
`balanced`). `[[project_defaults]]` entries can predefine tool/tags/task/logging per project name.
Configuration is strict: unknown keys and invalid values prevent startup rather than
being silently ignored.
`[[operator_profiles]]` and `[[playbooks]]` add policy-aware operator context and
remediation command packs, while `[[automation_hooks]]` supports explicit `api_version = 1`.

`WS_MOTION=off` takes precedence for an individual launch; `--no-animation`, `WS_NO_ANIMATION=1`,
reduced motion, and monochrome mode also disable optional motion. `NO_COLOR=1` starts in monochrome.
`WS_ASCII=1` avoids
Unicode decorations, which is useful for terminals with incomplete Unicode support.
For copy/paste in SSH terminals, ws attempts native clipboard first and then OSC 52 fallback; set
`WS_DISABLE_OSC52=1` to disable OSC 52 if your terminal policy blocks it.

### Terminal, theme, and accessibility guidance

The dashboard is keyboard-first. Focused dialogs keep their own controls active, `Esc` cancels or
returns to the previous safe context, and destructive confirmations focus Cancel by default. Use
`?` at any time for contextual shortcuts and `p` for the command palette.

Press `t` to cycle the built-in themes: Ithaca, Dark, Contrast Dark, Light, Pastel Light,
Monochrome, Midnight, Night Owl, Cyberpunk, Terminal, Terminal Green, and Paper. Press `w` to
cycle text scale (`compact`, `comfortable`, `readable`), `M` for motion presets, `C` for explicit
high-contrast mode, and `A` for accent tuning (`default`, `vivid`, `calm`, `safe`). The Interface
Controls panel (`I`) also includes a one-step layout preset cycle (density + text scale together).
If your terminal or SSH client has
limited colour, set `NO_COLOR=1`; if it has limited Unicode support, set `WS_ASCII=1`. For slow
links, remote terminals, or motion-sensitive users, prefer `ws-dev --no-animation` or
`WS_MOTION=off`.

The layout adapts from wide views down to narrow detail views. At very small dimensions it shows an
instructional fallback instead of exposing clipped controls; the Logs workspace requires at least
40x15. See [terminal compatibility and test status](docs/testing.md#terminal-compatibility-matrix)
before relying on a terminal/client combination in production.

`attention_scan_budget` controls how many eligible agent sessions a refresh may inspect. Its default
is `8`, with a validated range of `1` to `64`. Each background check reads at most 20 sanitized lines
and 8 KiB from the exact tmux session ID. Derived alerts remain in memory and never rewrite session
metadata.

## Data model

New metadata is stored in:

```text
${XDG_STATE_HOME:-~/.local/state}/workspace-session-manager/sessions/
```

Each JSON file is owner-only and written atomically. Schema-v2 records include the exact tmux session
ID plus independent task and input states. Schema-v1 records remain readable and are normalized in
memory; reading alone does not rewrite them. If a session name is later reused, the stale record does
not grant ownership.

See [architecture](docs/architecture.md), [security](docs/security.md), and
[migration](docs/migration.md) for design details.

## Quality checks

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -m "not integration"
WS_SNAPSHOT_MODE=1 WS_SNAPSHOT_NOW=2099-01-01T00:00:00+00:00 uv run pytest -m layout_snapshot tests/test_tui_snapshots.py
uv run pytest -m tui_behavior tests/test_tui.py
WS_RUN_TMUX_INTEGRATION=1 uv run pytest -m integration -q --no-cov
make secret-scan
```

The real-tmux integration tests use socket paths inside pytest temporary directories. Cleanup removes
only exact test session IDs and temporary sockets. Adoption coverage also verifies that rollback does
not restart, rename, or remove its disposable tmux session.

## Project status

Version `0.2.0` adds the production dashboard hierarchy, protected interactions, responsive modes,
sanitized logging, structured diagnostics, theme support, and deterministic visual regression
coverage. Installing it over an existing release remains an explicit approval-gated step; see
[migration](docs/migration.md).

## License

MIT. See [LICENSE](LICENSE).
