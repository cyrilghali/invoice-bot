"""
Invoice Bot - Main entry point.

Schedules two jobs:
  1. Poll inbox every N minutes (default: 60)
  2. Build a monthly report draft on the 1st of each month at a configured hour

All configuration is read from config.yaml (mounted at /app/config.yaml).
"""

import logging
import os
import sys
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import db
from excel_exporter import build_monthly_excel
from onedrive_uploader import get_month_folder_link, upload_attachment  # upload_attachment used for Excel summary
from pipeline import process_attachment
from poller import GraphClient
from reporter import create_monthly_report_draft
from utils import load_config, setup_logging

logger = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Poll job
# ---------------------------------------------------------------------------

def poll_inbox(config: dict) -> None:
    """Fetch new invoice emails and upload attachments to OneDrive."""
    logger.info("========== POLL START ==========")
    logger.info("Poll triggered at %s (UTC)", datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))

    data_dir = os.environ.get("DATA_DIR", "/app/data")

    client_id: str = config["microsoft"]["client_id"]
    root_folder_name: str = config["onedrive"]["folder_name"]

    # Optional whitelist — if absent/empty, all senders are scanned
    raw_senders: list[str] = (config.get("invoices") or {}).get("whitelisted_senders") or []
    whitelisted_senders = [s.lower().strip() for s in raw_senders] if raw_senders else None
    if whitelisted_senders:
        logger.info("Sender whitelist active: %d senders", len(whitelisted_senders))
    else:
        logger.info("No sender whitelist — scanning all inbox attachments (AI classifier active)")

    # Optional subject keyword filter
    raw_subject_kws: list[str] = (config.get("invoices") or {}).get("subject_keywords") or []
    subject_keywords = [k.lower().strip() for k in raw_subject_kws] if raw_subject_kws else None
    if subject_keywords:
        logger.info("Subject keyword filter active: %d keywords", len(subject_keywords))
    else:
        logger.info("No subject keyword filter — all subjects accepted")

    graph = GraphClient(client_id)

    # Optional date floor — ignore emails older than this date
    since_date: str | None = (config.get("debug") or {}).get("since_date") or None
    if since_date:
        logger.info("Date filter active: only processing emails since %s", since_date)
    else:
        logger.info("No date filter — processing all emails")

    # Fetch emails — subject filter pre-screens, AI classifier does final check
    link_keywords: list[str] = config.get("link_detection", {}).get("keywords", [])
    emails = graph.fetch_emails_with_attachments(
        whitelisted_senders=whitelisted_senders,
        since=since_date,
        link_keywords=link_keywords,
        subject_keywords=subject_keywords,
    )

    logger.info("Fetched %d email(s) with qualifying attachments.", len(emails))
    new_count = 0
    for email in emails:
        if db.is_email_processed(data_dir, email.email_id):
            logger.debug("Email %s already processed, skipping.", email.email_id)
            continue

        received_dt = email.received_datetime
        year = received_dt.year
        month = received_dt.month

        db.mark_email_processed(
            data_dir,
            email_id=email.email_id,
            sender=email.sender,
            subject=email.subject,
            received_at=email.received_at,
        )

        for attachment in email.attachments:
            try:
                status = process_attachment(
                    attachment=attachment,
                    email=email,
                    year=year,
                    month=month,
                    config=config,
                    data_dir=data_dir,
                    client_id=client_id,
                    root_folder_name=root_folder_name,
                    dry_run=False,
                )
                if status == "invoice":
                    new_count += 1
            except Exception as e:
                logger.error(
                    "Failed to process attachment %s from %s: %s",
                    attachment.name,
                    email.sender,
                    e,
                    exc_info=True,
                )

    logger.info("Poll complete. %d new invoice(s) stored.", new_count)
    logger.info("========== POLL END ==========\n")


# ---------------------------------------------------------------------------
# Monthly report job
# ---------------------------------------------------------------------------

def send_report(config: dict) -> None:
    """Build the monthly report draft for the previous month."""
    now = datetime.now(tz=timezone.utc)

    # Report covers the PREVIOUS month
    if now.month == 1:
        report_year, report_month = now.year - 1, 12
    else:
        report_year, report_month = now.year, now.month - 1

    logger.info("========== REPORT START ==========")
    logger.info(
        "Monthly report triggered for %d/%02d", report_year, report_month
    )

    data_dir = os.environ.get("DATA_DIR", "/app/data")

    accountant_email: str = config["accountant"]["email"]

    # Avoid creating duplicate drafts
    if db.has_monthly_report_been_sent(data_dir, report_year, report_month):
        logger.info(
            "Report for %d/%02d already created. Skipping.", report_year, report_month
        )
        return

    invoices = db.get_unreported_invoices(data_dir, report_year, report_month)

    if not invoices:
        logger.info(
            "No invoices found for %d/%02d. No draft created.", report_year, report_month
        )
        db.save_monthly_report(data_dir, report_year, report_month)
        return

    client_id: str = config["microsoft"]["client_id"]
    root_folder_name: str = config["onedrive"]["folder_name"]

    # Get OneDrive folder link for the month
    try:
        drive_folder_link = get_month_folder_link(
            client_id, root_folder_name, report_year, report_month
        )
    except Exception as e:
        logger.warning("Could not get OneDrive folder link: %s", e)
        drive_folder_link = ""

    graph = GraphClient(client_id)

    # --- Build Excel summary ---
    excel_bytes: bytes | None = None
    excel_filename = f"{report_year}-{report_month:02d}_summary.xlsx"
    try:
        excel_bytes = build_monthly_excel(invoices, report_year, report_month)
        # Upload to OneDrive (same month folder as the invoices)
        excel_drive_id, excel_drive_link = upload_attachment(
            client_id=client_id,
            root_folder_name=root_folder_name,
            attachment_name=excel_filename,
            attachment_bytes=excel_bytes,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            sender="summary",
            received_at=f"{report_year}-{report_month:02d}-01T00:00:00Z",
            year=report_year,
            month=report_month,
        )
        logger.info("Excel summary uploaded to OneDrive: %s", excel_drive_link)
    except Exception as e:
        logger.warning("Could not build/upload Excel summary: %s", e, exc_info=True)
        excel_bytes = None

    draft_id = create_monthly_report_draft(
        graph_client=graph,
        accountant_email=accountant_email,
        invoices=invoices,
        year=report_year,
        month=report_month,
        drive_folder_link=drive_folder_link,
        attachment_bytes_map={},  # We don't re-download from OneDrive; rely on links
        excel_bytes=excel_bytes,
    )

    # Mark invoices and the month as reported
    invoice_ids = [inv["id"] for inv in invoices]
    db.mark_invoices_reported(data_dir, invoice_ids)
    db.save_monthly_report(data_dir, report_year, report_month)

    logger.info("Monthly report draft created successfully (id=%s).", draft_id)
    logger.info("========== REPORT END ==========\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Bootstrap logging before anything else so all startup messages are captured
    data_dir = os.environ.get("DATA_DIR", "/app/data")
    os.makedirs(data_dir, exist_ok=True)

    config = load_config()
    log_level = config.get("logging", {}).get("log_level", "INFO")
    setup_logging(data_dir=data_dir, log_level=log_level)

    logger.info("Invoice Bot starting up")

    # Initialize DB
    db.init_db(data_dir)

    # Validate required config fields
    required = [
        ("microsoft", "client_id"),
        ("onedrive", "folder_name"),
        ("accountant", "email"),
    ]
    for section, key in required:
        value = config.get(section, {}).get(key)
        if not value or value in ("YOUR_CLIENT_ID_HERE", "YOUR_FOLDER_NAME_HERE"):
            logger.error(
                "Missing or unconfigured value: %s.%s in config.yaml", section, key
            )
            sys.exit(1)

    poll_interval = config.get("schedule", {}).get("poll_interval_minutes", 60)
    report_day = config.get("schedule", {}).get("report_day_of_month", 1)
    report_hour = config.get("schedule", {}).get("report_hour", 8)

    scheduler = BlockingScheduler(timezone="UTC")

    # Hourly inbox poll
    scheduler.add_job(
        poll_inbox,
        trigger=IntervalTrigger(minutes=poll_interval),
        args=[config],
        id="poll_inbox",
        name="Poll inbox for new invoices",
        next_run_time=datetime.now(tz=timezone.utc),  # run immediately on start
    )

    # Monthly report (1st of every month at configured hour)
    scheduler.add_job(
        send_report,
        trigger=CronTrigger(day=report_day, hour=report_hour, minute=0),
        args=[config],
        id="monthly_report",
        name="Build monthly invoice report draft",
    )

    logger.info(
        "Scheduler started. Polling every %d min. Report draft on day %d at %02d:00 UTC.",
        poll_interval,
        report_day,
        report_hour,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
