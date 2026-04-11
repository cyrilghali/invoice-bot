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
    conn.execute("PRAGMA busy_timeout=5000")
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
            CREATE TABLE IF NOT EXISTS processed_documents (
                source_name     TEXT NOT NULL,
                source_id       TEXT NOT NULL,
                processed_at    TEXT NOT NULL,
                sender          TEXT NOT NULL,
                subject         TEXT,
                received_at     TEXT,
                PRIMARY KEY (source_name, source_id)
            );

            CREATE TABLE IF NOT EXISTS invoices (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id        TEXT,
                source_name     TEXT,
                source_document_id TEXT,
                filename        TEXT NOT NULL,
                drive_file_id   TEXT,
                drive_web_link  TEXT,
                sender          TEXT NOT NULL,
                received_at     TEXT NOT NULL,
                year            INTEGER NOT NULL,
                month           INTEGER NOT NULL,
                reported        INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS monthly_reports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                year        INTEGER NOT NULL,
                month       INTEGER NOT NULL,
                sent_at     TEXT NOT NULL,
                UNIQUE(year, month)
            );

            CREATE TABLE IF NOT EXISTS source_runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                source_name     TEXT NOT NULL,
                started_at      TEXT NOT NULL,
                finished_at     TEXT,
                status          TEXT NOT NULL DEFAULT 'running',
                documents_found INTEGER DEFAULT 0,
                documents_new   INTEGER DEFAULT 0,
                invoices_saved  INTEGER DEFAULT 0,
                error_message   TEXT
            );
        """)
        conn.commit()

        # Migrations: add new columns to invoices if they don't exist yet
        existing_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(invoices)").fetchall()
        }
        migrations = [
            ("invoice_date", "TEXT"), ("supplier", "TEXT"),
            ("amount_ht", "REAL"), ("amount_ttc", "REAL"),
            ("amount_tva", "REAL"), ("currency", "TEXT"),
            ("source_name", "TEXT"), ("source_document_id", "TEXT"),
            ("content_hash", "TEXT"), ("entity", "TEXT"),
        ]
        for col_name, col_type in migrations:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE invoices ADD COLUMN {col_name} {col_type}")
                conn.commit()
                logger.info("Migration: added %s column to invoices table", col_name)

        # Migrate processed_emails → processed_documents (one-time)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "processed_emails" in tables:
            conn.execute("""
                INSERT OR IGNORE INTO processed_documents
                    (source_name, source_id, processed_at, sender, subject, received_at)
                SELECT 'email', email_id, processed_at, sender, subject, received_at
                FROM processed_emails
            """)
            # Backfill source_name/source_document_id on existing invoices
            conn.execute("""
                UPDATE invoices
                SET source_name = 'email', source_document_id = email_id
                WHERE source_name IS NULL AND email_id IS NOT NULL
            """)
            conn.execute("DROP TABLE processed_emails")
            conn.commit()
            logger.info("Migration: migrated processed_emails → processed_documents and dropped old table")

        logger.info("Database initialized at %s", Path(data_dir) / "invoices.db")


# ---------------------------------------------------------------------------
# Document processing deduplication
# ---------------------------------------------------------------------------

def is_document_processed(data_dir: str, source_name: str, source_id: str) -> bool:
    with _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_documents WHERE source_name = ? AND source_id = ?",
            (source_name, source_id),
        ).fetchone()
        return row is not None


def get_invoice_by_source_document_id(
    data_dir: str, source_name: str, source_document_id: str
) -> dict | None:
    """Look up the invoice row for a previously-processed source document.

    Used when a source wants to show the user where the existing file lives
    (filename, OneDrive link) instead of silently skipping a duplicate.
    """
    with _connect(data_dir) as conn:
        row = conn.execute(
            """
            SELECT * FROM invoices
            WHERE source_name = ? AND source_document_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (source_name, source_document_id),
        ).fetchone()
        return dict(row) if row else None


def forget_document(
    data_dir: str, source_name: str, source_id: str
) -> None:
    """Drop the dedup row and any unreported invoice linked to it.

    Reported invoices are left intact so monthly reports stay reproducible —
    we want re-processing to create a fresh row rather than mutate history.
    """
    with _connect(data_dir) as conn:
        conn.execute(
            "DELETE FROM processed_documents WHERE source_name = ? AND source_id = ?",
            (source_name, source_id),
        )
        conn.execute(
            """
            DELETE FROM invoices
            WHERE source_name = ? AND source_document_id = ? AND reported = 0
            """,
            (source_name, source_id),
        )
        conn.commit()
        logger.info("Forgot document: source=%s id=%s", source_name, source_id)


def mark_document_processed(
    data_dir: str,
    source_name: str,
    source_id: str,
    sender: str,
    subject: str,
    received_at: str,
) -> None:
    with _connect(data_dir) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO processed_documents
                (source_name, source_id, processed_at, sender, subject, received_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (source_name, source_id, datetime.now(timezone.utc).isoformat(), sender, subject, received_at),
        )
        conn.commit()
        logger.info("Document marked as processed: source=%s id=%s sender=%s", source_name, source_id, sender)


# ---------------------------------------------------------------------------
# Content-hash cross-source dedup
# ---------------------------------------------------------------------------

def is_content_already_invoiced(data_dir: str, content_hash: str) -> dict | None:
    """Check if a file with this content hash was already saved as an invoice.

    Returns the existing invoice row (as dict) if found, None otherwise.
    This catches duplicates across sources (e.g. same PDF via email AND WhatsApp).
    """
    with _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT * FROM invoices WHERE content_hash = ? LIMIT 1",
            (content_hash,),
        ).fetchone()
        if row:
            result = dict(row)
            logger.info(
                "Content hash %s… already invoiced: id=%d source=%s filename=%r",
                content_hash[:12], result["id"], result.get("source_name"), result["filename"],
            )
            return result
        return None


# ---------------------------------------------------------------------------
# Invoice CRUD
# ---------------------------------------------------------------------------

def save_invoice(
    data_dir: str,
    filename: str,
    sender: str,
    received_at: str,
    year: int,
    month: int,
    source_name: str | None = None,
    source_document_id: str | None = None,
    email_id: str | None = None,
    drive_file_id: str | None = None,
    drive_web_link: str | None = None,
    invoice_date: str | None = None,
    supplier: str | None = None,
    entity: str | None = None,
    amount_ht: float | None = None,
    amount_ttc: float | None = None,
    amount_tva: float | None = None,
    currency: str | None = None,
    content_hash: str | None = None,
) -> None:
    with _connect(data_dir) as conn:
        cursor = conn.execute(
            """
            INSERT INTO invoices
                (email_id, source_name, source_document_id, filename,
                 drive_file_id, drive_web_link,
                 sender, received_at, year, month, invoice_date, supplier,
                 entity, amount_ht, amount_ttc, amount_tva, currency, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email_id or source_document_id or "",
                source_name,
                source_document_id,
                filename,
                drive_file_id,
                drive_web_link,
                sender,
                received_at,
                year,
                month,
                invoice_date,
                supplier,
                entity,
                amount_ht,
                amount_ttc,
                amount_tva,
                currency,
                content_hash,
            ),
        )
        conn.commit()
        logger.info(
            "Invoice saved: id=%d filename=%r year=%d month=%d supplier=%r entity=%r "
            "invoice_date=%r amount_ht=%s amount_ttc=%s currency=%r source=%s",
            cursor.lastrowid, filename, year, month, supplier, entity,
            invoice_date, amount_ht, amount_ttc, currency, source_name,
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


# ---------------------------------------------------------------------------
# Monthly report tracking
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Source run tracking
# ---------------------------------------------------------------------------

def save_source_run(
    data_dir: str,
    source_name: str,
    started_at: str,
    finished_at: str,
    status: str,
    documents_found: int = 0,
    documents_new: int = 0,
    invoices_saved: int = 0,
    error_message: str | None = None,
) -> None:
    with _connect(data_dir) as conn:
        conn.execute(
            """
            INSERT INTO source_runs
                (source_name, started_at, finished_at, status,
                 documents_found, documents_new, invoices_saved, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source_name, started_at, finished_at, status,
             documents_found, documents_new, invoices_saved, error_message),
        )
        conn.commit()


def get_recent_runs(data_dir: str, source_name: str | None = None, limit: int = 20) -> list[dict]:
    with _connect(data_dir) as conn:
        if source_name:
            rows = conn.execute(
                "SELECT * FROM source_runs WHERE source_name = ? ORDER BY started_at DESC LIMIT ?",
                (source_name, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM source_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
