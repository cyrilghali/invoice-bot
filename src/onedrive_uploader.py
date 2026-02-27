"""
OneDrive uploader via Microsoft Graph API.

Uses the same MSAL token as the mail poller (delegated auth, Files.ReadWrite scope)
to upload invoice attachments to OneDrive, organized in folders: ROOT/YYYY/MM/<supplier>/

File naming convention:
    YYYY-MM-DD_company_original-filename.pdf

The date is the invoice date extracted from the document (falls back to received date).
The supplier folder is the AI-extracted supplier name (e.g. 'metro-france', 'engie'),
falling back to the second-level domain of the sender (e.g. 'amazon', 'edf', 'free').
Uncertain/rejected files go to ROOT/YYYY/MM/_a_verifier/ (flat, no supplier subfolder).
"""

import logging
import re
import unicodedata
from datetime import datetime

import requests

from auth_setup import get_access_token
from utils import GRAPH_BASE, sanitize_filename, sender_to_label

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

def _supplier_to_label(supplier: str) -> str:
    """
    Convert a free-text supplier name to a compact filename-safe label.

    Examples:
        "EDF Électricité de France"  -> "edf-electricite-de-france"
        "Free SAS"                   -> "free-sas"
        "Orange S.A."                -> "orange-sa"
    """
    nfkd = unicodedata.normalize("NFKD", supplier)
    ascii_str = nfkd.encode("ascii", "ignore").decode("ascii")
    label = re.sub(r"[^a-z0-9]+", "-", ascii_str.lower()).strip("-")
    return label[:40]


def build_filename(
    received_at: str,
    sender: str,
    original_name: str,
    invoice_date: str | None = None,
    supplier: str | None = None,
) -> str:
    """
    Build a clean, sortable filename:
        YYYY-MM-DD_company_original-name.ext

    The date prefix uses invoice_date (YYYY-MM-DD) when available, falling
    back to received_at. The company label is:
      - supplier (from document) when provided — used for internal senders
      - otherwise the second-level domain of the sender address
        (e.g. billing@notifications.amazon.fr -> amazon)
    """
    if invoice_date:
        date_str = invoice_date
    else:
        try:
            dt = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y-%m-%d")
        except Exception:
            date_str = "0000-00-00"

    if supplier:
        company_label = _supplier_to_label(supplier)
    else:
        company_label = sender_to_label(sender)

    clean_original = sanitize_filename(original_name)
    return f"{date_str}_{company_label}_{clean_original}"


# ---------------------------------------------------------------------------
# Graph API helpers
# ---------------------------------------------------------------------------

def _headers(client_id: str) -> dict:
    token = get_access_token(client_id)
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _get_or_create_folder(client_id: str, parent_path: str, name: str) -> str:
    """
    Return the OneDrive item ID of a folder named `name` under `parent_path`.
    Creates it if it does not exist.

    Uses /{parent}:/{name} path resolution — avoids $filter which is not
    supported on /children for personal OneDrive accounts.

    parent_path examples:
        "/me/drive/root"         (OneDrive root)
        "/me/drive/items/{id}"   (subfolder by ID)
    """
    # Try to resolve the folder by path directly (404 = does not exist yet)
    get_url = f"{GRAPH_BASE}{parent_path}:/{name}?$select=id,name,folder"
    resp = requests.get(get_url, headers=_headers(client_id), timeout=30)

    if resp.status_code == 200:
        item = resp.json()
        if "folder" in item:
            return item["id"]
        # Item exists but is not a folder — fall through to create with a suffix

    if resp.status_code not in (200, 404):
        resp.raise_for_status()

    # Create the folder under parent
    create_url = f"{GRAPH_BASE}{parent_path}/children"
    payload = {
        "name": name,
        "folder": {},
        "@microsoft.graph.conflictBehavior": "fail",
    }
    resp = requests.post(
        create_url,
        headers=_headers(client_id),
        json=payload,
        timeout=30,
    )
    # 409 Conflict = already exists (race condition) — re-fetch by path
    if resp.status_code == 409:
        resp2 = requests.get(get_url, headers=_headers(client_id), timeout=30)
        resp2.raise_for_status()
        return resp2.json()["id"]
    resp.raise_for_status()
    folder_id = resp.json()["id"]
    logger.info("Created OneDrive folder: %s (id=%s)", name, folder_id)
    return folder_id


def _get_invoice_folder_id(
    client_id: str, root_folder_id: str, year: int, month: int,
    supplier_label: str | None = None,
) -> str:
    """
    Ensure ROOT/{YYYY}/{MM}/{supplier} exists and return the deepest folder ID.

    Structure: ROOT/YYYY/MM/<supplier_label>/
    If supplier_label is None, returns the month folder (used by _a_verifier).
    """
    year_id = _get_or_create_folder(
        client_id, f"/me/drive/items/{root_folder_id}", str(year)
    )
    month_id = _get_or_create_folder(
        client_id, f"/me/drive/items/{year_id}", f"{month:02d}"
    )
    if supplier_label:
        return _get_or_create_folder(
            client_id, f"/me/drive/items/{month_id}", supplier_label
        )
    return month_id


def _get_or_create_root_folder(client_id: str, folder_name: str) -> str:
    """
    Return the ID of the root folder (e.g. 'Factures-GHALI') at the top of OneDrive.
    Creates it if absent.
    """
    return _get_or_create_folder(client_id, "/me/drive/root", folder_name)


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------

# Graph API simple PUT limit is 4 MB; larger files need an upload session.
_SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024  # 4 MB
_CHUNK_SIZE = 3_276_800  # ~3.125 MB — must be a multiple of 320 KiB per Graph API docs


def _upload_to_folder(
    client_id: str,
    folder_id: str,
    filename: str,
    file_bytes: bytes,
    content_type: str,
    log_label: str,
) -> tuple[str, str]:
    """
    Upload a file to a specific OneDrive folder, choosing simple PUT or
    chunked upload session depending on file size.

    Returns:
        (onedrive_file_id, onedrive_web_url)
    """
    # Idempotency — skip if already uploaded (resolve by path)
    check_url = f"{GRAPH_BASE}/me/drive/items/{folder_id}:/{filename}?$select=id,webUrl"
    resp = requests.get(check_url, headers=_headers(client_id), timeout=30)
    if resp.status_code == 200:
        existing = resp.json()
        logger.info("File already exists in %s (skipping upload): %s", log_label, filename)
        return existing["id"], existing.get("webUrl", "")
    if resp.status_code != 404:
        resp.raise_for_status()

    if len(file_bytes) <= _SIMPLE_UPLOAD_LIMIT:
        return _simple_upload(client_id, folder_id, filename, file_bytes, content_type)
    return _chunked_upload(client_id, folder_id, filename, file_bytes, content_type)


def _simple_upload(
    client_id: str,
    folder_id: str,
    filename: str,
    file_bytes: bytes,
    content_type: str,
) -> tuple[str, str]:
    """Simple PUT upload for files <= 4 MB."""
    upload_url = f"{GRAPH_BASE}/me/drive/items/{folder_id}:/{filename}:/content"
    token = get_access_token(client_id)
    resp = requests.put(
        upload_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
        },
        data=file_bytes,
        timeout=120,
    )
    resp.raise_for_status()
    item = resp.json()
    return item["id"], item.get("webUrl", "")


def _chunked_upload(
    client_id: str,
    folder_id: str,
    filename: str,
    file_bytes: bytes,
    content_type: str,
) -> tuple[str, str]:
    """Chunked upload session for files > 4 MB (up to 250 MB)."""
    # 1. Create upload session
    session_url = f"{GRAPH_BASE}/me/drive/items/{folder_id}:/{filename}:/createUploadSession"
    token = get_access_token(client_id)
    resp = requests.post(
        session_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "item": {
                "@microsoft.graph.conflictBehavior": "replace",
                "name": filename,
            }
        },
        timeout=30,
    )
    resp.raise_for_status()
    upload_url = resp.json()["uploadUrl"]

    # 2. Upload in chunks
    total = len(file_bytes)
    logger.info("Starting chunked upload: file=%r size=%d bytes chunks=%d", filename, total, -(-total // _CHUNK_SIZE))
    offset = 0
    item = None
    while offset < total:
        end = min(offset + _CHUNK_SIZE, total)
        chunk = file_bytes[offset:end]
        resp = requests.put(
            upload_url,
            headers={
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end - 1}/{total}",
            },
            data=chunk,
            timeout=120,
        )
        resp.raise_for_status()
        if resp.status_code in (200, 201):
            item = resp.json()
        offset = end

    if item is None:
        raise RuntimeError(f"Chunked upload of {filename} completed but no item returned")

    return item["id"], item.get("webUrl", "")


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

REVIEW_SUBFOLDER = "_a_verifier"


def _upload_file(
    client_id: str,
    root_folder_name: str,
    attachment_name: str,
    attachment_bytes: bytes,
    content_type: str,
    sender: str,
    received_at: str,
    year: int,
    month: int,
    invoice_date: str | None = None,
    supplier: str | None = None,
    review: bool = False,
) -> tuple[str, str]:
    """
    Upload a single attachment to OneDrive.

    When review=False, uploads to ROOT/YYYY/MM/<supplier>/.
    When review=True,  uploads to ROOT/YYYY/MM/_a_verifier/.

    Returns:
        (onedrive_file_id, onedrive_web_url)
    """
    root_id = _get_or_create_root_folder(client_id, root_folder_name)

    if review:
        month_folder_id = _get_invoice_folder_id(client_id, root_id, year, month)
        folder_id = _get_or_create_folder(
            client_id, f"/me/drive/items/{month_folder_id}", REVIEW_SUBFOLDER
        )
        dest_label = f"{root_folder_name}/{year}/{month:02d}/_a_verifier/"
        log_label = "_a_verifier"
    else:
        supplier_label = (_supplier_to_label(supplier) if supplier else None) or sender_to_label(sender)
        folder_id = _get_invoice_folder_id(client_id, root_id, year, month, supplier_label)
        dest_label = f"{root_folder_name}/{year}/{month:02d}/{supplier_label or '(root)'}/"
        log_label = "OneDrive"

    filename = build_filename(received_at, sender, attachment_name, invoice_date=invoice_date, supplier=supplier)
    size_kb = len(attachment_bytes) / 1024
    logger.info(
        "Uploading%s: file=%r size=%.1f KB destination=%s",
        " to _a_verifier" if review else " invoice",
        filename, size_kb, dest_label,
    )

    file_id, web_url = _upload_to_folder(
        client_id, folder_id, filename, attachment_bytes, content_type,
        log_label=log_label,
    )
    logger.info("Upload complete: file=%r id=%s url=%s", filename, file_id, web_url)
    return file_id, web_url


def upload_attachment(
    client_id: str,
    root_folder_name: str,
    attachment_name: str,
    attachment_bytes: bytes,
    content_type: str,
    sender: str,
    received_at: str,
    year: int,
    month: int,
    invoice_date: str | None = None,
    supplier: str | None = None,
) -> tuple[str, str]:
    """Upload a single attachment to the correct OneDrive supplier folder.

    Returns: (onedrive_file_id, onedrive_web_url)
    """
    return _upload_file(
        client_id, root_folder_name, attachment_name, attachment_bytes,
        content_type, sender, received_at, year, month,
        invoice_date=invoice_date, supplier=supplier, review=False,
    )


def upload_to_review(
    client_id: str,
    root_folder_name: str,
    attachment_name: str,
    attachment_bytes: bytes,
    content_type: str,
    sender: str,
    received_at: str,
    year: int,
    month: int,
    invoice_date: str | None = None,
    supplier: str | None = None,
) -> tuple[str, str]:
    """Upload an attachment to the _a_verifier/ subfolder.

    Returns: (onedrive_file_id, onedrive_web_url)
    """
    return _upload_file(
        client_id, root_folder_name, attachment_name, attachment_bytes,
        content_type, sender, received_at, year, month,
        invoice_date=invoice_date, supplier=supplier, review=True,
    )

