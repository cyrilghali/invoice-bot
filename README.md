# Invoice Bot

Automatically collects invoices from a personal Outlook/Hotmail inbox, stores them
in OneDrive organized by year/month, and sends a monthly summary to the accountant.

**Your father does nothing. The bot runs fully automatically.**

---

## How it works

1. Every hour: polls the Outlook inbox via Microsoft Graph API
2. Filters emails by subject keywords and an optional sender whitelist
3. Uses an AI classifier (Claude) to confirm attachments are actual invoices
4. Downloads PDF/image attachments
5. Uploads them to OneDrive under `<folder>/YYYY/MM/`
6. On the 1st of each month at 8:00 UTC: sends an email summary to the accountant from your father's own Outlook account

---

## Prerequisites

- Docker + Docker Compose installed on your NAS
- Your father's personal Outlook/Hotmail account credentials (he logs in once via browser)
- An Azure App Registration (free, takes 5 minutes — see below)
- An Anthropic API key (for the invoice classifier)

---

## Step 1: Azure App Registration

1. Go to [portal.azure.com](https://portal.azure.com) (sign in with any Microsoft account — yours is fine)
2. Search **"App registrations"** → click **New registration**
3. Fill in:
   - **Name:** `invoice-bot`
   - **Supported account types:** select **"Personal Microsoft accounts only"**
   - **Redirect URI:** leave blank
4. Click **Register**
5. Copy the **Application (client) ID** — you will need it in `config.yaml`

### Add API Permissions

In your app → **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated permissions**, add:

| Permission | Why |
|---|---|
| `Mail.Read` | Read inbox to find invoices |
| `Mail.Send` | Send monthly report from father's account |
| `Files.ReadWrite` | Upload invoices to OneDrive |
| `offline_access` | Keep him logged in permanently |

Click **Grant admin consent** (or it will be granted on first login).

### Enable Public Client Flow

**Authentication** → scroll to **Advanced settings** → toggle **"Allow public client flows"** → **Yes** → **Save**

---

## Step 2: Configure the bot

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` and fill in:

```yaml
microsoft:
  client_id: "paste-your-azure-client-id-here"

onedrive:
  folder_name: "Factures"  # Root folder created automatically in OneDrive

invoices:
  whitelisted_senders:        # Optional — omit to scan all emails
    - "factures@supplier1.fr"
    - "billing@supplier2.com"

accountant:
  email: "comptable@cabinet.fr"

classifier:
  api_key: "paste-your-anthropic-api-key-here"
```

---

## Step 3: Deploy on your NAS

```bash
# Clone or copy the project to your NAS
cd invoice-bot

# Build the Docker image
docker compose build

# Start the container
docker compose up -d
```

---

## Step 4: First-time Microsoft authentication (one-time only)

Your father needs to log in **once**. Run:

```bash
docker exec -it invoice-bot python src/auth_setup.py
```

You will see something like:

```
============================================================
ACTION REQUIRED - Microsoft Account Login
============================================================
To sign in, use a web browser to open https://microsoft.com/devicelogin
and enter the code ABCD-EFGH to authenticate.
============================================================
```

1. Open that URL on any browser (phone, tablet, laptop)
2. Enter the code shown
3. Sign in with **your father's Outlook/Hotmail account**
4. Done — the token is saved to `data/ms_token_cache.json`

The bot will silently refresh the token automatically. You only need to repeat
this step if the refresh token expires (after 90 days of zero activity).

---

## Managing the bot

```bash
# View live logs
docker compose logs -f

# Stop the bot
docker compose down

# Restart after a config change
docker compose restart

# Rebuild after code changes
docker compose build && docker compose up -d
```

---

## Adding a new invoice supplier

Edit `config.yaml`, add the supplier's email to `whitelisted_senders`, then restart:

```bash
docker compose restart
```

No rebuild needed — config is read on startup.

---

## File structure in OneDrive

```
Factures/
├── 2026/
│   ├── 01/
│   │   └── 2026-01-15_supplier1_invoice-123.pdf
│   └── 02/
│       ├── 2026-02-03_supplier2_receipt.pdf
│       └── 2026-02-19_supplier1_facture-456.pdf
└── 2027/
    └── ...
```

---

## Monthly report email

Sent on the 1st of each month at 08:00 UTC **from your father's own Outlook account** to the accountant. Contains:

- A table: date received / sender / filename / OneDrive link
- A link to the OneDrive month folder
- PDFs attached directly if total size < 20 MB, otherwise OneDrive links only

---

## Project structure

```
invoice-bot/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── config.yaml                  # Your config (gitignored)
├── config.example.yaml          # Template
├── .gitignore
├── data/                        # Auto-created, mounted as volume
│   ├── invoices.db              # SQLite database
│   └── ms_token_cache.json      # Microsoft auth token
└── src/
    ├── main.py                  # Entry point + scheduler
    ├── auth_setup.py            # One-time Microsoft login
    ├── poller.py                # Graph API inbox polling
    ├── detector.py              # Sender whitelist filter
    ├── classifier.py            # AI invoice classifier
    ├── onedrive_uploader.py     # OneDrive upload via Graph API
    ├── reporter.py              # Monthly report builder + sender
    ├── notifier.py              # Telegram notifications
    ├── excel_exporter.py        # Excel export helper
    ├── backfill.py              # Backfill historical invoices
    └── db.py                    # SQLite operations
```
