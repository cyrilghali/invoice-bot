"""
SQLite database layer for deduplication and invoice tracking.
"""

import contextlib
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def get_connection(data_dir: str) -> sqlite3.Connection:
    db_path = Path(data_dir) / "invoices.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextlib.contextmanager
def _connect(data_dir: str):
    """Context manager that opens and auto-closes a DB connection."""
    conn = get_connection(data_dir)
    try:
        yield conn
    finally:
        conn.close()


def init_db(data_dir: str) -> None:
    """Create tables if they don't exist, and run migrations for existing DBs."""
    with _connect(data_dir) as conn:
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
        migrations = [
            ("invoice_date", "TEXT"), ("supplier", "TEXT"),
            ("amount_ht", "REAL"), ("amount_ttc", "REAL"),
            ("amount_tva", "REAL"), ("currency", "TEXT"),
        ]
        for col_name, col_type in migrations:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE invoices ADD COLUMN {col_name} {col_type}")
                conn.commit()
                logger.info("Migration: added %s column to invoices table", col_name)

        logger.info("Database initialized at %s", Path(data_dir) / "invoices.db")


def is_email_processed(data_dir: str, email_id: str) -> bool:
    with _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_emails WHERE email_id = ?", (email_id,)
        ).fetchone()
        return row is not None


def mark_email_processed(
    data_dir: str,
    email_id: str,
    sender: str,
    subject: str,
    received_at: str,
) -> None:
    with _connect(data_dir) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO processed_emails
                (email_id, processed_at, sender, subject, received_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (email_id, datetime.now(timezone.utc).isoformat(), sender, subject, received_at),
        )
        conn.commit()
        logger.info("Email marked as processed: id=%s sender=%s subject=%r", email_id, sender, subject)


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
    with _connect(data_dir) as conn:
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


def get_unreported_invoices(data_dir: str, year: int, month: int) -> list[dict]:
    """Return all invoices for a given year/month that have not been reported yet."""
    with _connect(data_dir) as conn:
        rows = conn.execute(
            """
            SELECT * FROM invoices
            WHERE year = ? AND month = ? AND reported = 0
            ORDER BY COALESCE(invoice_date, received_at) ASC
            """,
            (year, month),
        ).fetchall()
        result = [dict(row) for row in rows]
        logger.info("Queried unreported invoices: year=%d month=%d count=%d", year, month, len(result))
        return result


def mark_invoices_reported(data_dir: str, invoice_ids: list[int]) -> None:
    with _connect(data_dir) as conn:
        conn.executemany(
            "UPDATE invoices SET reported = 1 WHERE id = ?",
            [(i,) for i in invoice_ids],
        )
        conn.commit()
        logger.info("Marked %d invoice(s) as reported.", len(invoice_ids))


def save_monthly_report(data_dir: str, year: int, month: int) -> None:
    with _connect(data_dir) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO monthly_reports (year, month, sent_at) VALUES (?, ?, ?)",
            (year, month, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def has_monthly_report_been_sent(data_dir: str, year: int, month: int) -> bool:
    with _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT 1 FROM monthly_reports WHERE year = ? AND month = ?",
            (year, month),
        ).fetchone()
        return row is not None
