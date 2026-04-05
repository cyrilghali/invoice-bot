#!/usr/bin/env python3
"""
One-time OneDrive cleanup script.

Handles:
1. Delete the orphan Darty JPG from the wrong 2028/01 folder
2. Delete duplicate files that were removed from DB
3. Delete the empty 2028 folder tree if it exists

Uses the same auth as the bot (MSAL token cache).
"""

import os
import sys
import logging

# Add src/ to path so we can reuse bot modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from auth_setup import get_access_token
from utils import GRAPH_BASE
import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "a2a25d4f-3562-4e0c-93b8-c15883e75d83")
ACCOUNT = "colisee.ghali@hotmail.com"
ROOT_FOLDER = "Factures-GHALI"


def headers():
    token = get_access_token(CLIENT_ID, account_hint=ACCOUNT)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def resolve_path(path: str) -> dict | None:
    """Resolve a OneDrive path to item metadata. Returns None if 404."""
    url = f"{GRAPH_BASE}/me/drive/root:/{path}?$select=id,name,webUrl,folder,file,size"
    resp = requests.get(url, headers=headers(), timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def list_children(item_id: str) -> list[dict]:
    """List children of a folder."""
    url = f"{GRAPH_BASE}/me/drive/items/{item_id}/children?$select=id,name,webUrl,folder,file,size"
    resp = requests.get(url, headers=headers(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def delete_item(item_id: str, name: str) -> bool:
    """Delete a OneDrive item by ID."""
    url = f"{GRAPH_BASE}/me/drive/items/{item_id}"
    resp = requests.delete(url, headers=headers(), timeout=30)
    if resp.status_code == 204:
        logger.info("DELETED: %s (id=%s)", name, item_id)
        return True
    elif resp.status_code == 404:
        logger.info("NOT FOUND (already gone): %s", name)
        return False
    else:
        resp.raise_for_status()
        return False


def move_item(item_id: str, dest_folder_id: str, new_name: str | None = None) -> dict:
    """Move a OneDrive item to a different folder."""
    url = f"{GRAPH_BASE}/me/drive/items/{item_id}"
    body: dict = {"parentReference": {"id": dest_folder_id}}
    if new_name:
        body["name"] = new_name
    resp = requests.patch(url, headers=headers(), json=body, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    logger.info("MOVED: %s → folder %s", result.get("name"), dest_folder_id)
    return result


def main():
    # -----------------------------------------------------------------------
    # 1. Find and clean up the 2028 folder (Darty JPG with wrong year)
    # -----------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("Step 1: Clean up 2028 folder (Darty OCR year error)")
    logger.info("=" * 60)

    folder_2028 = resolve_path(f"{ROOT_FOLDER}/2028")
    if folder_2028:
        logger.info("Found 2028 folder: %s", folder_2028["id"])
        # List everything inside recursively
        months = list_children(folder_2028["id"])
        for month_folder in months:
            logger.info("  Month folder: %s", month_folder["name"])
            if "folder" in month_folder:
                items = list_children(month_folder["id"])
                for item in items:
                    if "folder" in item:
                        # supplier subfolder
                        sub_items = list_children(item["id"])
                        for sub in sub_items:
                            logger.info("    File: %s (size=%s)", sub["name"], sub.get("size"))
                            delete_item(sub["id"], sub["name"])
                        # delete empty supplier folder
                        delete_item(item["id"], item["name"])
                    else:
                        logger.info("    File: %s (size=%s)", item["name"], item.get("size"))
                        delete_item(item["id"], item["name"])
                # delete empty month folder
                delete_item(month_folder["id"], month_folder["name"])
        # delete empty 2028 folder
        delete_item(folder_2028["id"], "2028")
    else:
        logger.info("No 2028 folder found — already clean")

    # -----------------------------------------------------------------------
    # 2. Find and delete duplicate files that were removed from DB
    #    These are files that exist on OneDrive but their DB rows were deleted.
    #    We know the exact filenames from the deleted rows.
    # -----------------------------------------------------------------------
    logger.info("")
    logger.info("=" * 60)
    logger.info("Step 2: Delete duplicate files from OneDrive")
    logger.info("=" * 60)

    # Duplicate files to delete (from the DB rows we removed)
    # Format: (year, month, supplier_label, filename)
    dupes_to_delete = [
        # Fresca 2026-02-28 1778.68 — ids 22, 27 deleted (keep 17)
        (2026, 2, "fresca", "2026-02-28_fresca_ListeReleve-20260302132448.pdf"),   # id=22 (email) — same filename as 27
        # Note: id=27 from colisee_inbox has same filename, file dedup in uploader means only one copy on disk
        
        # Carniato 2026-03-02 643.67 — id 28 deleted (keep 23)
        # id=28 colisee_inbox, same filename as id=23 — uploader skipped it (idempotent)
        
        # Fresca 2026-03-03 12.55 — id 24 deleted (keep 19)
        # id=24 colisee_inbox same as 19 — uploader skipped
        
        # Fresca 2026-03-03 444.82 — id 26 deleted (keep 21)
        # id=26 colisee_inbox same as 21 — uploader skipped
        
        # Rouquette 2026-03-02 209.04 — id 25 deleted (keep 20)
        # id=25 colisee_inbox same as 20 — uploader skipped
        
        # La Romainville 2026-01-29 168.2 — id 72 deleted (keep 65)
        (2026, 1, "la-romainville", "2026-01-29_la-romainville_IMG_0609.JPG"),  # id=72
        
        # Darty semantic dupe — id 73 deleted (keep 49)
        # Already handled above in the 2028 folder cleanup
    ]

    for year, month, supplier_label, filename in dupes_to_delete:
        path = f"{ROOT_FOLDER}/{year}/{month:02d}/{supplier_label}/{filename}"
        logger.info("Checking: %s", path)
        item = resolve_path(path)
        if item:
            delete_item(item["id"], f"{path}")
        else:
            logger.info("  Not found (already removed or uploader deduplicated)")

    # -----------------------------------------------------------------------
    # 3. Summary
    # -----------------------------------------------------------------------
    logger.info("")
    logger.info("=" * 60)
    logger.info("Cleanup complete!")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
