# Invoice Bot

Automatically collects invoices from a personal Outlook/Hotmail inbox, classifies
them with AI, stores them in OneDrive organized by year/month, and generates a
monthly Excel summary.

**Fully automatic. Once set up, no manual intervention is needed.**

---

## How it works

1. Every N minutes: polls the Outlook inbox via Microsoft Graph API
2. Filters emails by subject keywords and an optional sender whitelist
3. Uses an AI classifier (Claude) to confirm attachments are actual invoices
4. Downloads PDF/image attachments
5. Uploads them to OneDrive under `<folder>/YYYY/MM/`
6. On the 1st of each month: generates an Excel summary and uploads it to OneDrive

---

## Prerequisites

- Docker + Docker Compose installed on your server or NAS
- A personal Outlook/Hotmail account (the account owner logs in once via browser)
- An Azure App Registration (free, takes 5 minutes — see below)
- An Anthropic API key (for the invoice classifier)

---

## Step 1: Azure App Registration

1. Go to [portal.azure.com](https://portal.azure.com) (sign in with any Microsoft account)
2. Search **"App registrations"** -> click **New registration**
3. Fill in:
   - **Name:** `invoice-bot`
   - **Supported account types:** select **"Personal Microsoft accounts only"**
   - **Redirect URI:** leave blank
4. Click **Register**
5. Copy the **Application (client) ID** — you will need it in `.env`

### Add API Permissions

In your app -> **API permissions** -> **Add a permission** -> **Microsoft Graph** -> **Delegated permissions**, add:

| Permission | Why |
|---|---|
| `Mail.Read` | Read inbox to find invoices |
| `Files.ReadWrite` | Upload invoices to OneDrive |
| `offline_access` | Keep the session alive permanently |

Click **Grant admin consent** (or it will be granted on first login).

### Enable Public Client Flow

**Authentication** -> scroll to **Advanced settings** -> toggle **"Allow public client flows"** -> **Yes** -> **Save**

---

## Step 2: Configure the bot

**Secrets** (`.env`):

```bash
cp .env.example .env
```

Edit `.env` and fill in your API keys:

```
AZURE_CLIENT_ID=paste-your-azure-client-id-here
ANTHROPIC_API_KEY=paste-your-anthropic-api-key-here
```

**Settings** (`config.yaml`):

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` and fill in:

```yaml
onedrive:
  folder_name: "Invoices"  # Root folder created automatically in OneDrive

invoices:
  whitelisted_senders:        # Optional — omit to scan all emails
    - "factures@supplier1.fr"
    - "billing@supplier2.com"
```

See `config.example.yaml` for all available options.

---

## Step 3: Deploy

```bash
# Clone or copy the project to your server
cd invoice-bot

# Build the Docker image
docker compose build

# Start the container
docker compose up -d
```

---

## Step 4: First-time Microsoft authentication (one-time only)

The account owner needs to log in **once**. Run:

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
3. Sign in with the **Outlook/Hotmail account** you want to monitor
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
Invoices/
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

## Project structure

```
invoice-bot/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env                         # Your secrets (gitignored)
├── .env.example                 # Secrets template
├── config.yaml                  # Your settings (gitignored)
├── config.example.yaml          # Settings template
├── LICENSE
├── .gitignore
├── data/                        # Auto-created, mounted as volume
│   ├── invoices.db              # SQLite database
│   └── ms_token_cache.json      # Microsoft auth token
└── src/
    ├── main.py                  # Entry point + scheduler
    ├── auth_setup.py            # One-time Microsoft login
    ├── poller.py                # Graph API inbox polling
    ├── classifier.py            # AI invoice classifier (Claude)
    ├── pipeline.py              # Processing orchestration
    ├── onedrive_uploader.py     # OneDrive upload via Graph API
    ├── excel_exporter.py        # Monthly Excel summary builder
    ├── db.py                    # SQLite operations
    └── utils.py                 # Shared config & logging
```

## License

MIT — see [LICENSE](LICENSE).
