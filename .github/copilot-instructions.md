# Copilot instructions for workspace-session-manager

## Project context

- This repository is a Python 3.11+ terminal application (`ws`) built with Textual + Typer.
- Session safety and tmux ownership checks are core behavior; avoid changes that can mutate or attach to arbitrary live tmux sessions.
- Keep lifecycle and migration changes narrowly scoped and explicit.

## Setup and validation

Use the existing `uv`-based workflow:

```bash
uv sync --locked --extra dev
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -m "not integration"
```

Run integration tests only when needed for tmux behavior:

```bash
WS_RUN_TMUX_INTEGRATION=1 uv run pytest -m integration -q --no-cov
```

## Coding expectations

- Follow strict typing (`mypy` is strict for `src`).
- Preserve security/privacy guarantees around pane reads, sanitization, and metadata ownership.
- Do not add broad exception swallowing or silent fallbacks.
- Keep changes surgical and aligned with existing patterns in `src/workspace_session_manager`.
