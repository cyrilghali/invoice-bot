---
title: "refactor: Replace Anthropic SDK with claude -p CLI for classification"
type: refactor
status: completed
date: 2026-04-05
---

# refactor: Replace Anthropic SDK with claude -p CLI for classification

## Overview

Replace the `anthropic` Python SDK in the classifier with `claude -p` (Claude Code pipe mode) subprocess calls. This lets the user leverage their Claude subscription instead of paying for API access separately. Also upgrades from Haiku to Opus.

## Problem Frame

The classifier currently uses the Anthropic Python SDK with an API key to call Claude Haiku. Two quality issues motivate upgrading to Opus:

1. **Classifier confidence on hard inputs**: Cegedim sends a JPEG (`46.jpg`) of a car fleet invoice that Haiku can't read well — it gets `review` status. Netexial's 540KB PDF also got `review`. These end up in `_a_verifier/` for manual review. Opus has significantly better vision and document understanding.

2. **Supplier name inconsistency**: The same supplier appears under different extracted names — e.g., "Fresca" / "FRESCA" / "S.A.S. au capital de 830000..." are all the same company; "Rouquette" / "ROUQUETTE" / "ROUQUETTE SARL SAINT CYRIL" are all Rouquette. Opus should produce more consistent, normalized supplier names.

Additionally, the user wants to use their existing Claude subscription via the `claude` CLI tool instead of paying for separate API access.

## Requirements Trace

- R1. Classification must use `claude -p` subprocess instead of `anthropic` Python SDK
- R2. Model must be Claude Opus (via `--model opus`)
- R3. No `ANTHROPIC_API_KEY` required — the CLI uses the user's authenticated Claude subscription
- R4. All existing classification behavior preserved: text, image, and Excel inputs; JSON output parsing; confidence routing; supplier hint; owner name filtering
- R5. Error handling: subprocess failures (timeout, non-zero exit, malformed output) route to "review" like API errors do today

## Scope Boundaries

- Not changing the prompt content, JSON schema, or classification logic
- Not changing the pipeline, uploader, or any other module
- Not removing `anthropic` from requirements.txt yet (user may still want it for other purposes) — but removing the `import anthropic` from classifier.py
- Not changing the Docker setup (the user runs this on their computer where `claude` CLI is installed)

## Context & Research

### Relevant Code and Patterns

- `src/classifier.py`: Lines 130-202 — `_classify_text()` and `_classify_image()` use `client.messages.create()`
- `src/classifier.py`: Lines 275-378 — `is_invoice()` creates `anthropic.Anthropic(api_key=...)` client
- `src/utils.py`: Lines 96-102 — loads `ANTHROPIC_API_KEY` env var into config
- `config.example.yaml`: Lines 62-65 — classifier.api_key config

### Claude CLI Capabilities (from `claude --help`)

- `--print` / `-p`: Non-interactive pipe mode, reads prompt from argument or stdin
- `--model opus`: Selects Opus model
- `--system-prompt <prompt>`: Custom system prompt
- `--output-format json`: Returns structured JSON with `result` field
- `--json-schema <schema>`: Enforces JSON schema validation on output
- `--tools <tools>`: Restricts available tools (e.g., `"Read"` for image files, `""` to disable all)
- `--bare`: Minimal mode, skips hooks/plugins — good for programmatic use

## Key Technical Decisions

- **Subprocess via `subprocess.run`**: Simplest approach. Pass prompt as CLI argument, capture stdout. Use `timeout` parameter for safety.
- **`--bare` flag**: Use bare mode to skip hooks, plugins, and auto-discovery — faster and more predictable for programmatic calls.
- **Text classification — no tools needed**: Pipe the extracted text directly as part of the prompt argument. Use `--tools ""` to disable all tools for speed.
- **Image classification — temp file + Read tool**: Save image bytes to a temp file, ask Claude to read it. Use `--tools "Read"` so Claude can view the image file. Claude Code's Read tool handles images natively.
- **`--output-format text`**: Use plain text output (not JSON wrapper) since Claude's response is already the JSON we parse. The `--output-format json` wraps in an extra envelope which would complicate parsing.
- **`--json-schema`**: Not used — the existing `_parse_response()` already handles malformed JSON gracefully with fallback to review. Adding schema enforcement would cause hard failures instead.
- **Timeout**: 60 seconds per classification call. The CLI has startup overhead, and Opus is slower than Haiku.
- **Model configurable**: Add `classifier.model` to config (default: `opus`) so the user can switch models without code changes.

## Open Questions

### Resolved During Planning

- **How to handle images?** → Save to temp file, use `--tools "Read"` so Claude reads the image. Temp file cleaned up after.
- **How to pass the system prompt?** → `--system-prompt` CLI flag. The prompt is short enough for a command-line argument.
- **What about the API key validation logic?** → Remove it. No API key needed with CLI mode. Replace with a check that `claude` is available on PATH.

### Deferred to Implementation

- **Exact timeout value**: Starting with 60s, may need tuning based on Opus response times for invoice classification.
- **Whether `--bare` causes any issues with the user's setup**: Should be fine but only verifiable at runtime.

## Implementation Units

- [ ] **Unit 1: Refactor classifier.py to use claude CLI subprocess**

**Goal:** Replace all `anthropic` SDK usage with `subprocess.run` calls to `claude -p`

**Requirements:** R1, R2, R4, R5

**Dependencies:** None

**Files:**
- Modify: `src/classifier.py`
- Test: `tests/test_classifier.py`

**Approach:**
- Remove `import anthropic` and all `anthropic.Anthropic` client creation
- Replace `_classify_text()` signature: remove `client` parameter, add optional `model` parameter
- Implement `_run_claude_cli(prompt, system_prompt, model, tools, timeout)` helper that:
  - Builds command: `["claude", "-p", "--model", model, "--system-prompt", system_prompt, "--bare", "--tools", tools, prompt]`
  - Runs via `subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)`
  - Returns stdout stripped, or raises on non-zero exit
- Replace `_classify_text()` body: build the user prompt as before, call `_run_claude_cli` with `tools=""`
- Replace `_classify_image()`: accept `data` and `media_type`, write to `tempfile.NamedTemporaryFile` with correct extension, build prompt referencing the temp file path, call `_run_claude_cli` with `tools="Read"`, clean up temp file in `finally` block
- Update `is_invoice()`:
  - Remove API key loading and validation (lines 299-311)
  - Load `model` from `classifier_cfg.get("model", "opus")`
  - Pass `model` to `_classify_text` / `_classify_image` instead of `client`
- Update `MODEL` constant to `"opus"` (used as default, overridable via config)
- Keep `_parse_response()` completely unchanged
- Catch `subprocess.TimeoutExpired`, `FileNotFoundError` (claude not on PATH), and `subprocess.CalledProcessError` — all route to "review"

**Patterns to follow:**
- Existing error handling pattern in `is_invoice()` (lines 373-378): catch broad Exception, log warning, return "review"
- Existing temp file patterns in Python stdlib

**Test scenarios:**
- Happy path: `_run_claude_cli` returns valid JSON string, classification proceeds normally
- Happy path: text classification builds correct command with `--tools ""`
- Happy path: image classification writes temp file with correct extension, uses `--tools "Read"`
- Error path: `subprocess.TimeoutExpired` → returns review status
- Error path: `FileNotFoundError` (claude not installed) → returns review status  
- Error path: non-zero exit code → returns review status
- Edge case: temp file is cleaned up even when classification fails
- Integration: `is_invoice()` with PDF input calls `_classify_text` without `client` parameter
- Integration: `is_invoice()` loads model from config, defaults to "opus"

**Verification:**
- All existing `_parse_response` tests pass unchanged
- New subprocess tests pass with mocked `subprocess.run`
- `is_invoice()` no longer references `anthropic` module

- [ ] **Unit 2: Update config and documentation**

**Goal:** Remove API key requirement from config, add model setting

**Requirements:** R2, R3

**Dependencies:** Unit 1

**Files:**
- Modify: `src/utils.py`
- Modify: `config.example.yaml`
- Modify: `.env.example`

**Approach:**
- In `utils.py`: remove the `ANTHROPIC_API_KEY` env var overlay (lines 100-102) — no longer needed
- In `config.example.yaml`: replace `api_key` comment with `model` setting (default: `opus`), update comment to explain CLI mode
- In `.env.example`: remove `ANTHROPIC_API_KEY` line or comment it as deprecated

**Patterns to follow:**
- Existing config.example.yaml comment style

**Test scenarios:**
- Happy path: `load_config()` no longer injects `ANTHROPIC_API_KEY` into classifier config
- Edge case: config with no `classifier.model` key → defaults work in classifier

**Verification:**
- `load_config()` returns config without `classifier.api_key` when env var is unset
- `config.example.yaml` documents the new `model` setting

- [ ] **Unit 3: Clean up dependencies**

**Goal:** Remove `anthropic` from runtime dependencies

**Requirements:** R3

**Dependencies:** Unit 1, Unit 2

**Files:**
- Modify: `requirements.txt`

**Approach:**
- Remove `anthropic==0.40.0` line from `requirements.txt`

**Test expectation: none -- pure dependency removal, verified by import checks in Unit 1 tests**

**Verification:**
- `grep anthropic requirements.txt` returns nothing
- No import errors when running the test suite

## System-Wide Impact

- **Interaction graph:** Only `src/pipeline.py` calls `classifier.is_invoice()`. The public interface (arguments and return type) is unchanged — no downstream changes needed.
- **Error propagation:** Subprocess failures are caught in `is_invoice()` and routed to "review" — same fail-safe behavior as today's API errors.
- **State lifecycle risks:** Temp files for image classification must be cleaned up. Using `try/finally` ensures no leaked files.
- **Unchanged invariants:** `is_invoice()` return type `(status, invoice_date, supplier, amount_ht, amount_ttc, amount_tva, currency)` is unchanged. `_parse_response()` is unchanged. Pipeline and uploader are unaffected.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| `claude` CLI not available in Docker container | This is expected — the bot runs on the user's machine directly, not in Docker, when using CLI mode. Document this constraint. |
| CLI startup overhead makes classification slower | Acceptable tradeoff for using subscription. `--bare` flag minimizes overhead. |
| Opus is more expensive per-token than Haiku (even on subscription) | User explicitly requested Opus. Model is configurable via config if they want to change later. |
| Long system prompt may hit shell argument length limits | System prompt is ~500 chars — well within OS limits (typically 128KB+). |
| Image temp files on disk | Cleaned up in `finally` block. Using `tempfile` module ensures unique names and proper OS temp directory. |

## Sources & References

- Related code: `src/classifier.py`, `src/utils.py`, `src/pipeline.py`
- Claude CLI help: `claude --help` output confirming `-p`, `--model`, `--system-prompt`, `--bare`, `--tools` flags
