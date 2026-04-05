"""Tests for src/db.py — SQLite operations with real temp databases."""

import sqlite3
from pathlib import Path

import pytest

import db


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_creates_tables(self, tmp_data_dir):
        db.init_db(tmp_data_dir)
        conn = db.get_connection(tmp_data_dir)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "processed_documents" in tables
            assert "invoices" in tables
            assert "monthly_reports" in tables
            assert "source_runs" in tables
        finally:
            conn.close()

    def test_creates_db_file(self, tmp_data_dir):
        db.init_db(tmp_data_dir)
        assert (Path(tmp_data_dir) / "invoices.db").exists()

    def test_idempotent(self, tmp_data_dir):
        db.init_db(tmp_data_dir)
        db.init_db(tmp_data_dir)  # Should not raise

    def test_migration_columns_exist(self, tmp_data_dir):
        db.init_db(tmp_data_dir)
        conn = db.get_connection(tmp_data_dir)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(invoices)").fetchall()}
            for col in ("invoice_date", "supplier", "amount_ht", "amount_ttc",
                        "amount_tva", "currency", "source_name", "source_document_id"):
                assert col in cols, f"Missing column: {col}"
        finally:
            conn.close()

    def test_busy_timeout_set(self, tmp_data_dir):
        conn = db.get_connection(tmp_data_dir)
        try:
            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert timeout == 5000
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Migration from processed_emails
# ---------------------------------------------------------------------------

class TestMigration:
    def test_migrates_processed_emails(self, tmp_data_dir):
        """Simulate an old DB with processed_emails and verify migration."""
        conn = db.get_connection(tmp_data_dir)
        try:
            conn.executescript("""
                CREATE TABLE processed_emails (
                    email_id TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    subject TEXT,
                    received_at TEXT
                );
                CREATE TABLE invoices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email_id TEXT,
                    filename TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    month INTEGER NOT NULL,
                    reported INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE monthly_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    year INTEGER NOT NULL,
                    month INTEGER NOT NULL,
                    sent_at TEXT NOT NULL,
                    UNIQUE(year, month)
                );
            """)
            conn.execute(
                "INSERT INTO processed_emails VALUES (?, ?, ?, ?, ?)",
                ("old-email-1", "2025-01-01T00:00:00Z", "a@b.com", "Invoice", "2025-01-01T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO invoices (email_id, filename, sender, received_at, year, month) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("old-email-1", "inv.pdf", "a@b.com", "2025-01-01T00:00:00Z", 2025, 1),
            )
            conn.commit()
        finally:
            conn.close()

        db.init_db(tmp_data_dir)

        # Verify processed_emails was dropped
        conn = db.get_connection(tmp_data_dir)
        try:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "processed_emails" not in tables
            assert "processed_documents" in tables

            # Verify data migrated
            row = conn.execute(
                "SELECT * FROM processed_documents WHERE source_name='email' AND source_id='old-email-1'"
            ).fetchone()
            assert row is not None
            assert row["sender"] == "a@b.com"

            # Verify invoices backfilled
            inv = conn.execute("SELECT * FROM invoices WHERE email_id='old-email-1'").fetchone()
            assert inv["source_name"] == "email"
            assert inv["source_document_id"] == "old-email-1"
        finally:
            conn.close()

    def test_migration_idempotent(self, tmp_data_dir):
        """Running init_db twice after migration doesn't duplicate rows."""
        conn = db.get_connection(tmp_data_dir)
        try:
            conn.executescript("""
                CREATE TABLE processed_emails (
                    email_id TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    subject TEXT,
                    received_at TEXT
                );
                CREATE TABLE invoices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email_id TEXT,
                    filename TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    month INTEGER NOT NULL,
                    reported INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE monthly_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    year INTEGER NOT NULL,
                    month INTEGER NOT NULL,
                    sent_at TEXT NOT NULL,
                    UNIQUE(year, month)
                );
            """)
            conn.execute(
                "INSERT INTO processed_emails VALUES (?, ?, ?, ?, ?)",
                ("e1", "2025-01-01T00:00:00Z", "a@b.com", "Sub", "2025-01-01T00:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

        db.init_db(tmp_data_dir)
        db.init_db(tmp_data_dir)  # Second call should not duplicate

        conn = db.get_connection(tmp_data_dir)
        try:
            count = conn.execute("SELECT COUNT(*) FROM processed_documents").fetchone()[0]
            assert count == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Document processing deduplication
# ---------------------------------------------------------------------------

class TestDocumentProcessing:
    def test_not_processed_initially(self, initialized_db):
        assert db.is_document_processed(initialized_db, "email", "doc-001") is False

    def test_mark_and_check(self, initialized_db):
        db.mark_document_processed(
            initialized_db, "email", "doc-001", "sender@test.com", "Test Subject", "2025-01-01T00:00:00Z"
        )
        assert db.is_document_processed(initialized_db, "email", "doc-001") is True

    def test_different_source_names_are_independent(self, initialized_db):
        db.mark_document_processed(
            initialized_db, "email", "id-001", "a@b.com", "Sub", "2025-01-01T00:00:00Z"
        )
        assert db.is_document_processed(initialized_db, "email", "id-001") is True
        assert db.is_document_processed(initialized_db, "cegedim", "id-001") is False

    def test_duplicate_insert_ignored(self, initialized_db):
        db.mark_document_processed(
            initialized_db, "email", "doc-001", "a@b.com", "Sub1", "2025-01-01T00:00:00Z"
        )
        # INSERT OR IGNORE — should not raise
        db.mark_document_processed(
            initialized_db, "email", "doc-001", "a@b.com", "Sub2", "2025-01-02T00:00:00Z"
        )
        assert db.is_document_processed(initialized_db, "email", "doc-001") is True


# ---------------------------------------------------------------------------
# Invoice CRUD
# ---------------------------------------------------------------------------

class TestInvoiceCrud:
    def _save_sample(self, data_dir, source_name="email", source_id="e1",
                     filename="inv.pdf", month=3):
        db.mark_document_processed(
            data_dir, source_name, source_id, "s@t.com", "Sub", "2025-03-01T00:00:00Z"
        )
        db.save_invoice(
            data_dir,
            filename=filename,
            sender="s@t.com",
            received_at="2025-03-01T00:00:00Z",
            year=2025,
            month=month,
            source_name=source_name,
            source_document_id=source_id,
            email_id=source_id if source_name == "email" else None,
            drive_file_id="file-id-1",
            drive_web_link="https://onedrive.example/file1",
            invoice_date="2025-03-01",
            supplier="Acme Corp",
            amount_ht=100.0,
            amount_ttc=120.0,
            amount_tva=20.0,
            currency="EUR",
        )

    def test_save_and_query(self, initialized_db):
        self._save_sample(initialized_db)
        invoices = db.get_unreported_invoices(initialized_db, 2025, 3)
        assert len(invoices) == 1
        inv = invoices[0]
        assert inv["filename"] == "inv.pdf"
        assert inv["supplier"] == "Acme Corp"
        assert inv["amount_ht"] == 100.0
        assert inv["amount_ttc"] == 120.0
        assert inv["amount_tva"] == 20.0
        assert inv["currency"] == "EUR"
        assert inv["source_name"] == "email"
        assert inv["source_document_id"] == "e1"

    def test_unreported_excludes_other_months(self, initialized_db):
        self._save_sample(initialized_db, month=3)
        invoices = db.get_unreported_invoices(initialized_db, 2025, 4)
        assert len(invoices) == 0

    def test_mark_reported(self, initialized_db):
        self._save_sample(initialized_db)
        invoices = db.get_unreported_invoices(initialized_db, 2025, 3)
        ids = [inv["id"] for inv in invoices]
        db.mark_invoices_reported(initialized_db, ids)
        assert db.get_unreported_invoices(initialized_db, 2025, 3) == []

    def test_save_with_null_optional_fields(self, initialized_db):
        db.save_invoice(
            initialized_db,
            filename="bare.pdf",
            sender="s@t.com",
            received_at="2025-04-01T00:00:00Z",
            year=2025,
            month=4,
            source_name="scraper",
            source_document_id="doc-42",
        )
        invoices = db.get_unreported_invoices(initialized_db, 2025, 4)
        assert len(invoices) == 1
        assert invoices[0]["supplier"] is None
        assert invoices[0]["amount_ht"] is None
        assert invoices[0]["email_id"] is None


# ---------------------------------------------------------------------------
# Monthly report tracking
# ---------------------------------------------------------------------------

class TestMonthlyReport:
    def test_not_sent_initially(self, initialized_db):
        assert db.has_monthly_report_been_sent(initialized_db, 2025, 3) is False

    def test_save_and_check(self, initialized_db):
        db.save_monthly_report(initialized_db, 2025, 3)
        assert db.has_monthly_report_been_sent(initialized_db, 2025, 3) is True

    def test_different_month_not_affected(self, initialized_db):
        db.save_monthly_report(initialized_db, 2025, 3)
        assert db.has_monthly_report_been_sent(initialized_db, 2025, 4) is False

    def test_duplicate_insert_ignored(self, initialized_db):
        db.save_monthly_report(initialized_db, 2025, 3)
        db.save_monthly_report(initialized_db, 2025, 3)  # INSERT OR IGNORE
        assert db.has_monthly_report_been_sent(initialized_db, 2025, 3) is True


# ---------------------------------------------------------------------------
# Source run tracking
# ---------------------------------------------------------------------------

class TestSourceRuns:
    def test_save_and_retrieve(self, initialized_db):
        db.save_source_run(
            initialized_db,
            source_name="email",
            started_at="2025-03-15T10:00:00Z",
            finished_at="2025-03-15T10:01:00Z",
            status="ok",
            documents_found=5,
            documents_new=2,
            invoices_saved=1,
        )
        runs = db.get_recent_runs(initialized_db, source_name="email")
        assert len(runs) == 1
        assert runs[0]["source_name"] == "email"
        assert runs[0]["status"] == "ok"
        assert runs[0]["documents_found"] == 5
        assert runs[0]["documents_new"] == 2
        assert runs[0]["invoices_saved"] == 1

    def test_error_run(self, initialized_db):
        db.save_source_run(
            initialized_db,
            source_name="cegedim",
            started_at="2025-03-15T10:00:00Z",
            finished_at="2025-03-15T10:00:05Z",
            status="error",
            error_message="Connection timeout",
        )
        runs = db.get_recent_runs(initialized_db, source_name="cegedim")
        assert len(runs) == 1
        assert runs[0]["status"] == "error"
        assert runs[0]["error_message"] == "Connection timeout"

    def test_get_recent_all_sources(self, initialized_db):
        db.save_source_run(initialized_db, "email", "2025-01-01T00:00:00Z", "2025-01-01T00:01:00Z", "ok")
        db.save_source_run(initialized_db, "cegedim", "2025-01-01T00:00:00Z", "2025-01-01T00:01:00Z", "ok")
        runs = db.get_recent_runs(initialized_db)
        assert len(runs) == 2

    def test_limit_works(self, initialized_db):
        for i in range(5):
            db.save_source_run(initialized_db, "email", f"2025-01-0{i+1}T00:00:00Z", f"2025-01-0{i+1}T00:01:00Z", "ok")
        runs = db.get_recent_runs(initialized_db, source_name="email", limit=3)
        assert len(runs) == 3
