"""
Backfill script — process historical invoice emails.

Walks through the inbox from a given date, finds invoice emails using the same
whitelist + subject keyword + AI classifier logic as the live bot, and uploads
confirmed invoices to OneDrive. Already-processed emails are skipped via SQLite.

Does NOT send any email to the accountant or Telegram notifications.

Usage:
    # Process all emails since the date set in config (debug.since_date)
    docker exec -it invoice-bot python src/backfill.py

    # Process emails since a specific date
    docker exec -it invoice-bot python src/backfill.py --since 2025-01-01

    # Dry run: log what would be processed, upload nothing, write nothing to DB
    docker exec -it invoice-bot python src/backfill.py --dry-run
    docker exec -it invoice-bot python src/backfill.py --since 2025-06-01 --dry-run
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

import db
from onedrive_uploader import build_filename
from pipeline import process_attachment
from poller import GraphClient
from utils import load_config, setup_logging

logger = logging.getLogger("backfill")



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill historical invoice emails.")
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        default=None,
        help=(
            "Only process emails received on or after this date. "
            "Defaults to debug.since_date in config.yaml."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be processed without uploading or writing to the DB.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    data_dir = os.environ.get("DATA_DIR", "/app/data")
    os.makedirs(data_dir, exist_ok=True)

    config = load_config()
    log_level = config.get("logging", {}).get("log_level", "INFO")
    setup_logging(data_dir=data_dir, log_level=log_level)

    logger.info("========== BACKFILL START ==========")
    db.init_db(data_dir)

    client_id: str = config["microsoft"]["client_id"]
    root_folder_name: str = config["onedrive"]["folder_name"]

    # Whitelist + subject keywords — same logic as main.py
    raw_senders: list[str] = (config.get("invoices") or {}).get("whitelisted_senders") or []
    whitelisted_senders = [s.lower().strip() for s in raw_senders] if raw_senders else None
    if whitelisted_senders:
        logger.info("Sender whitelist active: %d senders", len(whitelisted_senders))

    raw_subject_kws: list[str] = (config.get("invoices") or {}).get("subject_keywords") or []
    subject_keywords = [k.lower().strip() for k in raw_subject_kws] if raw_subject_kws else None
    if subject_keywords:
        logger.info("Subject keyword filter active: %d keywords", len(subject_keywords))

    # Internal senders — same as main.py
    raw_internal: list[str] = (config.get("invoices") or {}).get("internal_senders") or []
    internal_senders: set[str] = {s.lower().strip() for s in raw_internal}
    if internal_senders:
        logger.info("Internal senders configured: %s", internal_senders)

    # Resolve --since: CLI arg > config debug.since_date > None (all history)
    since_iso: str | None = None
    if args.since:
        try:
            dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            since_iso = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            logger.info("Date filter (CLI): emails since %s", args.since)
        except ValueError:
            logger.error("Invalid --since format %r (expected YYYY-MM-DD)", args.since)
            sys.exit(1)
    else:
        since_iso = (config.get("debug") or {}).get("since_date") or None
        if since_iso:
            logger.info("Date filter (config): emails since %s", since_iso)
        else:
            logger.info("No date filter — processing full inbox history")

    if args.dry_run:
        logger.info("DRY RUN — nothing will be uploaded or written to DB.")

    graph = GraphClient(client_id)
    link_keywords: list[str] = config.get("link_detection", {}).get("keywords", [])

    logger.info("Fetching emails from inbox (this may take a while)...")
    emails = graph.fetch_emails_with_attachments(
        whitelisted_senders=whitelisted_senders,
        since=since_iso,
        link_keywords=link_keywords,
        subject_keywords=subject_keywords,
        max_results=None,  # paginate through full inbox
    )

    if not emails:
        logger.info("No matching emails found.")
        return

    logger.info("Found %d email(s) with attachments to process.", len(emails))

    already_processed = 0
    new_invoices = 0
    sent_to_review = 0
    errors = 0

    for email in emails:
        if db.is_email_processed(data_dir, email.email_id):
            already_processed += 1
            logger.debug("Already processed: %s (from %s)", email.subject, email.sender)
            continue

        received_dt = email.received_datetime
        year = received_dt.year
        month = received_dt.month

        logger.info(
            "[%s] From: %s | Subject: %r | Attachments: %d",
            received_dt.strftime("%Y-%m-%d"),
            email.sender,
            email.subject,
            len(email.attachments),
        )

        if args.dry_run:
            for att in email.attachments:
                # Dry-run: just log what filename would be built — no Claude call
                filename = build_filename(email.received_at, email.sender, att.name)
                logger.info(
                    "  [DRY RUN] Would process: %s → %s/YYYY/MM/%s",
                    att.name, root_folder_name, filename,
                )
            continue

        # Mark email as processed before uploading (avoids reprocessing on partial failure)
        db.mark_email_processed(
            data_dir,
            email_id=email.email_id,
            sender=email.sender,
            subject=email.subject,
            received_at=email.received_at,
        )

        for att in email.attachments:
            try:
                status = process_attachment(
                    attachment=att,
                    email=email,
                    year=year,
                    month=month,
                    config=config,
                    data_dir=data_dir,
                    client_id=client_id,
                    root_folder_name=root_folder_name,
                    internal_senders=internal_senders,
                    dry_run=False,
                )
                if status == "invoice":
                    new_invoices += 1
                else:
                    sent_to_review += 1
            except Exception as e:
                errors += 1
                logger.error(
                    "  Failed to process %s from %s: %s",
                    att.name, email.sender, e,
                    exc_info=True,
                )

    logger.info("========== BACKFILL COMPLETE ==========")
    logger.info(
        "Summary: emails_found=%d already_processed=%d new_invoices=%d sent_to_review=%d errors=%d%s",
        len(emails),
        already_processed,
        new_invoices,
        sent_to_review,
        errors,
        " [DRY RUN]" if args.dry_run else "",
    )

    print()
    print("=" * 60)
    print("Backfill complete")
    print(f"  Emails found:          {len(emails)}")
    print(f"  Already processed:     {already_processed}")
    print(f"  New invoices uploaded: {new_invoices}")
    print(f"  Sent to _a_verifier:   {sent_to_review}")
    if errors:
        print(f"  Errors:                {errors}")
    if args.dry_run:
        print("  (dry run — nothing was uploaded or written)")
    print("=" * 60)


if __name__ == "__main__":
    main()
