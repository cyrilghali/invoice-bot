"""
Shared constants and utilities used across multiple modules.
"""

import logging
import logging.handlers
import os
import sys

import yaml

logger = logging.getLogger(__name__)

# Microsoft Graph API base URL
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

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
