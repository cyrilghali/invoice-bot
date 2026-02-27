"""
Shared constants and utilities used across multiple modules.
"""

import logging
import logging.handlers
import os
import re
import sys

import yaml

logger = logging.getLogger(__name__)

# Microsoft Graph API base URL
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Default data directory (centralised to avoid repeating the fallback everywhere)
DEFAULT_DATA_DIR = "/app/data"

# French month names (1-indexed; index 0 is empty)
MONTH_NAMES_FR = [
    "", "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(data_dir: str = "/app/data", log_level: str = "INFO") -> None:
    """
    Configure the root logger with:
      - StreamHandler  → stdout (Docker console, existing behaviour)
      - RotatingFileHandler → data/bot.log (5 MB per file, 5 backups)

    Call this once at the very start of each entry point (main.py, backfill.py,
    auth_setup.py).  Subsequent calls are safe but have no effect because
    basicConfig / addHandler only add handlers when none exist yet.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    # Stdout handler — keeps existing Docker console output
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))

    # Rotating file handler — 5 MB × 5 files = up to 25 MB on disk
    os.makedirs(data_dir, exist_ok=True)
    log_path = os.path.join(data_dir, "bot.log")
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))

    root = logging.getLogger()
    # Only configure if no handlers are already attached (idempotent)
    if not root.handlers:
        root.setLevel(level)
        root.addHandler(stdout_handler)
        root.addHandler(file_handler)
    else:
        # Entry point called setup_logging after another basicConfig — add file handler if missing
        has_file = any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers)
        if not has_file:
            root.addHandler(file_handler)
        root.setLevel(level)

    logging.getLogger(__name__).debug(
        "Logging initialised — file: %s (level=%s)", log_path, log_level.upper()
    )


def load_config() -> dict:
    """Load YAML configuration and overlay secrets from environment variables.

    Environment variables take precedence over config.yaml values:
      AZURE_CLIENT_ID    -> microsoft.client_id
      ANTHROPIC_API_KEY  -> classifier.api_key
    """
    config_path = os.environ.get("CONFIG_PATH", "/app/config.yaml")
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        logger.info("Configuration loaded from %s", config_path)
    except FileNotFoundError:
        logger.error("config.yaml not found at %s", config_path)
        sys.exit(1)
    except yaml.YAMLError as e:
        logger.error("Invalid config.yaml: %s", e)
        sys.exit(1)

    # Overlay secrets from environment variables (take precedence over YAML)
    env_client_id = os.environ.get("AZURE_CLIENT_ID")
    if env_client_id:
        cfg.setdefault("microsoft", {})["client_id"] = env_client_id

    env_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if env_api_key:
        cfg.setdefault("classifier", {})["api_key"] = env_api_key

    return cfg


# ---------------------------------------------------------------------------
# Filename / label helpers (shared by onedrive_uploader and excel_exporter)
# ---------------------------------------------------------------------------

def normalize_content_type(ct: str) -> str:
    """Strip charset/boundary suffixes and normalise a MIME content-type string."""
    return ct.split(";")[0].strip().lower()


def sanitize_filename(name: str) -> str:
    """Remove characters that are problematic in filenames."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)


# Common TLD suffixes to strip when extracting the company name from a domain.
_COMPOUND_TLDS = {
    "co.uk", "co.jp", "co.nz", "co.za", "co.in", "co.kr",
    "com.au", "com.br", "com.fr", "com.mx", "com.ar",
    "org.uk", "net.au", "gov.uk",
}


def sender_to_label(sender: str) -> str:
    """
    Extract the company name from a sender email address.

    Strategy: take the second-level domain (just before the TLD), which is
    almost always the company name regardless of subdomains or compound TLDs.

    Examples:
        noreply@hotmail.com               -> hotmail
        factures@edf.fr                   -> edf
        billing@notifications.amazon.fr   -> amazon
        invoice@free.fr                   -> free
        support@company.co.uk             -> company
    """
    if "@" in sender:
        domain = sender.split("@")[-1].lower().strip()
    else:
        domain = sender.lower().strip()

    parts = domain.split(".")

    if len(parts) >= 3 and ".".join(parts[-2:]) in _COMPOUND_TLDS:
        company = parts[-3]
    elif len(parts) >= 2:
        company = parts[-2]
    else:
        company = parts[0]

    return sanitize_filename(company)
