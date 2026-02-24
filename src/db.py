"""
SQLite database layer for deduplication and invoice tracking.
"""

import sqlite3
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def get_connection(data_dir: str) -> sqlite3.Connection:
    db_path = Path(data_dir) / "invoices.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(data_dir: str) -> None:
    """Create tables if they don't exist, and run migrations for existing DBs."""
    conn = get_connection(data_dir)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS processed_emails (
                email_id        TEXT PRIMARY KEY,
                processed_at    TEXT NOT NULL,
                sender          TEXT NOT NULL,
                subject         TEXT,
                received_at     TEXT
            );

            CREATE TABLE IF NOT EXISTS invoices (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id        TEXT NOT NULL,
                filename        TEXT NOT NULL,
                drive_file_id   TEXT,
                drive_web_link  TEXT,
                sender          TEXT NOT NULL,
                received_at     TEXT NOT NULL,
                year            INTEGER NOT NULL,
                month           INTEGER NOT NULL,
                reported        INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (email_id) REFERENCES processed_emails(email_id)
            );

            CREATE TABLE IF NOT EXISTS monthly_reports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                year        INTEGER NOT NULL,
                month       INTEGER NOT NULL,
                sent_at     TEXT NOT NULL,
                UNIQUE(year, month)
            );

        """)
        conn.commit()

        # Migrations: add new columns if they don't exist yet
        existing_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(invoices)").fetchall()
        }
        if "invoice_date" not in existing_cols:
            conn.execute("ALTER TABLE invoices ADD COLUMN invoice_date TEXT")
            conn.commit()
            logger.info("Migration: added invoice_date column to invoices table")
        if "supplier" not in existing_cols:
            conn.execute("ALTER TABLE invoices ADD COLUMN supplier TEXT")
            conn.commit()
            logger.info("Migration: added supplier column to invoices table")
        if "amount_ht" not in existing_cols:
            conn.execute("ALTER TABLE invoices ADD COLUMN amount_ht REAL")
            conn.commit()
            logger.info("Migration: added amount_ht column to invoices table")
        if "amount_ttc" not in existing_cols:
            conn.execute("ALTER TABLE invoices ADD COLUMN amount_ttc REAL")
            conn.commit()
            logger.info("Migration: added amount_ttc column to invoices table")
        if "amount_tva" not in existing_cols:
            conn.execute("ALTER TABLE invoices ADD COLUMN amount_tva REAL")
            conn.commit()
            logger.info("Migration: added amount_tva column to invoices table")
        if "currency" not in existing_cols:
            conn.execute("ALTER TABLE invoices ADD COLUMN currency TEXT")
            conn.commit()
            logger.info("Migration: added currency column to invoices table")

        logger.info("Database initialized at %s", Path(data_dir) / "invoices.db")
    finally:
        conn.close()


def is_email_processed(data_dir: str, email_id: str) -> bool:
    conn = get_connection(data_dir)
    try:
        row = conn.execute(
            "SELECT 1 FROM processed_emails WHERE email_id = ?", (email_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_email_processed(
    data_dir: str,
    email_id: str,
    sender: str,
    subject: str,
    received_at: str,
) -> None:
    conn = get_connection(data_dir)
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO processed_emails
                (email_id, processed_at, sender, subject, received_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (email_id, datetime.utcnow().isoformat(), sender, subject, received_at),
        )
        conn.commit()
        logger.info("Email marked as processed: id=%s sender=%s subject=%r", email_id, sender, subject)
    finally:
        conn.close()


def save_invoice(
    data_dir: str,
    email_id: str,
    filename: str,
    sender: str,
    received_at: str,
    year: int,
    month: int,
    drive_file_id: str | None = None,
    drive_web_link: str | None = None,
    invoice_date: str | None = None,
    supplier: str | None = None,
    amount_ht: float | None = None,
    amount_ttc: float | None = None,
    amount_tva: float | None = None,
    currency: str | None = None,
) -> None:
    conn = get_connection(data_dir)
    try:
        cursor = conn.execute(
            """
            INSERT INTO invoices
                (email_id, filename, drive_file_id, drive_web_link,
                 sender, received_at, year, month, invoice_date, supplier,
                 amount_ht, amount_ttc, amount_tva, currency)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email_id,
                filename,
                drive_file_id,
                drive_web_link,
                sender,
                received_at,
                year,
                month,
                invoice_date,
                supplier,
                amount_ht,
                amount_ttc,
                amount_tva,
                currency,
            ),
        )
        conn.commit()
        logger.info(
            "Invoice saved: id=%d filename=%r year=%d month=%d supplier=%r "
            "invoice_date=%r amount_ht=%s amount_ttc=%s currency=%r",
            cursor.lastrowid, filename, year, month, supplier,
            invoice_date, amount_ht, amount_ttc, currency,
        )
    finally:
        conn.close()


def get_unreported_invoices(data_dir: str, year: int, month: int) -> list[dict]:
    """Return all invoices for a given year/month that have not been reported yet."""
    conn = get_connection(data_dir)
    try:
        rows = conn.execute(
            """
            SELECT * FROM invoices
            WHERE year = ? AND month = ? AND reported = 0
            ORDER BY COALESCE(invoice_date, received_at) ASC
            """,
            (year, month),
        ).fetchall()
        result = [dict(row) for row in rows]
        logger.info(
            "Queried unreported invoices: year=%d month=%d count=%d",
            year, month, len(result),
        )
        return result
    finally:
        conn.close()


def mark_invoices_reported(data_dir: str, invoice_ids: list[int]) -> None:
    conn = get_connection(data_dir)
    try:
        conn.executemany(
            "UPDATE invoices SET reported = 1 WHERE id = ?",
            [(i,) for i in invoice_ids],
        )
        conn.commit()
        logger.info("Marked %d invoice(s) as reported: ids=%s", len(invoice_ids), invoice_ids)
    finally:
        conn.close()


def save_monthly_report(data_dir: str, year: int, month: int) -> None:
    conn = get_connection(data_dir)
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO monthly_reports (year, month, sent_at)
            VALUES (?, ?, ?)
            """,
            (year, month, datetime.utcnow().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def has_monthly_report_been_sent(data_dir: str, year: int, month: int) -> bool:
    conn = get_connection(data_dir)
    try:
        row = conn.execute(
            "SELECT 1 FROM monthly_reports WHERE year = ? AND month = ?",
            (year, month),
        ).fetchone()
        return row is not None
    finally:
        conn.close()



def get_invoice_by_drive_id(data_dir: str, drive_file_id: str) -> dict | None:
    """Return the invoice row for the given OneDrive file ID, or None if not tracked."""
    conn = get_connection(data_dir)
    try:
        row = conn.execute(
            "SELECT * FROM invoices WHERE drive_file_id = ?",
            (drive_file_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_invoice_by_drive_id(
    data_dir: str,
    drive_file_id: str,
    filename: str,
    drive_web_link: str,
    year: int,
    month: int,
    invoice_date: str | None,
    supplier: str | None,
) -> bool:
    """
    Update a tracked invoice row identified by its OneDrive file ID.

    Called by the remediation script after a file has been renamed/moved on
    OneDrive so the DB stays consistent with the new filename, folder, date
    and supplier.

    Returns True if a matching row was found and updated, False otherwise
    (e.g. the file was uploaded outside the bot and has no DB record).
    """
    conn = get_connection(data_dir)
    try:
        cursor = conn.execute(
            """
            UPDATE invoices
               SET filename       = ?,
                   drive_web_link = ?,
                   year           = ?,
                   month          = ?,
                   invoice_date   = ?,
                   supplier       = ?
             WHERE drive_file_id  = ?
            """,
            (filename, drive_web_link, year, month, invoice_date, supplier, drive_file_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()



