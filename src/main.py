"""
Invoice Bot - Main entry point.

Schedules two jobs:
  1. Poll inbox every N minutes (default: 60) and upload confirmed invoices to OneDrive.
  2. On the 1st of each month, build an Excel summary and upload it to OneDrive.

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
from onedrive_uploader import upload_attachment
from pipeline import process_attachment
from poller import GraphClient
from utils import DEFAULT_DATA_DIR, load_config, setup_logging

logger = logging.getLogger("main")


def poll_inbox(config: dict) -> None:
    """Fetch new invoice emails and upload attachments to OneDrive."""
    logger.info("========== POLL START ==========")
    logger.info("Poll triggered at %s (UTC)", datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))

    data_dir = os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)

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

        # Mark after all attachments are processed so a crash mid-email
        # allows the email to be retried on the next poll.
        db.mark_email_processed(
            data_dir,
            email_id=email.email_id,
            sender=email.sender,
            subject=email.subject,
            received_at=email.received_at,
        )

    logger.info("Poll complete. %d new invoice(s) stored.", new_count)
    logger.info("========== POLL END ==========\n")


# ---------------------------------------------------------------------------
# Monthly Excel report job
# ---------------------------------------------------------------------------

def send_report(config: dict) -> None:
    """Build a monthly Excel summary and upload it to OneDrive."""
    now = datetime.now(tz=timezone.utc)
    report_year, report_month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)

    logger.info("========== REPORT START ==========")
    logger.info("Monthly Excel report triggered for %d/%02d", report_year, report_month)

    data_dir = os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)

    if db.has_monthly_report_been_sent(data_dir, report_year, report_month):
        logger.info("Report for %d/%02d already done. Skipping.", report_year, report_month)
        return

    invoices = db.get_unreported_invoices(data_dir, report_year, report_month)

    if not invoices:
        logger.info("No invoices for %d/%02d — skipping Excel.", report_year, report_month)
        db.save_monthly_report(data_dir, report_year, report_month)
        return

    client_id: str = config["microsoft"]["client_id"]
    root_folder_name: str = config["onedrive"]["folder_name"]
    excel_filename = f"{report_year}-{report_month:02d}_summary.xlsx"

    try:
        excel_bytes = build_monthly_excel(invoices, report_year, report_month)
        _, excel_link = upload_attachment(
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
        logger.info("Excel summary uploaded to OneDrive: %s", excel_link)
    except Exception as e:
        logger.error("Failed to build/upload Excel summary: %s", e, exc_info=True)
        return

    db.mark_invoices_reported(data_dir, [inv["id"] for inv in invoices])
    db.save_monthly_report(data_dir, report_year, report_month)

    logger.info(
        "Report done: %d invoice(s) marked reported for %d/%02d.",
        len(invoices), report_year, report_month,
    )
    logger.info("========== REPORT END ==========\n")


def main() -> None:
    # Bootstrap logging before anything else so all startup messages are captured
    data_dir = os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)
    os.makedirs(data_dir, exist_ok=True)

    config = load_config()
    log_level = config.get("logging", {}).get("log_level", "INFO")
    setup_logging(data_dir=data_dir, log_level=log_level)

    logger.info("Invoice Bot starting up")

    # Initialize DB
    db.init_db(data_dir)

    # Validate required config fields (secrets may come from env vars via load_config)
    _placeholders = {"YOUR_CLIENT_ID_HERE", "YOUR_FOLDER_NAME_HERE", "your-client-id"}
    required = [
        ("microsoft", "client_id"),
        ("onedrive", "folder_name"),
    ]
    for section, key in required:
        value = config.get(section, {}).get(key)
        if not value or value in _placeholders:
            logger.error(
                "Missing or unconfigured value: %s.%s — set it in config.yaml or via environment variable",
                section, key,
            )
            sys.exit(1)

    poll_interval = config.get("schedule", {}).get("poll_interval_minutes", 60)
    report_day    = config.get("schedule", {}).get("report_day_of_month", 1)
    report_hour   = config.get("schedule", {}).get("report_hour", 8)

    scheduler = BlockingScheduler(timezone="UTC")

    scheduler.add_job(
        poll_inbox,
        trigger=IntervalTrigger(minutes=poll_interval),
        args=[config],
        id="poll_inbox",
        name="Poll inbox for new invoices",
        next_run_time=datetime.now(tz=timezone.utc),  # run immediately on start
    )

    scheduler.add_job(
        send_report,
        trigger=CronTrigger(day=report_day, hour=report_hour, minute=0),
        args=[config],
        id="monthly_report",
        name="Upload monthly Excel summary to OneDrive",
    )

    logger.info(
        "Scheduler started. Polling every %d min. Monthly Excel on day %d at %02d:00 UTC.",
        poll_interval, report_day, report_hour,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
