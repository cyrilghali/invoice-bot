---
title: "feat: Pluggable invoice sources"
type: feat
status: active
date: 2026-04-05
---

# feat: Pluggable invoice sources

## Overview

Transform the bot from a single-source email poller into a multi-source system where each supplier can define its own way of fetching invoices (email, web scraping, API, etc.). All sources feed into the same classify → upload → DB pipeline.

## Problem Frame

The bot is hardcoded to fetch invoices from one email inbox. Some suppliers have web portals, APIs, or other mechanisms that require scraping or custom integrations. The architecture should support adding new sources by dropping a Python file into a folder and adding config — no framework, no base classes.

## Requirements Trace

- R1. Each source is a Python file in `sources/` exporting a `fetch(config) -> list[Attachment]` function
- R2. Each source has its own cron schedule defined in config.yaml
- R3. Each source has its own dedup tracking (don't reprocess already-fetched files)
- R4. The shared pipeline (classify → upload → DB → Excel) is untouched
- R5. The existing email source works exactly as before, just moved to `sources/email.py`
- R6. Source failures are isolated — one crashing source doesn't block others

## Scope Boundaries

- Not building any new sources yet (Cegedim, Illy, etc.) — just the plugin system and moving email into it
- Not changing classifier, uploader, DB, or Excel exporter
- Not changing the Attachment or Email dataclasses

## Key Technical Decisions

- **Convention-based plugins**: Each source is a `.py` file in `sources/` that exports `fetch(config: dict) -> list[SourceDocument]`. No base class, no registry, no decorators. `main.py` auto-discovers files in the folder.
- **SourceDocument instead of raw Attachment**: Sources return a lightweight wrapper that carries the attachment plus metadata the pipeline needs (sender, subject, received_at, source_name). This decouples sources from the Email dataclass which is email-specific.
- **Per-source dedup via source_id**: Each source document includes a `source_id` — a unique string the source generates (email_id for email, URL+date for scrapers, etc.). The DB tracks `(source_name, source_id)` pairs to prevent reprocessing.
- **Cron schedules in config**: Each source entry in config.yaml has a `schedule` field using cron syntax. The scheduler creates one job per source.
- **Auto-discovery**: `main.py` reads `sources` keys from config.yaml, imports the matching `.py` file from `sources/`, and registers a scheduled job for each.

## High-Level Technical Design

> *Directional guidance, not implementation specification.*

```
config.yaml                    sources/                    main.py
┌──────────────┐    imports    ┌──────────────┐    calls   ┌──────────────┐
│ sources:     │───────────────│ email.py     │◄───────────│ for each     │
│   email:     │               │   fetch()    │            │ source in    │
│     schedule │               ├──────────────┤            │ config:      │
│   cegedim:   │───────────────│ cegedim.py   │◄───────────│   schedule   │
│     schedule │               │   fetch()    │            │   fetch()    │
└──────────────┘               └──────┬───────┘            └──────┬───────┘
                                      │                           │
                                      │ list[SourceDocument]      │
                                      ▼                           ▼
                               ┌──────────────────────────────────┐
                               │ pipeline.py (unchanged)          │
                               │ classify → upload → DB           │
                               └──────────────────────────────────┘
```

## Implementation Units

- [ ] **Unit 1: Create SourceDocument dataclass and update pipeline**

**Goal:** Introduce a source-agnostic document type that any source can produce, and make the pipeline accept it.

**Requirements:** R1, R4

**Dependencies:** None

**Files:**
- Create: `src/source_types.py`
- Modify: `src/pipeline.py`
- Test: `tests/test_pipeline.py`

**Approach:**
- Define `SourceDocument` dataclass: `source_name`, `source_id`, `sender`, `subject`, `received_at`, `attachments: list[Attachment]`
- Update `process_attachment` to accept a `SourceDocument` instead of an `Email` — the fields it actually uses (sender, subject, received_at) are the same
- Keep the existing `Email` dataclass in poller.py — the email source will convert Email → SourceDocument

**Test scenarios:**
- Happy path: `process_attachment` with a SourceDocument produces the same result as with an Email
- Edge case: SourceDocument with empty sender/subject handled gracefully

**Verification:**
- All existing pipeline tests pass with minimal changes (mock Email replaced by SourceDocument)

- [ ] **Unit 2: Add per-source dedup to DB**

**Goal:** Track processed documents by `(source_name, source_id)` instead of just `email_id`

**Requirements:** R3

**Dependencies:** Unit 1

**Files:**
- Modify: `src/db.py`
- Test: `tests/test_db.py`

**Approach:**
- Add `processed_documents` table: `source_name TEXT, source_id TEXT, processed_at TEXT, sender TEXT, subject TEXT, received_at TEXT, PRIMARY KEY (source_name, source_id)`
- Add `is_document_processed(data_dir, source_name, source_id)` and `mark_document_processed(...)` functions
- Keep old `processed_emails` table and functions for backwards compatibility during migration — new code uses the new table
- Migrate existing `processed_emails` rows into `processed_documents` with `source_name='email'`

**Test scenarios:**
- Happy path: mark document processed, check returns True
- Happy path: different source_name + same source_id are independent
- Edge case: duplicate insert is ignored (INSERT OR IGNORE)
- Integration: migration copies existing processed_emails rows

**Verification:**
- Existing DB tests pass, new dedup tests pass

- [ ] **Unit 3: Move email poller into sources/email.py**

**Goal:** Extract the email polling logic into the plugin format

**Requirements:** R1, R5

**Dependencies:** Unit 1, Unit 2

**Files:**
- Create: `src/sources/__init__.py`
- Create: `src/sources/email.py`
- Modify: `src/poller.py` (keep as-is for GraphClient, remove from main.py direct usage)
- Test: `tests/test_sources_email.py`

**Approach:**
- `sources/email.py` exports `fetch(config: dict) -> list[SourceDocument]`
- Internally uses `GraphClient.fetch_emails_with_attachments()` (unchanged)
- Converts each `Email` to a `SourceDocument` with `source_name="email"`, `source_id=email.internet_message_id or email.email_id`
- The email-specific config (whitelisted_senders, subject_keywords, since_date, link_detection) moves under `sources.email` in config.yaml

**Test scenarios:**
- Happy path: fetch() returns SourceDocuments from mocked GraphClient
- Happy path: source_id uses internet_message_id when available
- Edge case: empty inbox returns empty list

**Verification:**
- Email source produces identical results to the old direct-poll flow

- [ ] **Unit 4: Rewrite main.py with auto-discovery scheduler**

**Goal:** main.py dynamically loads sources from config and schedules each one

**Requirements:** R2, R6

**Dependencies:** Unit 2, Unit 3

**Files:**
- Modify: `src/main.py`
- Test: `tests/test_main.py`

**Approach:**
- Read `sources` dict from config.yaml
- For each source name, `importlib.import_module(f"sources.{name}")` and get its `fetch` function
- Parse `schedule` field as cron expression, register with APScheduler's `CronTrigger`
- The poll loop for each source: call `fetch()`, skip already-processed (via new DB functions), run pipeline for each new document
- Wrap each source's poll in try/except so one source crashing doesn't affect others
- Monthly report job stays unchanged

**Test scenarios:**
- Happy path: scheduler registers one job per source in config
- Error path: source with missing .py file logs warning and skips
- Error path: source fetch() raising exception doesn't crash other sources
- Integration: end-to-end with mocked source → pipeline → DB

**Verification:**
- Bot starts with multiple sources configured, each runs on its own schedule

## System-Wide Impact

- **Interaction graph:** main.py no longer imports poller directly — it goes through sources/email.py. Pipeline accepts SourceDocument instead of Email.
- **Error propagation:** Each source is wrapped in try/except. One source failing doesn't block others or crash the scheduler.
- **Unchanged invariants:** classifier.py, onedrive_uploader.py, excel_exporter.py are completely untouched. The Attachment dataclass is unchanged.
- **Config migration:** The email-specific config moves under `sources.email` in config.yaml. Old top-level keys should still work during transition.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Config format change breaks existing setup | Support both old and new config format during transition; log deprecation warning for old format |
| Auto-import of arbitrary .py files | Only import modules listed in config.yaml `sources` keys — not everything in the folder |
| Per-source dedup migration | Migrate existing processed_emails rows automatically on first run |

## Sources & References

- Related code: `src/main.py`, `src/poller.py`, `src/pipeline.py`, `src/db.py`
