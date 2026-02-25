"""
Attachment processing pipeline.

Classifies each email attachment and routes it to the appropriate OneDrive folder.
"""

import io
import logging
import zipfile
from datetime import datetime

import db
from classifier import is_invoice
from onedrive_uploader import build_filename, upload_attachment, upload_to_review
from poller import Attachment, Email

logger = logging.getLogger(__name__)

# Supported member types inside a ZIP archive
_ZIP_SUPPORTED_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".xlsx", ".xls")


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
    email: Email,
    year: int,
    month: int,
    config: dict,
    data_dir: str,
    client_id: str,
    root_folder_name: str,
) -> str:
    """
    Classify a single attachment and upload it to the appropriate OneDrive folder.
    ZIP files are unpacked and each member is processed individually — the ZIP
    itself is never uploaded.

    Returns the status string: "invoice", "review", or "rejected".
    For ZIPs, returns "invoice" if at least one member was an invoice.

    Raises on unexpected errors — callers should catch and log.
    """
    # --- ZIP: unpack and recurse into each member ---
    name_lower = attachment.name.lower()
    ct = attachment.content_type.split(";")[0].strip().lower()
    if ct in ("application/zip", "application/x-zip-compressed") or name_lower.endswith(".zip"):
        members = _unpack_zip(attachment)
        if not members:
            logger.info("ZIP %s: no supported members found, skipping", attachment.name)
            return "rejected"
        logger.info("ZIP %s: unpacking %d member(s) for individual classification", attachment.name, len(members))
        any_invoice = False
        for member in members:
            try:
                member_status = process_attachment(
                    attachment=member,
                    email=email,
                    year=year,
                    month=month,
                    config=config,
                    data_dir=data_dir,
                    client_id=client_id,
                    root_folder_name=root_folder_name,
                )
                if member_status == "invoice":
                    any_invoice = True
            except Exception as e:
                logger.error("Failed to process ZIP member %s: %s", member.name, e, exc_info=True)
        return "invoice" if any_invoice else "rejected"

    # --- Normal (non-ZIP) attachment ---
    logger.info(
        "Processing attachment: file=%r type=%s size=%d bytes from=%s",
        attachment.name,
        attachment.content_type.split(";")[0].strip(),
        len(attachment.content_bytes),
        email.sender,
    )

    # Look up canonical supplier hint for this sender
    sender_key = email.sender.lower().strip()
    sender_suppliers: dict[str, str] = (config.get("invoices") or {}).get("sender_suppliers") or {}
    hint_supplier: str | None = sender_suppliers.get(sender_key)

    status, invoice_date, doc_supplier, amount_ht, amount_ttc, amount_tva, currency = is_invoice(
        attachment, config, hint_supplier=hint_supplier
    )

    # Use AI-extracted supplier name for folder/filename (falls back to sender domain in uploader)
    filename_supplier: str | None = doc_supplier

    # Derive folder year/month from invoice date when available
    inv_year, inv_month = year, month
    if invoice_date:
        try:
            inv_dt = datetime.fromisoformat(invoice_date)
            inv_year, inv_month = inv_dt.year, inv_dt.month
            logger.info(
                "Using invoice date %s for %s (received %s)",
                invoice_date, attachment.name, email.received_at,
            )
        except ValueError:
            logger.warning(
                "Could not parse invoice_date %r for %s — falling back to received date",
                invoice_date, attachment.name,
            )
            invoice_date = None

    stored_filename = build_filename(
        email.received_at, email.sender, attachment.name,
        invoice_date=invoice_date,
        supplier=filename_supplier,
    )

    if status == "invoice":
        logger.info(
            "INVOICE confirmed: file=%r supplier=%r invoice_date=%r "
            "amount_ht=%s amount_ttc=%s currency=%r folder=%s/%d/%02d",
            attachment.name, doc_supplier, invoice_date,
            amount_ht, amount_ttc, currency, root_folder_name, inv_year, inv_month,
        )
        drive_file_id, drive_web_link = upload_attachment(
            client_id=client_id,
            root_folder_name=root_folder_name,
            attachment_name=attachment.name,
            attachment_bytes=attachment.content_bytes,
            content_type=attachment.content_type,
            sender=email.sender,
            received_at=email.received_at,
            year=inv_year,
            month=inv_month,
            invoice_date=invoice_date,
            supplier=filename_supplier,
        )
        db.save_invoice(
            data_dir,
            email_id=email.email_id,
            filename=stored_filename,
            sender=email.sender,
            received_at=email.received_at,
            year=inv_year,
            month=inv_month,
            drive_file_id=drive_file_id,
            drive_web_link=drive_web_link,
            invoice_date=invoice_date,
            supplier=doc_supplier,
            amount_ht=amount_ht,
            amount_ttc=amount_ttc,
            amount_tva=amount_tva,
            currency=currency,
        )
        logger.info(
            "Invoice saved to DB: filename=%r year=%d month=%d link=%s",
            stored_filename, inv_year, inv_month, drive_web_link,
        )
    else:
        reason_label = "rejected" if status == "rejected" else "review"
        logger.info(
            "Routing to _a_verifier: file=%r status=%s from=%s folder=%s/%d/%02d/_a_verifier",
            attachment.name, reason_label, email.sender, root_folder_name, inv_year, inv_month,
        )
        _, review_web_link = upload_to_review(
            client_id=client_id,
            root_folder_name=root_folder_name,
            attachment_name=attachment.name,
            attachment_bytes=attachment.content_bytes,
            content_type=attachment.content_type,
            sender=email.sender,
            received_at=email.received_at,
            year=inv_year,
            month=inv_month,
            invoice_date=invoice_date,
            supplier=filename_supplier,
        )
    return status
