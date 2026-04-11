"""
Attachment processing pipeline.

Classifies each email attachment and routes it to the appropriate OneDrive folder.
"""

import hashlib
import io
import logging
import zipfile
from datetime import datetime

import db
from classifier import is_invoice
from onedrive_uploader import build_filename, upload_attachment, upload_to_review
from poller import Attachment
from utils import normalize_content_type

logger = logging.getLogger(__name__)

# Supported member types inside a ZIP archive
_ZIP_SUPPORTED_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".xlsx", ".xls", ".csv")


def _unpack_zip(attachment: Attachment) -> list[Attachment]:
    """
    Extract all supported files from a ZIP attachment and return them as
    individual Attachment objects, preserving the original content_type.
    Nested ZIPs are skipped.
    """
    members: list[Attachment] = []
    try:
        with zipfile.ZipFile(io.BytesIO(attachment.content_bytes)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                # Skip macOS resource fork files (.__MACOSX/, ._filename)
                basename = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
                if basename.startswith("._") or "__MACOSX" in info.filename:
                    logger.debug("ZIP member %s: skipping macOS metadata file", info.filename)
                    continue
                name_lower = info.filename.lower()
                if not any(name_lower.endswith(ext) for ext in _ZIP_SUPPORTED_EXTENSIONS):
                    logger.debug("ZIP member %s: unsupported type, skipping", info.filename)
                    continue
                try:
                    data = zf.read(info.filename)
                except Exception as e:
                    logger.warning("Could not read ZIP member %s: %s", info.filename, e)
                    continue
                ext = name_lower.rsplit(".", 1)[-1]
                ct_map = {
                    "pdf": "application/pdf",
                    "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "png": "image/png",
                    "tiff": "image/tiff",
                    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "xls": "application/vnd.ms-excel",
                    "csv": "text/csv",
                }
                content_type = ct_map.get(ext, "application/octet-stream")
                members.append(Attachment(
                    name=basename,
                    content_type=content_type,
                    content_bytes=data,
                ))
    except zipfile.BadZipFile as e:
        logger.warning("Could not open ZIP %s: %s", attachment.name, e)
    return members


def process_attachment(
    attachment: Attachment,
    sender: str,
    received_at: str,
    year: int,
    month: int,
    config: dict,
    data_dir: str,
    client_id: str,
    root_folder_name: str,
    source_name: str | None = None,
    source_document_id: str | None = None,
    account_hint: str | None = None,
) -> dict:
    """
    Classify a single attachment and upload it to the appropriate OneDrive folder.
    ZIP files are unpacked and each member is processed individually — the ZIP
    itself is never uploaded.

    Returns a dict: {"status": str, "existing": dict | None}. Status is one of
    "invoice", "review", "rejected", or "duplicate". "existing" is populated
    when the pipeline detected that this file represents an already-stored
    invoice (either via content hash or via classifier-fingerprint match).
    For ZIPs, returns status="invoice" if at least one member was an invoice.

    Raises on unexpected errors — callers should catch and log.
    """
    # --- ZIP: unpack and recurse into each member ---
    name_lower = attachment.name.lower()
    ct = normalize_content_type(attachment.content_type)
    if ct in ("application/zip", "application/x-zip-compressed") or name_lower.endswith(".zip"):
        members = _unpack_zip(attachment)
        if not members:
            logger.info("ZIP %s: no supported members found, skipping", attachment.name)
            return {"status": "rejected", "existing": None}
        logger.info("ZIP %s: unpacking %d member(s) for individual classification", attachment.name, len(members))
        any_invoice = False
        for member in members:
            try:
                member_result = process_attachment(
                    attachment=member,
                    sender=sender,
                    received_at=received_at,
                    year=year,
                    month=month,
                    config=config,
                    data_dir=data_dir,
                    client_id=client_id,
                    root_folder_name=root_folder_name,
                    source_name=source_name,
                    source_document_id=source_document_id,
                    account_hint=account_hint,
                )
                if member_result.get("status") == "invoice":
                    any_invoice = True
            except Exception as e:
                logger.error("Failed to process ZIP member %s: %s", member.name, e, exc_info=True)
        return {"status": "invoice" if any_invoice else "rejected", "existing": None}

    # --- Normal (non-ZIP) attachment ---
    logger.info(
        "Processing attachment: file=%r type=%s size=%d bytes from=%s",
        attachment.name,
        attachment.content_type.split(";")[0].strip(),
        len(attachment.content_bytes),
        sender,
    )

    # Cross-source dedup: skip if identical content was already invoiced
    content_hash = hashlib.sha256(attachment.content_bytes).hexdigest()
    existing = db.is_content_already_invoiced(data_dir, content_hash)
    if existing:
        logger.info(
            "DUPLICATE skipped: file=%r matches existing invoice id=%d (%s via %s)",
            attachment.name, existing["id"], existing["filename"], existing.get("source_name"),
        )
        return {"status": "duplicate", "existing": existing}

    # Look up canonical supplier hint for this sender
    sender_key = sender.lower().strip()
    sender_suppliers: dict[str, str] = (config.get("invoices") or {}).get("sender_suppliers") or {}
    hint_supplier: str | None = sender_suppliers.get(sender_key)

    status, invoice_date, doc_supplier, entity, amount_ht, amount_ttc, amount_tva, currency = is_invoice(
        attachment, config, hint_supplier=hint_supplier
    )

    # Canonical supplier from config takes priority over AI extraction
    filename_supplier: str | None = hint_supplier or doc_supplier

    # Resolve OneDrive root folder based on entity (billed party).
    # entity_folders in config maps entity name patterns to folder names.
    # Falls back to the source's default root_folder_name.
    resolved_root_folder = root_folder_name
    if entity:
        entity_folders: dict[str, str] = (config.get("invoices") or {}).get("entity_folders") or {}
        entity_upper = entity.upper()
        for pattern, folder in entity_folders.items():
            if pattern.upper() in entity_upper:
                resolved_root_folder = folder
                logger.info("Entity '%s' matched pattern '%s' → folder '%s'", entity, pattern, folder)
                break

    # Derive folder year/month from invoice date when available
    inv_year, inv_month = year, month
    if invoice_date:
        try:
            inv_dt = datetime.fromisoformat(invoice_date)
            inv_year, inv_month = inv_dt.year, inv_dt.month
            logger.info(
                "Using invoice date %s for %s (received %s)",
                invoice_date, attachment.name, received_at,
            )
        except ValueError:
            logger.warning(
                "Could not parse invoice_date %r for %s — falling back to received date",
                invoice_date, attachment.name,
            )
            invoice_date = None

    stored_filename = build_filename(
        received_at, sender, attachment.name,
        invoice_date=invoice_date,
        supplier=filename_supplier,
    )

    if status == "invoice":
        # Fingerprint dedup — catches re-photographed or re-compressed copies of
        # the same real-world receipt where sha256 differs but extracted fields
        # (supplier, date, total) are identical. Runs before upload so we don't
        # pollute OneDrive with near-duplicates.
        fingerprint_match = db.find_invoice_by_fingerprint(
            data_dir, doc_supplier, invoice_date, amount_ttc
        )
        if fingerprint_match and fingerprint_match.get("drive_web_link"):
            logger.info(
                "FINGERPRINT DUPLICATE: file=%r matches existing invoice id=%d "
                "supplier=%r date=%s amount_ttc=%s — skipping upload/save",
                attachment.name, fingerprint_match["id"],
                fingerprint_match.get("supplier"), invoice_date, amount_ttc,
            )
            return {"status": "duplicate", "existing": fingerprint_match}

        logger.info(
            "INVOICE confirmed: file=%r supplier=%r entity=%r invoice_date=%r "
            "amount_ht=%s amount_ttc=%s currency=%r folder=%s/%d/%02d",
            attachment.name, doc_supplier, entity, invoice_date,
            amount_ht, amount_ttc, currency, resolved_root_folder, inv_year, inv_month,
        )
        drive_file_id, drive_web_link = upload_attachment(
            client_id=client_id,
            root_folder_name=resolved_root_folder,
            attachment_name=attachment.name,
            attachment_bytes=attachment.content_bytes,
            content_type=attachment.content_type,
            sender=sender,
            received_at=received_at,
            year=inv_year,
            month=inv_month,
            invoice_date=invoice_date,
            supplier=filename_supplier,
            account_hint=account_hint,
        )
        db.save_invoice(
            data_dir,
            filename=stored_filename,
            sender=sender,
            received_at=received_at,
            year=inv_year,
            month=inv_month,
            source_name=source_name,
            source_document_id=source_document_id,
            drive_file_id=drive_file_id,
            drive_web_link=drive_web_link,
            invoice_date=invoice_date,
            supplier=doc_supplier,
            entity=entity,
            amount_ht=amount_ht,
            amount_ttc=amount_ttc,
            amount_tva=amount_tva,
            currency=currency,
            content_hash=content_hash,
        )
        logger.info(
            "Invoice saved to DB: filename=%r year=%d month=%d link=%s",
            stored_filename, inv_year, inv_month, drive_web_link,
        )
    elif status == "review":
        logger.info(
            "Uncertain — routing to _a_verifier: file=%r from=%s folder=%s/%d/%02d/_a_verifier",
            attachment.name, sender, resolved_root_folder, inv_year, inv_month,
        )
        _, review_web_link = upload_to_review(
            client_id=client_id,
            root_folder_name=resolved_root_folder,
            attachment_name=attachment.name,
            attachment_bytes=attachment.content_bytes,
            content_type=attachment.content_type,
            sender=sender,
            received_at=received_at,
            year=inv_year,
            month=inv_month,
            invoice_date=invoice_date,
            supplier=filename_supplier,
            account_hint=account_hint,
        )
    else:
        # rejected — confidently not an invoice (CGU, CGV, logos, etc.) — skip entirely
        logger.info("Rejected (not an invoice): file=%r from=%s — not uploaded", attachment.name, sender)
    return {"status": status, "existing": None}
