"""
Manual source — process a local file (photo, PDF, scan) through the invoice pipeline.

Designed to be called from the CLI by Hermes or any external trigger:

    python -m sources.run manual_upload --file /path/to/photo.jpg
    python -m sources.run manual_upload --file /path/to/photo.jpg --sender "Dad"

The file is classified, uploaded to OneDrive, and tracked in the DB just like
email-sourced invoices. Dedup uses the file's SHA-256 hash as source_id.

Config in config.yaml:
    sources:
      manual_upload:
        module: manual_source
        onedrive_folder_name: "Factures-GHALI"
        onedrive_account: "colisee.ghali@hotmail.com"
        default_sender: "manual"
"""

import hashlib
import logging
import mimetypes
import os
import sys
from datetime import datetime, timezone

import db
from pipeline import process_attachment
from poller import Attachment
from utils import DEFAULT_DATA_DIR

logger = logging.getLogger(__name__)

# MIME types we can process (same as email_source)
SUPPORTED_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/zip",
    "application/x-zip-compressed",
    "text/csv",
}


def _file_hash(data: bytes) -> str:
    """SHA-256 hex digest of file contents — used as dedup key."""
    return hashlib.sha256(data).hexdigest()


def _guess_content_type(filepath: str) -> str:
    """Guess MIME type from file extension, defaulting to application/pdf."""
    ct, _ = mimetypes.guess_type(filepath)
    return ct or "application/pdf"


def run(config: dict, data_dir: str) -> None:
    """
    Process one or more local files through the invoice pipeline.

    Expects config["_files"] to be a list of file paths (injected by run.py
    from --file CLI args). Falls back to config["files"] for config.yaml usage.
    """
    source_name = config.get("source_name", "manual_upload")
    started_at = datetime.now(tz=timezone.utc).isoformat()

    # Get file paths from CLI injection or config
    files: list[str] = config.get("_files") or config.get("files") or []
    if not files:
        logger.error("No files to process. Use --file <path> to specify files.")
        print("Error: no files to process. Use --file <path>", file=sys.stderr)
        return

    sender = config.get("_sender") or config.get("default_sender", "manual")
    client_id: str = config["client_id"]
    root_folder_name: str = config["onedrive_folder_name"]
    upload_account_hint: str | None = config.get("onedrive_account")

    logger.info("========== %s: MANUAL PROCESSING START ==========", source_name)
    logger.info("Files to process: %d, sender: %s", len(files), sender)

    # Build pipeline config
    pipeline_config = {}
    if config.get("invoices"):
        pipeline_config["invoices"] = config["invoices"]
    if config.get("classifier"):
        pipeline_config["classifier"] = config["classifier"]

    documents_found = 0
    documents_new = 0
    invoices_saved = 0
    error_message = None
    results: list[dict] = []

    try:
        for filepath in files:
            filepath = os.path.expanduser(filepath)
            if not os.path.isfile(filepath):
                logger.error("File not found: %s", filepath)
                results.append({"file": filepath, "status": "error", "reason": "file not found"})
                continue

            documents_found += 1
            filename = os.path.basename(filepath)
            content_type = _guess_content_type(filepath)

            if content_type not in SUPPORTED_TYPES:
                logger.warning("Unsupported file type %s for %s, skipping", content_type, filename)
                results.append({"file": filepath, "status": "rejected", "reason": f"unsupported type: {content_type}"})
                continue

            file_bytes = open(filepath, "rb").read()
            source_id = _file_hash(file_bytes)

            # Dedup
            if db.is_document_processed(data_dir, source_name, source_id):
                logger.info("File %s already processed (hash=%s…), skipping.", filename, source_id[:12])
                results.append({"file": filepath, "status": "skipped", "reason": "already processed"})
                continue

            documents_new += 1
            now = datetime.now(tz=timezone.utc)
            received_at = now.isoformat()
            year = now.year
            month = now.month

            attachment = Attachment(
                name=filename,
                content_type=content_type,
                content_bytes=file_bytes,
            )

            try:
                status = process_attachment(
                    attachment=attachment,
                    sender=sender,
                    received_at=received_at,
                    year=year,
                    month=month,
                    config=pipeline_config,
                    data_dir=data_dir,
                    client_id=client_id,
                    root_folder_name=root_folder_name,
                    source_name=source_name,
                    source_document_id=source_id,
                    account_hint=upload_account_hint,
                )
                if status == "invoice":
                    invoices_saved += 1
                results.append({"file": filepath, "status": status})
            except Exception as e:
                logger.error("Failed to process %s: %s", filename, e, exc_info=True)
                results.append({"file": filepath, "status": "error", "reason": str(e)})

            db.mark_document_processed(
                data_dir,
                source_name=source_name,
                source_id=source_id,
                sender=sender,
                subject=f"manual: {filename}",
                received_at=received_at,
            )

    except Exception as e:
        error_message = str(e)
        logger.error("Manual source failed: %s", e, exc_info=True)

    finished_at = datetime.now(tz=timezone.utc).isoformat()
    db.save_source_run(
        data_dir,
        source_name=source_name,
        started_at=started_at,
        finished_at=finished_at,
        status="error" if error_message else "ok",
        documents_found=documents_found,
        documents_new=documents_new,
        invoices_saved=invoices_saved,
        error_message=error_message,
    )

    # Print summary to stdout (for Hermes to read)
    for r in results:
        status_icon = {"invoice": "✅", "review": "⚠️", "rejected": "❌", "skipped": "⏭️", "duplicate": "⏭️", "error": "💥"}.get(r["status"], "?")
        reason = f" ({r['reason']})" if "reason" in r else ""
        print(f"{status_icon} {os.path.basename(r['file'])}: {r['status']}{reason}")

    logger.info(
        "Manual processing complete. %d file(s), %d new, %d invoice(s).",
        documents_found, documents_new, invoices_saved,
    )
    logger.info("========== %s: MANUAL PROCESSING END ==========\n", source_name)
