"""
Telegram bot — long-polling receiver that funnels incoming documents/photos
through the invoice pipeline.

Pure stdlib + `requests` — no python-telegram-bot dependency, no webhook, no
public URL. Runs as its own long-lived process via invoice-bot-telegram.service
so a bot crash can't take down the scheduler that handles email polling.

Config (top-level `telegram:` block in config.yaml):
    telegram:
      allowed_senders: [123456789]      # Telegram numeric user IDs
      onedrive_folder_name: "Factures-GHALI"
      onedrive_account: "colisee.ghali@hotmail.com"
      default_sender: "telegram"
      max_file_size_mb: 20               # optional, Telegram Bot API caps ~20MB

Secrets (.env):
    TELEGRAM_BOT_TOKEN=123456:ABC-DEF...

Run manually:
    python3 -m sources.telegram_bot
"""

import logging
import mimetypes
import os
import shutil
import sys
import tempfile
import time
from typing import Any

import requests

import db
from sources import manual_source
from utils import DEFAULT_DATA_DIR, load_config, sanitize_filename, setup_logging

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
LONG_POLL_TIMEOUT = 30  # seconds; getUpdates blocks until a message arrives or timeout


def _token() -> str:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not tok:
        raise RuntimeError("TELEGRAM_BOT_TOKEN env var is required")
    return tok


def _api_url(token: str, method: str) -> str:
    return f"{API_BASE}/bot{token}/{method}"


def _data_dir() -> str:
    return os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)


def _get_updates(token: str, offset: int | None) -> list[dict]:
    params = {"timeout": LONG_POLL_TIMEOUT, "allowed_updates": '["message"]'}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(
        _api_url(token, "getUpdates"),
        params=params,
        timeout=LONG_POLL_TIMEOUT + 10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        logger.error("getUpdates not ok: %s", data)
        return []
    return data.get("result") or []


def _send_text(token: str, chat_id: int, text: str) -> None:
    try:
        requests.post(
            _api_url(token, "sendMessage"),
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except Exception as e:
        logger.warning("sendMessage failed for chat %s: %s", chat_id, e)


def _download_file(token: str, file_id: str, suggested_name: str, data_dir: str) -> str:
    """Fetch file metadata, then download bytes to a per-message temp dir."""
    meta = requests.get(_api_url(token, "getFile"), params={"file_id": file_id}, timeout=30)
    meta.raise_for_status()
    payload = meta.json()
    if not payload.get("ok"):
        raise RuntimeError(f"getFile failed: {payload}")
    file_path = (payload.get("result") or {}).get("file_path")
    if not file_path:
        raise RuntimeError("getFile missing file_path")

    url = f"{API_BASE}/file/bot{token}/{file_path}"
    dl = requests.get(url, timeout=120)
    dl.raise_for_status()

    tmp_dir = tempfile.mkdtemp(prefix="tg_", dir=os.path.join(data_dir, "tmp") if os.path.isdir(os.path.join(data_dir, "tmp")) else None)
    try:
        safe = sanitize_filename(suggested_name) or f"{file_id}.bin"
        if "." not in safe:
            # Guess extension from the remote path so the classifier gets a hint.
            ext = os.path.splitext(file_path)[1]
            if ext:
                safe += ext
        target = os.path.join(tmp_dir, safe)
        with open(target, "wb") as f:
            f.write(dl.content)
        return target
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def _extract_media(message: dict) -> tuple[str, str] | None:
    """Return (file_id, suggested_filename) for a supported attachment, or None."""
    doc = message.get("document")
    if doc and doc.get("file_id"):
        return doc["file_id"], doc.get("file_name") or f"{doc['file_id']}.bin"

    photos = message.get("photo") or []
    if photos:
        # Telegram sends multiple sizes; pick the largest.
        best = max(photos, key=lambda p: p.get("file_size") or 0)
        return best["file_id"], f"{best['file_id']}.jpg"

    return None


def _run_pipeline(filepath: str, sender_label: str, cfg: dict, data_dir: str) -> tuple[bool, str]:
    tg = cfg.get("telegram") or {}
    client_id = (
        tg.get("client_id")
        or (cfg.get("microsoft") or {}).get("client_id")
        or os.environ.get("AZURE_CLIENT_ID")
    )
    root_folder_name = tg.get("onedrive_folder_name") or (cfg.get("onedrive") or {}).get("folder_name")

    if not client_id or not root_folder_name:
        return False, "missing client_id or onedrive folder in config"

    instance_config = {
        "source_name": "telegram",
        "client_id": client_id,
        "onedrive_folder_name": root_folder_name,
        "onedrive_account": tg.get("onedrive_account"),
        "default_sender": tg.get("default_sender", "telegram"),
        "_files": [filepath],
        "_sender": sender_label,
        "invoices": cfg.get("invoices"),
        "classifier": cfg.get("classifier"),
    }

    try:
        manual_source.run(instance_config, data_dir)
    except Exception as e:
        logger.error("Pipeline crashed: %s", e, exc_info=True)
        return False, str(e)
    return True, "ok"


def _handle_message(message: dict, token: str, cfg: dict, data_dir: str) -> None:
    tg = cfg.get("telegram") or {}
    allowed = set(tg.get("allowed_senders") or [])
    frm = message.get("from") or {}
    chat = message.get("chat") or {}
    user_id = frm.get("id")
    chat_id = chat.get("id")
    if chat_id is None:
        return

    if allowed and user_id not in allowed:
        logger.warning("Rejected message from non-allowlisted user: %s (%s)", user_id, frm.get("username"))
        _send_text(token, chat_id, "❌ Non autorisé.")
        return

    media = _extract_media(message)
    if not media:
        logger.info("Ignoring non-media message from %s type=%s", user_id, message.get("text", "")[:40])
        return

    file_id, suggested = media
    try:
        filepath = _download_file(token, file_id, suggested, data_dir)
    except Exception as e:
        logger.error("Download failed: %s", e)
        _send_text(token, chat_id, f"❌ Erreur téléchargement: {e}")
        return

    sender_label = frm.get("username") or frm.get("first_name") or f"telegram:{user_id}"
    logger.info("Processing %s from %s (%s)", os.path.basename(filepath), user_id, sender_label)

    ok, reason = _run_pipeline(filepath, sender_label, cfg, data_dir)
    _send_text(token, chat_id, "Reçu ✅" if ok else f"❌ Erreur: {reason}")

    try:
        os.unlink(filepath)
        os.rmdir(os.path.dirname(filepath))
    except OSError:
        pass


def main() -> int:
    data_dir = _data_dir()
    os.makedirs(data_dir, exist_ok=True)

    cfg = load_config(exit_on_error=False)
    log_level = (cfg.get("logging") or {}).get("log_level", "INFO")
    setup_logging(data_dir=data_dir, log_level=log_level)
    db.init_db(data_dir)

    token = _token()
    logger.info("Telegram bot starting — long-polling getUpdates")

    offset: int | None = None
    backoff = 1
    while True:
        try:
            updates = _get_updates(token, offset)
            backoff = 1
        except requests.exceptions.ReadTimeout:
            # Normal for long polling when no messages arrive.
            continue
        except Exception as e:
            logger.error("getUpdates failed: %s (backoff %ds)", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        # Reload config each batch so edits take effect without restart.
        cfg = load_config(exit_on_error=False)

        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message")
            if not msg:
                continue
            try:
                _handle_message(msg, token, cfg, data_dir)
            except Exception as e:
                logger.error("Handler crashed on update %s: %s", update.get("update_id"), e, exc_info=True)


if __name__ == "__main__":
    sys.exit(main() or 0)
