# Invoice Bot

Automated invoice collection bot with pluggable sources. Each source (email inbox, web scraper, API) fetches documents and runs them through a shared classify → upload → DB pipeline. Generates monthly Excel summaries.

## Running

```bash
# Production — systemd user service (24/7, auto-restart)
systemctl --user start invoice-bot
systemctl --user status invoice-bot
journalctl --user -u invoice-bot -f   # live logs

# WhatsApp webhook receiver — separate service (push-based, not APScheduler)
systemctl --user start invoice-bot-whatsapp
journalctl --user -u invoice-bot-whatsapp -f

# Development — foreground
python3 src/main.py
uvicorn sources.whatsapp_webhook:app --app-dir src --port 8321  # whatsapp webhook

# Run a single source externally (for heavy scrapers via systemd timer/cron)
python3 -m sources.run <instance_name>
```

### First-time setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill config
cp config.example.yaml config.yaml
cp .env.example .env

# 3. Authenticate Microsoft account (one-time)
python3 src/auth_setup.py

# 4. Install systemd services
mkdir -p ~/.config/systemd/user
cp invoice-bot.service ~/.config/systemd/user/
cp invoice-bot-whatsapp.service ~/.config/systemd/user/  # if using WhatsApp
systemctl --user daemon-reload
systemctl --user enable --now invoice-bot
systemctl --user enable --now invoice-bot-whatsapp  # if using WhatsApp
loginctl enable-linger $USER   # survive reboots without login
```

## Testing

```bash
python3 -m pytest tests/
python3 -m pytest tests/ -v          # verbose
python3 -m pytest tests/test_classifier.py  # single file
```

## Architecture

```
APScheduler (BlockingScheduler)          [invoice-bot.service]
├── source auto-discovery from config.yaml
│   ├── sources/email_source.py  — polls inbox via Microsoft Graph API
│   ├── sources/<custom>.py      — any custom source (scraper, API, etc.)
│   │   Each source calls directly:
│   │   ├── classifier.py  → classifies via `claude -p` CLI (Opus model)
│   │   ├── pipeline.py    → routes attachments based on classification
│   │   ├── onedrive_uploader.py → uploads to OneDrive
│   │   └── db.py          → dedup + invoice tracking
│   └── sources/run.py     — CLI entry point for external triggers
└── send_report()  — 1st of each month
    ├── db.py            → queries unreported invoices
    └── excel_exporter.py → builds Excel summary

FastAPI + uvicorn                         [invoice-bot-whatsapp.service]
└── sources/whatsapp_webhook.py — push-based, receives Meta webhook POSTs
    └── reuses manual_source.run() → pipeline → OneDrive + DB
```

Sources with `interval_minutes` in config are scheduled by APScheduler.
Sources without it are triggered externally via `python -m sources.run <name>`.
Multiple instances of the same source module are supported (e.g. two email inboxes).

The WhatsApp webhook is **not** a `sources:` entry — it lives under a top-level
`whatsapp:` config block and runs as its own uvicorn process, so a webhook
crash never takes down the polling scheduler. See "WhatsApp Cloud API setup"
below for the one-time Meta dashboard walkthrough.

## Key files

- `src/main.py` — entry point, source auto-discovery, scheduler setup
- `src/sources/email_source.py` — email inbox source (Microsoft Graph)
- `src/sources/run.py` — CLI entry point for externally triggered sources
- `src/classifier.py` — Claude CLI classification (uses `claude -p --model opus`)
- `src/pipeline.py` — attachment classification and routing
- `src/poller.py` — Microsoft Graph API client and email dataclasses
- `src/db.py` — SQLite: dedup (processed_documents), invoices, source_runs
- `src/utils.py` — shared config loading, logging, path defaults
- `config.yaml` — application configuration (gitignored)
- `.env` — secrets: AZURE_CLIENT_ID (gitignored)
- `data/` — SQLite DB, MSAL token cache, log files (gitignored)

## Adding a new source

1. Create `src/sources/<name>.py` with a `run(config: dict, data_dir: str) -> None` function
2. Add a config entry under `sources:` in `config.yaml`
3. The source calls `is_invoice()`, `upload_attachment()`, `save_invoice()` directly
4. Use `db.is_document_processed()` / `db.mark_document_processed()` for dedup
5. Call `db.save_source_run()` at the end for observability

## WhatsApp Cloud API setup

One-time setup for the push-based `invoice-bot-whatsapp.service`:

1. **Meta Business** → business.facebook.com → create account.
2. **developers.facebook.com** → Create App → "Business" → add "WhatsApp" product.
3. **WhatsApp → API Setup** → add and verify phone number (warning: the number can no longer be used with regular WhatsApp once verified).
4. **Business Settings → Users → System Users** → create one → assign permissions `whatsapp_business_messaging` + `whatsapp_business_management` → generate permanent token.
5. Grab from the dashboard: `PHONE_NUMBER_ID`, `ACCESS_TOKEN`, `APP_SECRET` (App Settings → Basic).
6. Put secrets in `.env` (`WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN` — the last is arbitrary, you invent it).
7. Fill the `whatsapp:` block in `config.yaml` (`phone_number_id`, `allowed_senders`, `onedrive_folder_name`).
8. **Public URL** — easiest is Tailscale Funnel: `tailscale funnel --bg 8321` → gives you a `https://<host>.ts.net/` URL.
9. **Meta dashboard → WhatsApp → Configuration → Webhook** → paste `https://<host>.ts.net/webhook` + the verify token → subscribe to the `messages` field.
10. Start the service: `systemctl --user enable --now invoice-bot-whatsapp`.

Media URLs returned by the Graph API expire in ~5 minutes, so the webhook
downloads inline before handing off to `manual_source.run()`.

## Conventions

- Python 3.12, no type stubs needed
- Tests use pytest + pytest-mock, run from repo root
- Classification prompts are in French (user's invoices are French)
- The classifier does NOT use the Anthropic SDK — it shells out to `claude -p` using the user's Claude subscription
- All paths are configurable via env vars (CONFIG_PATH, DATA_DIR) with repo-relative defaults
- Source dedup uses `(source_name, source_id)` composite key in `processed_documents` table
- Each source run is logged to `source_runs` table for observability
