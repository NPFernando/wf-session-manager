# Testing

## Fast suite

```bash
uv sync --locked --extra dev
uv run pytest -m "not integration"
```

Unit and Textual pilot tests use temporary state, temporary legacy sidecars, and fake tmux backends.
They do not access the live tmux server or operational metadata.

## Real tmux

```bash
WS_RUN_TMUX_INTEGRATION=1 uv run pytest -m integration -q --no-cov
```

The integration tests use `tmux -S` with a socket inside pytest's temporary directory. Cleanup
requires the generated socket path, session name, and exact tmux ID. One test covers managed
creation; the other removes its test owner marker, adopts the same exact ID, rolls adoption back, and
verifies that the tmux session remained alive throughout.

## Visual regression

`pytest-textual-snapshot` records deterministic SVG frames for wide, medium, narrow, empty, warning,
failure, usage-limit, diagnostics, create, create-advanced, validation-error, filter, palette,
responsive Manage, filtered Manage, disabled Manage actions, identity and status forms,
destructive-confirmation, responsive Logs, Live and Saved sources, Logs find/pause/warning/error/empty
states, in-place narrow details at 80 and 99 columns, narrow detail warning/failure/long-content,
Attention scanning, responsive Attention warning and clear states, dark, light, monochrome, ASCII,
reduced-motion, diagnostics-running, long-content, 50-session, and 200-session states.
Reviewed before/after frames at
`160x45`, `120x35`, `100x30`, and `80x24` are stored under `docs/screenshots/`.
Fake-backend terminal recordings and replay instructions are stored under `docs/recordings/`; they
never connect to the live tmux server or production metadata.

Run layout snapshots separately from interactive behavior:

```bash
WS_SNAPSHOT_MODE=1 WS_SNAPSHOT_NOW=2099-01-01T00:00:00+00:00 \
  uv run pytest -m layout_snapshot tests/test_tui_snapshots.py
uv run pytest -m tui_behavior tests/test_tui.py
```

When intentional UI changes are made, update only the snapshot baseline:

```bash
WS_SNAPSHOT_MODE=1 WS_SNAPSHOT_NOW=2099-01-01T00:00:00+00:00 \
  uv run pytest -m layout_snapshot tests/test_tui_snapshots.py --snapshot-update
```

## Terminal compatibility matrix

This is a compatibility and evidence matrix, not a promise that every terminal behaves identically.
Rows marked **Untested** have not been verified by this project yet; please run the manual checks
below and report the terminal, version, locale, SSH client, and tmux version with the outcome.

| Environment | Expected behavior | Evidence/status |
| --- | --- | --- |
| Linux + Python 3.11/3.12/3.13 + tmux | Unit/TUI suite and isolated tmux lifecycle coverage | CI |
| 160x45, 120x35, 100x30, 80x24 terminal | Responsive dashboard snapshots | Automated snapshot coverage |
| 72x20 terminal | Very-narrow dashboard fallback | Automated TUI coverage |
| Below the dashboard minimum | Clear small-terminal guidance rather than clipped controls | Automated TUI coverage |
| Logs at 40x15 or larger | Logs workspace is available | Automated TUI coverage |
| Logs below 40x15 | Clear Logs size guidance | Automated TUI coverage |
| `NO_COLOR=1` | Monochrome theme and no optional motion | Automated TUI coverage |
| `WS_ASCII=1` or non-UTF terminal locale | ASCII-safe decorations | Automated TUI coverage |
| OpenSSH reconnect / local tmux client | Persistent sessions and attach/switch behavior | Manual: untested as a compatibility matrix entry |
| iTerm2, Windows Terminal, GNOME Terminal, Kitty, Alacritty, WezTerm | Keyboard, colour, Unicode, resize, and theme behavior | **Untested** |
| screen, mosh, serial consoles, browser terminals | Fallback rendering, input, and resize behavior | **Untested** |

For a constrained client, begin with `NO_COLOR=1 WS_ASCII=1 WS_MOTION=off uv run ws-dev` and use
`?` to review the available keyboard controls. Theme cycling (`t`) and the normal theme need a
colour-capable terminal; `--no-animation` is suitable for slow remote connections or motion-sensitive
use.

## Manual TUI matrix

Run `uv run textual run --dev workspace_session_manager.tui:WsApp` only with a suitable service fixture, or
run `uv run ws-dev` against the managed inventory. Use `uv run ws-dev list --all` for read-only diagnostics.

Check at least:

- `80x24`, `100x30`, `120x35`, and `160x45`
- resize through `72x20` and below the dashboard minimum; verify the fallback is legible and no
  action is reachable behind it
- Logs at `40x15` and below; verify its dedicated size guidance
- SSH disconnect and reconnect after creating a disposable `ws-dev` session
- inside-tmux switching and outside-tmux attachment
- missing Claude, Codex, and Hermes commands
- exact-name delete mismatch and cancellation
- unmanaged-session hiding and mutation rejection
- private-file and stale adoption-plan rejection, read-only validation, and exact-batch rollback
- preservation checksum enforcement and refusal to retire an active classic command
- simulated installer success, pre-cutover adoption rollback, and rollback-failure reporting
- real advisory-lock contention between isolated installer processes
- atomic command-switch failure recovery and command-backup collision refusal
- retirement archive payload corruption and relocatable checksum verification
- sanitized pane output containing ANSI, IP, home path, and test token patterns
- zero-result searches and empty inventories with no hidden actionable selection
- create validation failure with values preserved and Create disabled
- latest-value validation after rapid invalid-to-valid name changes
- Search suspension and exact focus/query/scroll restoration around the Create form
- exclusive Search, Filter, Palette, Manage, Form, and Confirmation mode transitions
- shortcut suppression while overlays are active and command dispatch after palette restoration
- identity-bound Manage operations and confirmation cancellation back to the originating action
- Manage local search plus highlighted-action and scroll restoration across nested edit forms
- final session-ID normalization, collision checks, prefix opt-out, and no duplicate tool prefix
- multiline task entry, advanced-option focus restoration, and incremental new-row insertion
- actionable startup failure with rollback verification, retry, details, and metadata cleanup states
- usage-limit propagation across header count, selected row, Activity, agent state, and Summary/Raw
- unselected usage-limit discovery, baseline notification suppression, warning resolution, and one
  aggregated post-baseline notification per scan batch
- priority/fair Attention rotation, bounded exact-ID inspection, stale identity/revision rejection,
  delayed-scan deduplication, retry recovery, and prior-alert retention on read failure
- temporary Attention entry and exact restoration of search, filter, selection, focus, list scroll,
  narrow detail state, and inspector/output positions
- slow diagnostics progress plus PASS/WARN/FAIL/INFO completion totals
- Cancel-focused confirmation for every protected Manage operation
- all built-in themes, monochrome, `NO_COLOR`, and `WS_ASCII=1` rendering
- `g` grouping and `z` density changes, including the configured defaults; environment display as
  hidden, label, and hostname
- output and session-list scroll position preservation across background refresh
- Live/Saved log switching, source-local selection restoration, follow/pause, manual retry, and find
- log-read failure recovery, stale worker rejection, exact-ID attach blocking, and timer cleanup
- narrow list/detail transitions, inspector scrolling, contextual Help, and stopped-session Manage
- narrow detail viewport restoration across Edit, Task, Logs, Manage, Help, refresh, and reopen
- safe narrow-detail exit after filter exclusion, identity replacement, session loss, or wide resize
- `--no-animation`, `WS_MOTION=off`, reduced motion, and focus restoration after modal cancellation
