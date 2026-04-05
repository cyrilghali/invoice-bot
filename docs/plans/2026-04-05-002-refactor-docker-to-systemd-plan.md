---
title: "refactor: Switch from Docker to native systemd service"
type: refactor
status: completed
date: 2026-04-05
---

# refactor: Switch from Docker to native systemd service

## Overview

Replace the Docker-based deployment with a native Python process managed by a systemd user service. The bot must run 24/7 and auto-restart on failure or machine reboot. Also create a CLAUDE.md for the project.

## Problem Frame

The bot currently runs in Docker with `restart: always`. Now that the classifier uses `claude -p` (which needs the host CLI), Docker adds friction — the `claude` binary isn't available inside the container. Running natively on the Ubuntu machine simplifies this and avoids Docker overhead.

The bot must survive reboots and crashes automatically, which Docker's `restart: always` currently provides. Systemd user services with `loginctl enable-linger` replicate this behavior.

## Requirements Trace

- R1. Bot runs as a systemd user service under `cyrilghali`
- R2. Bot auto-starts on boot and auto-restarts on crash (like Docker `restart: always`)
- R3. Default paths change from `/app/data` and `/app/config.yaml` to repo-local `data/` and `config.yaml`
- R4. No Docker dependency required to run the bot
- R5. CLAUDE.md created with project conventions and run instructions

## Scope Boundaries

- Not deleting Docker files (Dockerfile, docker-compose.yml) — they can stay for reference or alternative use
- Not changing any application logic, classification, or pipeline behavior
- Not changing the test suite (tests don't depend on Docker paths)

## Key Technical Decisions

- **Systemd user service (not system-level)**: Runs under `cyrilghali` without root. Uses `~/.config/systemd/user/` and `loginctl enable-linger` for boot persistence.
- **Restart policy**: `Restart=on-failure` with `RestartSec=10` — mirrors Docker's `restart: always` behavior for crashes while not restarting on clean shutdown.
- **Path defaults**: Change `DEFAULT_DATA_DIR` from `/app/data` to `data` (repo-relative) and `CONFIG_PATH` default from `/app/config.yaml` to `config.yaml`. Both remain overridable via env vars for backwards compatibility.
- **Environment file**: The systemd unit loads `.env` via `EnvironmentFile` for `AZURE_CLIENT_ID` and any other secrets.

## Open Questions

### Resolved During Planning

- **Why user service, not system service?** The bot runs as `cyrilghali`, needs access to their `claude` CLI and home directory. User service is simpler and doesn't need root.
- **What about `loginctl enable-linger`?** Required so the user's systemd services start at boot, not just at login. One-time command.

### Deferred to Implementation

- **Exact `WorkingDirectory` path**: Will use the repo checkout path, discovered at implementation time.

## Implementation Units

- [ ] **Unit 1: Update default paths in utils.py**

**Goal:** Change hardcoded `/app/` defaults to repo-relative paths

**Requirements:** R3

**Dependencies:** None

**Files:**
- Modify: `src/utils.py`
- Test: `tests/test_utils.py`

**Approach:**
- Change `DEFAULT_DATA_DIR` from `/app/data` to `data`
- Change `CONFIG_PATH` default from `/app/config.yaml` to `config.yaml`
- Change `setup_logging` default `data_dir` parameter from `/app/data` to `data`
- Env var overrides (`DATA_DIR`, `CONFIG_PATH`) continue to work — no behavior change for anyone who sets them

**Patterns to follow:**
- Existing env var overlay pattern in `load_config()`

**Test scenarios:**
- Happy path: `load_config()` with `CONFIG_PATH` pointing to a local file works as before
- Happy path: `DEFAULT_DATA_DIR` is now `data` not `/app/data`
- Edge case: `CONFIG_PATH` env var still overrides the new default

**Verification:**
- All existing tests pass with the new defaults
- Bot can start with `config.yaml` in the repo root and `data/` directory

- [ ] **Unit 2: Create systemd user service file**

**Goal:** Create a systemd unit file that keeps the bot running 24/7

**Requirements:** R1, R2, R4

**Dependencies:** Unit 1

**Files:**
- Create: `invoice-bot.service`

**Approach:**
- Unit type: `simple` (APScheduler's `BlockingScheduler` runs in foreground)
- `WorkingDirectory` = repo path
- `ExecStart` = `python3 src/main.py`
- `Restart=on-failure`, `RestartSec=10`
- `EnvironmentFile` = repo path `.env`
- `Environment=PATH` must include the directory where `claude` CLI lives (typically `~/.claude/local/`)

**Test expectation: none -- pure config file, verified by systemd loading it**

**Verification:**
- `systemctl --user status invoice-bot` shows the service loaded
- After `systemctl --user start invoice-bot`, the bot polls the inbox
- After `kill <pid>`, systemd restarts the bot automatically

- [ ] **Unit 3: Create CLAUDE.md**

**Goal:** Document project conventions, setup, and run instructions for Claude Code

**Requirements:** R5

**Dependencies:** Unit 1, Unit 2

**Files:**
- Create: `CLAUDE.md`

**Approach:**
- Project description and purpose
- How to run: `systemctl --user start invoice-bot` (production) and `python3 src/main.py` (development)
- How to test: `python3 -m pytest tests/`
- Key architecture: scheduler → poller → classifier → pipeline → uploader
- Config: `config.yaml` + `.env` for secrets
- Data: `data/` directory (SQLite, token cache, logs)
- Note: classifier uses `claude -p` CLI, not the Anthropic SDK
- Conventions: Python 3.12, pytest, French prompts for classification

**Test expectation: none -- documentation file**

**Verification:**
- CLAUDE.md exists at repo root with accurate, up-to-date instructions

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| `loginctl enable-linger` not run → service doesn't start at boot | Document in CLAUDE.md and README as a one-time setup step |
| `claude` CLI not on PATH in systemd environment | Explicitly set `PATH` in the service unit to include `~/.claude/local/` |
| Existing `data/` directory permissions | Bot already creates `data/` with `os.makedirs(exist_ok=True)` — no change needed |

## Documentation / Operational Notes

- One-time setup: `loginctl enable-linger cyrilghali`
- Install service: `cp invoice-bot.service ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now invoice-bot`
- View logs: `journalctl --user -u invoice-bot -f`

## Sources & References

- Related code: `src/utils.py` (path defaults), `src/main.py` (entry point), `Dockerfile`, `docker-compose.yml`
