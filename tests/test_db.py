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
            assert "processed_emails" in tables
            assert "invoices" in tables
            assert "monthly_reports" in tables
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
            for col in ("invoice_date", "supplier", "amount_ht", "amount_ttc", "amount_tva", "currency"):
                assert col in cols, f"Missing migration column: {col}"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Email processing deduplication
# ---------------------------------------------------------------------------

class TestEmailProcessing:
    def test_not_processed_initially(self, initialized_db):
        assert db.is_email_processed(initialized_db, "email-001") is False

    def test_mark_and_check(self, initialized_db):
        db.mark_email_processed(
            initialized_db, "email-001", "sender@test.com", "Test Subject", "2025-01-01T00:00:00Z"
        )
        assert db.is_email_processed(initialized_db, "email-001") is True

    def test_duplicate_insert_ignored(self, initialized_db):
        db.mark_email_processed(
            initialized_db, "email-001", "a@b.com", "Sub1", "2025-01-01T00:00:00Z"
        )
        # INSERT OR IGNORE — should not raise
        db.mark_email_processed(
            initialized_db, "email-001", "a@b.com", "Sub2", "2025-01-02T00:00:00Z"
        )
        assert db.is_email_processed(initialized_db, "email-001") is True


# ---------------------------------------------------------------------------
# Invoice CRUD
# ---------------------------------------------------------------------------

class TestInvoiceCrud:
    def _save_sample(self, data_dir, email_id="e1", filename="inv.pdf", month=3):
        db.mark_email_processed(data_dir, email_id, "s@t.com", "Sub", "2025-03-01T00:00:00Z")
        db.save_invoice(
            data_dir,
            email_id=email_id,
            filename=filename,
            sender="s@t.com",
            received_at="2025-03-01T00:00:00Z",
            year=2025,
            month=month,
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
        db.mark_email_processed(initialized_db, "e2", "s@t.com", "Sub", "2025-04-01T00:00:00Z")
        db.save_invoice(
            initialized_db,
            email_id="e2",
            filename="bare.pdf",
            sender="s@t.com",
            received_at="2025-04-01T00:00:00Z",
            year=2025,
            month=4,
        )
        invoices = db.get_unreported_invoices(initialized_db, 2025, 4)
        assert len(invoices) == 1
        assert invoices[0]["supplier"] is None
        assert invoices[0]["amount_ht"] is None


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
