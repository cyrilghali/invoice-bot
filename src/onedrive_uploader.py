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
from utils import GRAPH_BASE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Filename helpers (identical to the old drive_uploader)
# ---------------------------------------------------------------------------

def _sanitize_filename(name: str) -> str:
    """Remove characters that are problematic in filenames."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)


def _supplier_to_label(supplier: str) -> str:
    """
    Convert a free-text supplier name to a compact filename-safe label.

    Examples:
        "EDF Électricité de France"  -> "edf-electricite-de-france"
        "Free SAS"                   -> "free-sas"
        "Orange S.A."                -> "orange-sa"
    """
    # Normalise unicode (é -> e, ç -> c, etc.)
    nfkd = unicodedata.normalize("NFKD", supplier)
    ascii_str = nfkd.encode("ascii", "ignore").decode("ascii")
    # Lowercase, replace non-alphanumeric runs with a single hyphen
    label = re.sub(r"[^a-z0-9]+", "-", ascii_str.lower()).strip("-")
    # Cap length so filenames stay reasonable
    return label[:40]


# Common TLD suffixes to strip when extracting the company name from a domain.
# Covers country-code + generic TLDs, including compound ones (co.uk, com.fr, …).
_COMPOUND_TLDS = {
    "co.uk", "co.jp", "co.nz", "co.za", "co.in", "co.kr",
    "com.au", "com.br", "com.fr", "com.mx", "com.ar",
    "org.uk", "net.au", "gov.uk",
}


def _sender_to_label(sender: str) -> str:
    """
    Extract the company name from a sender email address.

    Strategy: take the second-level domain (just before the TLD), which is
    almost always the company name regardless of subdomains or compound TLDs.

    Examples:
        noreply@hotmail.com               -> hotmail   (fallback still works)
        factures@edf.fr                   -> edf
        billing@notifications.amazon.fr   -> amazon
        invoice@free.fr                   -> free
        no-reply@mailo.com                -> mailo
        support@company.co.uk             -> company
    """
    if "@" in sender:
        domain = sender.split("@")[-1].lower().strip()
    else:
        domain = sender.lower().strip()

    parts = domain.split(".")

    # Check for known compound TLDs (e.g. co.uk) — strip 2 parts from right
    if len(parts) >= 3 and ".".join(parts[-2:]) in _COMPOUND_TLDS:
        company = parts[-3]
    elif len(parts) >= 2:
        # Standard: strip the last part (TLD), take the one before it
        company = parts[-2]
    else:
        company = parts[0]

    return _sanitize_filename(company)


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
        company_label = _sender_to_label(sender)

    clean_original = _sanitize_filename(original_name)
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
# Public interface
# ---------------------------------------------------------------------------

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
    """
    Upload a single attachment to the correct OneDrive folder.

    Returns:
        (onedrive_file_id, onedrive_web_url)
    """
    root_id = _get_or_create_root_folder(client_id, root_folder_name)
    # Determine supplier folder label: AI-extracted name takes priority, sender domain as fallback
    supplier_label = (_supplier_to_label(supplier) if supplier else None) or _sender_to_label(sender)
    folder_id = _get_invoice_folder_id(client_id, root_id, year, month, supplier_label)
    filename = build_filename(received_at, sender, attachment_name, invoice_date=invoice_date, supplier=supplier)

    size_kb = len(attachment_bytes) / 1024
    logger.info(
        "Uploading invoice: file=%r size=%.1f KB destination=%s/%d/%02d/%s/",
        filename, size_kb, root_folder_name, year, month, supplier_label or "(root)",
    )

    # Check idempotency — skip if already uploaded (resolve by path, avoids $filter)
    check_url = f"{GRAPH_BASE}/me/drive/items/{folder_id}:/{filename}?$select=id,webUrl"
    resp = requests.get(check_url, headers=_headers(client_id), timeout=30)
    if resp.status_code == 200:
        existing = resp.json()
        logger.info("File already exists in OneDrive (skipping upload): %s", filename)
        return existing["id"], existing.get("webUrl", "")
    if resp.status_code != 404:
        resp.raise_for_status()

    # Upload via PUT (simple upload, up to 4 MB; for larger files use upload session)
    upload_url = (
        f"{GRAPH_BASE}/me/drive/items/{folder_id}:/{filename}:/content"
    )
    token = get_access_token(client_id)
    resp = requests.put(
        upload_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
        },
        data=attachment_bytes,
        timeout=120,
    )
    resp.raise_for_status()
    item = resp.json()
    file_id = item["id"]
    web_url = item.get("webUrl", "")
    logger.info(
        "Upload complete: file=%r id=%s url=%s",
        filename, file_id, web_url,
    )
    return file_id, web_url


REVIEW_SUBFOLDER = "_a_verifier"


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
    """
    Upload an attachment to the _a_verifier/ subfolder inside the month folder.

    Files land at: ROOT/YYYY/MM/_a_verifier/YYYY-MM-DD_company_filename.ext

    Returns:
        (onedrive_file_id, onedrive_web_url)
    """
    root_id = _get_or_create_root_folder(client_id, root_folder_name)
    month_folder_id = _get_invoice_folder_id(client_id, root_id, year, month)
    review_folder_id = _get_or_create_folder(
        client_id, f"/me/drive/items/{month_folder_id}", REVIEW_SUBFOLDER
    )

    filename = build_filename(received_at, sender, attachment_name, invoice_date=invoice_date, supplier=supplier)
    size_kb = len(attachment_bytes) / 1024
    logger.info(
        "Uploading to _a_verifier: file=%r size=%.1f KB destination=%s/%d/%02d/_a_verifier/",
        filename, size_kb, root_folder_name, year, month,
    )

    # Idempotency check
    check_url = f"{GRAPH_BASE}/me/drive/items/{review_folder_id}:/{filename}?$select=id,webUrl"
    resp = requests.get(check_url, headers=_headers(client_id), timeout=30)
    if resp.status_code == 200:
        existing = resp.json()
        logger.info("File already exists in _a_verifier (skipping upload): %s", filename)
        return existing["id"], existing.get("webUrl", "")
    if resp.status_code != 404:
        resp.raise_for_status()

    upload_url = f"{GRAPH_BASE}/me/drive/items/{review_folder_id}:/{filename}:/content"
    token = get_access_token(client_id)
    resp = requests.put(
        upload_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
        },
        data=attachment_bytes,
        timeout=120,
    )
    resp.raise_for_status()
    item = resp.json()
    file_id = item["id"]
    web_url = item.get("webUrl", "")
    logger.info(
        "Upload to _a_verifier complete: file=%r id=%s url=%s",
        filename, file_id, web_url,
    )
    return file_id, web_url


def get_month_folder_link(
    client_id: str, root_folder_name: str, year: int, month: int
) -> str:
    """Return the web URL of the month folder in OneDrive (for the monthly report)."""
    root_id = _get_or_create_root_folder(client_id, root_folder_name)
    folder_id = _get_invoice_folder_id(client_id, root_id, year, month)
    resp = requests.get(
        f"{GRAPH_BASE}/me/drive/items/{folder_id}?$select=webUrl",
        headers=_headers(client_id),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("webUrl", "")
