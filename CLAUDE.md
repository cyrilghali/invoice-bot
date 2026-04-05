# Invoice Bot

Automated invoice collection bot that polls an Outlook/Hotmail inbox, classifies attachments using Claude AI, uploads invoices to OneDrive organized by year/month, and generates monthly Excel summaries.

## Running

```bash
# Production — systemd user service (24/7, auto-restart)
systemctl --user start invoice-bot
systemctl --user status invoice-bot
journalctl --user -u invoice-bot -f   # live logs

# Development — foreground
python3 src/main.py
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

# 4. Install systemd service
mkdir -p ~/.config/systemd/user
cp invoice-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now invoice-bot
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
APScheduler (BlockingScheduler)
├── poll_inbox()  — every N minutes
│   ├── poller.py      → fetches emails via Microsoft Graph API
│   ├── classifier.py  → classifies via `claude -p` CLI (Opus model)
│   ├── pipeline.py    → routes attachments based on classification
│   └── onedrive_uploader.py → uploads to OneDrive
└── send_report()  — 1st of each month
    ├── db.py            → queries unreported invoices
    └── excel_exporter.py → builds Excel summary
```

## Key files

- `src/main.py` — entry point, scheduler setup
- `src/classifier.py` — Claude CLI classification (uses `claude -p --model opus --bare`)
- `src/pipeline.py` — attachment processing and routing
- `src/poller.py` — Microsoft Graph inbox polling
- `src/utils.py` — shared config loading, logging, path defaults
- `config.yaml` — application configuration (gitignored)
- `.env` — secrets: AZURE_CLIENT_ID (gitignored)
- `data/` — SQLite DB, MSAL token cache, log files (gitignored)

## Conventions

- Python 3.12, no type stubs needed
- Tests use pytest + pytest-mock, run from repo root
- Classification prompts are in French (user's invoices are French)
- The classifier does NOT use the Anthropic SDK — it shells out to `claude -p` using the user's Claude subscription
- All paths are configurable via env vars (CONFIG_PATH, DATA_DIR) with repo-relative defaults
