---
title: "feat: Pluggable document sources"
type: feat
status: active
date: 2026-04-05
---

# feat: Pluggable document sources

## Overview

Make it easy to add new invoice sources (web scrapers, APIs, additional inboxes) alongside the existing email poller. Each source is a self-contained Python file in `src/sources/` exporting a `run(config, data_dir)` function that fetches documents and calls the existing classify/upload/DB functions directly. Lightweight sources are scheduled by APScheduler inside the bot. Heavy sources (headless browser scrapers) are triggered externally by systemd timers or cron.

## Problem Frame

The bot only fetches invoices from one Outlook inbox. Some suppliers have web portals or APIs. Adding a new source should be: write one Python file, add one config entry, done. Heavy scrapers shouldn't live inside the bot's long-running process.

## Requirements Trace

- R1. Each source is a `.py` file in `src/sources/` with a `run(config, data_dir)` function
- R2. Lightweight sources have `interval_minutes` in config and are scheduled by APScheduler
- R3. Heavy sources have no `interval_minutes` and are triggered externally (systemd timer / cron) via a CLI entry point
- R4. Dedup generalized to `(source_name, source_id)` in the DB — existing `processed_emails` data migrated to the new table, old table dropped
- R5. Source failures are isolated — one crashing source doesn't block others
- R6. Existing email source works identically, just relocated
- R7. Multiple instances of the same source module are supported — config maps instance names to modules via a `module` field (e.g. two inboxes using the same `email_source` module with different credentials)
- R8. Each `run()` execution is logged to a `source_runs` table (source_name, timestamps, counts, error) for observability

## Scope Boundaries

- Not building any new concrete sources yet — only the structure and the email source migration
- Not changing classifier, uploader, or Excel exporter

## Key Technical Decisions

- **No shared pipeline abstraction**: Each source calls `is_invoice()`, `upload_attachment()`, `save_invoice()` directly. The "pipeline" is just 3 function calls — not worth abstracting.
- **No SourceDocument dataclass**: Each source handles its own data shape internally. The only shared contract is the dedup DB functions.
- **`run()` as universal entry point**: Works for polling an inbox, scraping a portal, or calling an API. APScheduler or systemd/cron calls the same function.
- **Two scheduling modes**: Sources with `interval_minutes` in config are scheduled by APScheduler inside the bot. Sources without it are triggered externally — a thin CLI script (`python -m sources.run <instance_name>`) loads config and calls `run()`.
- **Instance name ≠ module name**: Config maps instance names to modules via `module` field (defaults to instance name). The instance name is the `source_name` in the DB. Allows multiple instances of the same module.
- **`email_source.py` not `email.py`**: Avoids shadowing Python's stdlib `email` module, which would cause subtle import bugs.
- **Clean DB cutover**: `processed_emails` rows migrated into `processed_documents`, old table dropped.
- **`source_runs` table for observability**: Each `run()` execution logs one row: source_name, timestamps, counts (documents found / new / invoices saved), and error message if any. Makes it trivial to debug "what happened last night" without parsing logs.

## High-Level Technical Design

> *Directional guidance, not implementation specification.*

```yaml
# config.yaml
sources:
  main_inbox:                    # instance name → source_name in DB
    module: email_source         # which .py to load (default: instance name)
    interval_minutes: 10         # → APScheduler manages this
    client_id: "..."
    whitelisted_senders: [...]
  secondary_inbox:
    module: email_source
    interval_minutes: 30
    client_id: "..."
  cegedim:                       # no interval_minutes → triggered externally
    username: "..."
    password_env: "CEGEDIM_PASS"
```

```
                    ┌─────────────────────────────────┐
                    │ main.py (long-running)           │
                    │   APScheduler                    │
                    │   ┌───────────────────────────┐  │
Lightweight:        │   │ every 10m: run(main_inbox)│──│──> sources/email_source.py
(in-process)        │   │ every 30m: run(sec_inbox) │──│──> sources/email_source.py
                    │   │ monthly:   send_report()  │  │
                    │   └───────────────────────────┘  │
                    └─────────────────────────────────┘

                    ┌─────────────────────────────────┐
Heavy:              │ systemd timer / cron             │
(external trigger)  │   daily: python -m sources.run cegedim │
                    └────────────────┬────────────────┘
                                     │
                                     ▼
                              sources/cegedim.py ──> shared functions
```

Both paths call the same `run(config, data_dir)` and use the same DB functions.

## Implementation Units

- [ ] **Unit 1: Generalize DB dedup from email_id to (source_name, source_id)**

**Goal:** Replace `processed_emails` with a source-agnostic `processed_documents` table so any source can track what it has already processed.

**Requirements:** R4

**Dependencies:** None

**Files:**
- Modify: `src/db.py`
- Modify: `tests/test_db.py`

**Approach:**
- Add `PRAGMA busy_timeout=5000` to `get_connection()` for concurrent source jobs
- Create `processed_documents` table: `source_name TEXT, source_id TEXT, processed_at TEXT, sender TEXT, subject TEXT, received_at TEXT, PRIMARY KEY (source_name, source_id)`
- Create `source_runs` table: `id INTEGER PRIMARY KEY AUTOINCREMENT, source_name TEXT, started_at TEXT, finished_at TEXT, status TEXT (ok/error), documents_found INTEGER, documents_new INTEGER, invoices_saved INTEGER, error_message TEXT`
- Add `save_source_run(data_dir, source_name, started_at, finished_at, status, documents_found, documents_new, invoices_saved, error_message)` and `get_recent_runs(data_dir, source_name=None, limit=20)` functions
- In `init_db()`, migrate `processed_emails` rows into `processed_documents` with `source_name='email'`, then drop `processed_emails`
- Replace `is_email_processed` / `mark_email_processed` with `is_document_processed(data_dir, source_name, source_id)` and `mark_document_processed(data_dir, source_name, source_id, sender, subject, received_at)`
- Add `source_name` and `source_document_id` columns to `invoices` table. Backfill existing rows with `source_name='email'`, `source_document_id=email_id`. `email_id` stays but becomes nullable.
- Update `save_invoice()` to accept `source_name` and `source_document_id`. `email_id` becomes optional.

**Patterns to follow:**
- Existing migration pattern in `init_db()`: `PRAGMA table_info` + `ALTER TABLE ADD COLUMN`
- `INSERT OR IGNORE` for idempotent dedup

**Test scenarios:**
- Happy path: `mark_document_processed` + `is_document_processed` returns True
- Happy path: same `source_id` with different `source_name` are independent
- Happy path: `save_source_run` stores a run record, `get_recent_runs` retrieves it
- Edge case: duplicate `(source_name, source_id)` is silently ignored
- Integration: migration copies `processed_emails` rows correctly
- Integration: `invoices` table gets new columns backfilled

**Verification:**
- All existing DB tests pass with updated function signatures. Migration is idempotent.

---

- [ ] **Unit 2: Move email logic into sources/email_source.py**

**Goal:** Extract the email fetch logic from `main.py` into a self-contained source file.

**Requirements:** R1, R6

**Dependencies:** Unit 1

**Files:**
- Create: `src/sources/__init__.py` (empty)
- Create: `src/sources/email_source.py`
- Modify: `src/pipeline.py` (update to use new DB functions)
- Test: `tests/test_sources_email.py`

**Approach:**
- `email_source.py` exports `run(config: dict, data_dir: str) -> None`
- Move the body of `main.py:poll_inbox()` into this function. It:
  - Reads `source_name` from config (the instance name, passed by caller)
  - Creates `GraphClient` using `config["client_id"]`
  - Fetches emails with config params (whitelisted_senders, subject_keywords, since_date, link_keywords)
  - For each email, checks `is_document_processed(data_dir, source_name, message_id)`
  - For each attachment, calls `process_attachment()` (from pipeline.py)
  - Marks document processed via `mark_document_processed()`
  - Tracks counts (found/new/saved) and calls `save_source_run()` at the end (including on error)
- `source_id` = `email.internet_message_id or email.email_id` (stable across folders)
- Update `pipeline.py:process_attachment()` to accept individual fields (`sender`, `received_at`, `source_id`) instead of the `Email` dataclass — or keep passing the email object internally. Whichever is simpler during implementation.

**Patterns to follow:**
- `main.py:poll_inbox()` — move, don't rewrite

**Test scenarios:**
- Happy path: `run()` fetches emails, classifies, uploads, marks processed
- Happy path: already-processed emails are skipped
- Edge case: empty inbox does nothing
- Edge case: attachment processing failure doesn't prevent marking other emails

**Verification:**
- `run()` produces identical behavior to the old `poll_inbox()`

---

- [ ] **Unit 3: Rewrite main.py with source auto-discovery + CLI entry point**

**Goal:** main.py schedules lightweight sources via APScheduler. A CLI entry point allows external triggers for heavy sources.

**Requirements:** R2, R3, R5, R7

**Dependencies:** Unit 1, Unit 2

**Files:**
- Modify: `src/main.py`
- Create: `src/sources/run.py` (CLI entry point)
- Modify: `config.yaml`
- Modify: `config.example.yaml`
- Modify: `tests/test_main.py`

**Approach:**
- **main.py source discovery**: For each instance `name` in `config["sources"]`:
  - Read `module` field (default: `name` itself)
  - `importlib.import_module(f"sources.{module}")` and grab its `run` function. Log and skip if not found.
  - If `interval_minutes` is present: register with `IntervalTrigger(minutes=N)` and `next_run_time=now`. Pass instance config + `source_name=name` to `run()`.
  - If `interval_minutes` is absent: log that the source is externally triggered, skip scheduling.
  - Wrap each source's `run()` in try/except for isolation (R5).
- **CLI entry point** (`src/sources/run.py`): `python -m sources.run <instance_name>` loads config, finds the instance, imports the module, calls `run()`. This is what systemd timers / cron call for heavy sources.
- Monthly report job unchanged.
- Remove old `poll_inbox()`. Remove direct `poller` import.
- Update `config.yaml` and `config.example.yaml` with the new `sources:` structure.

**Patterns to follow:**
- Existing APScheduler job registration in `main.py`
- Existing per-attachment try/except error handling

**Test scenarios:**
- Happy path: scheduler registers jobs only for sources with `interval_minutes`
- Happy path: two instances of the same module get separate jobs with separate source_names
- Happy path: CLI entry point loads and runs a source by instance name
- Error path: missing source module logs warning, skips, others still work
- Error path: source `run()` exception doesn't crash other sources
- Edge case: source with no new documents — nothing happens, no errors

**Verification:**
- Bot starts, email source runs on its interval.
- `python -m sources.run cegedim` runs the cegedim source once and exits.
- Adding a new source is: one .py file + one config entry + optionally a systemd timer.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| DB migration loses or duplicates data | Idempotent migration (`INSERT OR IGNORE`), verified by tests |
| `email_source.py` shadows stdlib `email` | Named `email_source.py` explicitly |
| `importlib` loads arbitrary code | Only imports modules listed in config `sources` keys |
| SQLite contention from concurrent sources | `PRAGMA busy_timeout=5000` + WAL mode |
| CLI entry point runs concurrently with bot | SQLite WAL + busy_timeout handles this; dedup prevents double-processing |

## Sources & References

- Related code: `src/main.py`, `src/poller.py`, `src/pipeline.py`, `src/db.py`
- Related tests: `tests/conftest.py`, `tests/test_pipeline.py`, `tests/test_db.py`, `tests/test_main.py`
