"""
Email inbox source — fetches invoices from a Microsoft Outlook/Hotmail inbox
via the Graph API and processes them through the classify → upload → DB pipeline.
"""

import logging
import os
from datetime import datetime, timezone

import db
from pipeline import process_attachment
from poller import GraphClient
from utils import DEFAULT_DATA_DIR

logger = logging.getLogger(__name__)


def run(config: dict, data_dir: str) -> None:
    """
    Fetch new emails from an inbox and process attachments.

    Args:
        config: Source instance config dict. Expected keys:
            - source_name: instance name (set by caller)
            - client_id: Azure App Registration Client ID
            - whitelisted_senders: optional list of sender addresses
            - subject_keywords: optional list of subject filter keywords
            - since_date: optional ISO date floor
            - link_keywords: optional list of link detection keywords
            - onedrive_folder_name: OneDrive root folder name
            - invoices: optional dict with sender_suppliers mapping
        data_dir: Path to data directory (SQLite DB, token cache).
    """
    source_name = config.get("source_name", "email")
    started_at = datetime.now(tz=timezone.utc).isoformat()

    logger.info("========== %s: POLL START ==========", source_name)
    logger.info("Poll triggered at %s (UTC)", started_at)

    client_id: str = config["client_id"]
    account_hint: str | None = config.get("account") or None
    # OneDrive upload can use a different account (e.g. centralize invoices
    # from multiple inboxes into one OneDrive). Falls back to inbox account.
    upload_account_hint: str | None = config.get("onedrive_account") or account_hint
    root_folder_name: str = config["onedrive_folder_name"]

    # Optional whitelist
    raw_senders: list[str] = config.get("whitelisted_senders") or []
    whitelisted_senders = [s.lower().strip() for s in raw_senders] if raw_senders else None
    if whitelisted_senders:
        logger.info("Sender whitelist active: %d senders", len(whitelisted_senders))
    else:
        logger.info("No sender whitelist — scanning all inbox attachments (AI classifier active)")

    # Optional subject keyword filter
    raw_subject_kws: list[str] = config.get("subject_keywords") or []
    subject_keywords = [k.lower().strip() for k in raw_subject_kws] if raw_subject_kws else None
    if subject_keywords:
        logger.info("Subject keyword filter active: %d keywords", len(subject_keywords))

    graph = GraphClient(client_id, account_hint=account_hint)

    # Optional date floor
    since_date: str | None = config.get("since_date") or None
    if since_date:
        logger.info("Date filter active: only processing emails since %s", since_date)

    # Fetch emails
    link_keywords: list[str] = config.get("link_keywords") or []
    emails = graph.fetch_emails_with_attachments(
        whitelisted_senders=whitelisted_senders,
        since=since_date,
        link_keywords=link_keywords,
        subject_keywords=subject_keywords,
    )

    logger.info("Fetched %d email(s) with qualifying attachments.", len(emails))

    documents_found = len(emails)
    documents_new = 0
    invoices_saved = 0
    error_message = None

    # Build a pipeline config that includes sender_suppliers if provided
    pipeline_config = {}
    if config.get("invoices"):
        pipeline_config["invoices"] = config["invoices"]
    if config.get("classifier"):
        pipeline_config["classifier"] = config["classifier"]

    try:
        for email in emails:
            source_id = email.internet_message_id or email.email_id
            if db.is_document_processed(data_dir, source_name, source_id):
                logger.debug("Document %s already processed, skipping.", source_id)
                continue

            documents_new += 1
            received_dt = email.received_datetime
            year = received_dt.year
            month = received_dt.month

            for attachment in email.attachments:
                try:
                    result = process_attachment(
                        attachment=attachment,
                        sender=email.sender,
                        received_at=email.received_at,
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
                    if result.get("status") == "invoice":
                        invoices_saved += 1
                except Exception as e:
                    logger.error(
                        "Failed to process attachment %s from %s: %s",
                        attachment.name, email.sender, e, exc_info=True,
                    )

            db.mark_document_processed(
                data_dir,
                source_name=source_name,
                source_id=source_id,
                sender=email.sender,
                subject=email.subject,
                received_at=email.received_at,
            )

    except Exception as e:
        error_message = str(e)
        logger.error("Source %s failed: %s", source_name, e, exc_info=True)

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

    logger.info("Poll complete. %d new document(s), %d invoice(s) stored.", documents_new, invoices_saved)
    logger.info("========== %s: POLL END ==========\n", source_name)
