"""
WhatsApp Cloud API webhook — receives messages pushed by Meta and feeds
document/image attachments through the invoice pipeline.

Unlike the pull-based sources, this is a long-lived FastAPI app driven by
incoming HTTPS POSTs from Meta. Run it with:

    uvicorn sources.whatsapp_webhook:app --app-dir src --host 0.0.0.0 --port 8321

or via the invoice-bot-whatsapp.service systemd unit.

Config lives under a top-level `whatsapp:` block in config.yaml (not under
`sources:`, since main.py only schedules sources with `interval_minutes`).
Secrets come from environment variables:

    WHATSAPP_ACCESS_TOKEN   — permanent System User token from Meta
    WHATSAPP_VERIFY_TOKEN   — arbitrary string, echoed during Meta's GET challenge
    WHATSAPP_APP_SECRET     — used to verify X-Hub-Signature-256 on incoming POSTs

Media URLs returned by Graph expire in ~5 minutes, so downloads happen inline.
Allowlisted sender phone numbers are enforced before any network work.
"""

import hashlib
import hmac
import json
import logging
import os
import shutil
import tempfile
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI, Header, HTTPException, Query, Request

import db
from sources import manual_source
from utils import DEFAULT_DATA_DIR, load_config, sanitize_filename, setup_logging

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.facebook.com/v21.0"
SUPPORTED_MESSAGE_TYPES = ("document", "image")


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir = _data_dir()
    os.makedirs(data_dir, exist_ok=True)
    cfg = _get_config()
    log_level = (cfg.get("logging") or {}).get("log_level", "INFO")
    setup_logging(data_dir=data_dir, log_level=log_level)
    db.init_db(data_dir)
    logger.info("WhatsApp webhook ready — POST /webhook")
    yield


app = FastAPI(title="invoice-bot whatsapp webhook", lifespan=lifespan)


def _data_dir() -> str:
    return os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)


def _get_config() -> dict:
    """Reload config on every call so edits take effect without restart."""
    try:
        return load_config(exit_on_error=False)
    except Exception as e:
        logger.error("Failed to load config: %s", e)
        return {}


def _whatsapp_config(cfg: dict | None = None) -> dict:
    return (cfg or _get_config()).get("whatsapp") or {}


def _verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Verify Meta's X-Hub-Signature-256 HMAC using the app secret."""
    app_secret = os.environ.get("WHATSAPP_APP_SECRET")
    if not app_secret:
        logger.error("WHATSAPP_APP_SECRET not set — refusing to process webhook")
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/webhook")
def verify(
    mode: str | None = Query(None, alias="hub.mode"),
    token: str | None = Query(None, alias="hub.verify_token"),
    challenge: str | None = Query(None, alias="hub.challenge"),
):
    """Meta calls this once when you register the webhook URL."""
    expected = os.environ.get("WHATSAPP_VERIFY_TOKEN")
    if mode == "subscribe" and expected and token == expected and challenge is not None:
        logger.info("Webhook verified by Meta")
        return int(challenge) if challenge.isdigit() else challenge
    logger.warning("Webhook verification failed: mode=%s", mode)
    raise HTTPException(status_code=403, detail="verification failed")


@app.post("/webhook")
async def receive(
    request: Request,
    x_hub_signature_256: str | None = Header(None),
):
    """Receive messages from Meta. Always returns 200 unless signature fails —
    Meta retries non-2xx responses aggressively and we don't want storms on
    our own bugs."""
    raw = await request.body()

    if not _verify_signature(raw, x_hub_signature_256):
        logger.warning("Webhook signature verification failed")
        raise HTTPException(status_code=403, detail="invalid signature")

    try:
        payload = json.loads(raw)
        _handle_payload(payload)
    except Exception as e:
        logger.error("Webhook handler crashed: %s", e, exc_info=True)

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Payload processing
# ---------------------------------------------------------------------------


def _handle_payload(payload: dict) -> None:
    """Extract messages from the Meta envelope and dispatch each."""
    cfg = _get_config()
    wa = _whatsapp_config(cfg)
    access_token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    if not access_token:
        logger.error("WHATSAPP_ACCESS_TOKEN not set — cannot download media")
        return

    allowed = set(wa.get("allowed_senders") or [])

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            contacts = {
                c["wa_id"]: (c.get("profile") or {}).get("name")
                for c in (value.get("contacts") or [])
                if c.get("wa_id")
            }
            for message in value.get("messages") or []:
                _handle_message(message, contacts, allowed, access_token, cfg)


def _handle_message(
    message: dict,
    contacts: dict,
    allowed_senders: set,
    access_token: str,
    cfg: dict,
) -> None:
    from_number = message.get("from", "")
    msg_type = message.get("type")

    if allowed_senders and from_number not in allowed_senders:
        logger.warning("Rejected message from non-allowlisted sender: %s", from_number)
        return

    if msg_type not in SUPPORTED_MESSAGE_TYPES:
        logger.info("Ignoring %s message from %s", msg_type, from_number)
        return

    media = message.get(msg_type) or {}
    media_id = media.get("id")
    if not media_id:
        logger.warning("Message of type %s has no media id", msg_type)
        return

    # Document messages carry a filename; image messages don't.
    filename = media.get("filename") or f"{msg_type}-{media_id}.jpg"

    try:
        filepath = _download_media(media_id, filename, access_token)
    except Exception as e:
        logger.error("Failed to download media %s: %s", media_id, e)
        _send_text(from_number, f"❌ Erreur de téléchargement: {e}", access_token)
        return

    sender_label = contacts.get(from_number) or f"whatsapp:{from_number}"

    try:
        _process_through_manual_source(filepath, sender_label, cfg)
        _send_text(from_number, "Reçu ✅", access_token)
    except Exception as e:
        logger.error("Pipeline failed for %s: %s", filename, e, exc_info=True)
        _send_text(from_number, f"❌ Erreur: {e}", access_token)
    finally:
        try:
            os.unlink(filepath)
            os.rmdir(os.path.dirname(filepath))
        except OSError:
            pass


def _download_media(media_id: str, filename: str, access_token: str) -> str:
    """Two-step Graph download: fetch short-lived URL, then the bytes."""
    headers = {"Authorization": f"Bearer {access_token}"}

    meta_resp = requests.get(f"{GRAPH_API}/{media_id}", headers=headers, timeout=30)
    meta_resp.raise_for_status()
    media_url = (meta_resp.json() or {}).get("url")
    if not media_url:
        raise ValueError("media URL missing in Graph response")

    dl_resp = requests.get(media_url, headers=headers, timeout=60)
    dl_resp.raise_for_status()

    tmp_dir = tempfile.mkdtemp(prefix="wa_")
    try:
        safe_name = sanitize_filename(filename) or f"{media_id}.bin"
        filepath = os.path.join(tmp_dir, safe_name)
        with open(filepath, "wb") as f:
            f.write(dl_resp.content)
        return filepath
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def _process_through_manual_source(filepath: str, sender_label: str, cfg: dict) -> None:
    """Reuse manual_source's pipeline+dedup+DB logging by passing it a config dict."""
    wa = _whatsapp_config(cfg)
    client_id = (
        wa.get("client_id")
        or (cfg.get("microsoft") or {}).get("client_id")
        or os.environ.get("AZURE_CLIENT_ID")
    )
    root_folder_name = wa.get("onedrive_folder_name") or (cfg.get("onedrive") or {}).get("folder_name")

    instance_config = {
        "source_name": "whatsapp",
        "client_id": client_id,
        "onedrive_folder_name": root_folder_name,
        "onedrive_account": wa.get("onedrive_account"),
        "default_sender": wa.get("default_sender", "whatsapp"),
        "_files": [filepath],
        "_sender": sender_label,
        "invoices": cfg.get("invoices"),
        "classifier": cfg.get("classifier"),
    }

    manual_source.run(instance_config, _data_dir())


def _send_text(to_number: str, text: str, access_token: str) -> None:
    """Send a text reply via WhatsApp Cloud API. Best-effort — failures are logged."""
    wa = _whatsapp_config()
    phone_number_id = wa.get("phone_number_id")
    if not phone_number_id:
        logger.warning("whatsapp.phone_number_id not set, cannot send reply")
        return
    try:
        resp = requests.post(
            f"{GRAPH_API}/{phone_number_id}/messages",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "to": to_number,
                "type": "text",
                "text": {"body": text},
            },
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.warning("Failed to send WhatsApp reply to %s: %s", to_number, e)


