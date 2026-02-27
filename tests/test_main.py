"""Tests for src/main.py — poll_inbox and send_report orchestration."""

import os
from unittest.mock import patch, MagicMock, call
from datetime import datetime, timezone

import pytest

from main import poll_inbox, send_report
from poller import Attachment, Email


# ---------------------------------------------------------------------------
# poll_inbox
# ---------------------------------------------------------------------------

class TestPollInbox:
    def _base_config(self):
        return {
            "microsoft": {"client_id": "cid"},
            "onedrive": {"folder_name": "Root"},
            "invoices": {
                "whitelisted_senders": [],
                "subject_keywords": [],
                "sender_suppliers": {},
            },
            "link_detection": {"keywords": []},
        }

    @patch("main.process_attachment", return_value="invoice")
    @patch("main.db")
    @patch("main.GraphClient")
    def test_processes_new_emails(self, MockGraph, mock_db, mock_process, monkeypatch):
        monkeypatch.setenv("DATA_DIR", "/tmp/test-data")
        mock_db.is_email_processed.return_value = False

        email = Email(
            email_id="e1",
            sender="a@b.com",
            subject="Facture",
            received_at="2025-03-15T10:00:00Z",
            attachments=[Attachment("inv.pdf", "application/pdf", b"%PDF")],
        )
        MockGraph.return_value.fetch_emails_with_attachments.return_value = [email]

        poll_inbox(self._base_config())
        mock_process.assert_called_once()
        mock_db.mark_email_processed.assert_called_once()

    @patch("main.process_attachment")
    @patch("main.db")
    @patch("main.GraphClient")
    def test_skips_already_processed(self, MockGraph, mock_db, mock_process, monkeypatch):
        monkeypatch.setenv("DATA_DIR", "/tmp/test-data")
        mock_db.is_email_processed.return_value = True

        email = Email(
            email_id="e1",
            sender="a@b.com",
            subject="Facture",
            received_at="2025-03-15T10:00:00Z",
            attachments=[Attachment("inv.pdf", "application/pdf", b"%PDF")],
        )
        MockGraph.return_value.fetch_emails_with_attachments.return_value = [email]

        poll_inbox(self._base_config())
        mock_process.assert_not_called()
        mock_db.mark_email_processed.assert_not_called()

    @patch("main.process_attachment", side_effect=Exception("upload failed"))
    @patch("main.db")
    @patch("main.GraphClient")
    def test_continues_on_attachment_error(self, MockGraph, mock_db, mock_process, monkeypatch):
        monkeypatch.setenv("DATA_DIR", "/tmp/test-data")
        mock_db.is_email_processed.return_value = False

        email = Email(
            email_id="e1",
            sender="a@b.com",
            subject="Facture",
            received_at="2025-03-15T10:00:00Z",
            attachments=[
                Attachment("inv1.pdf", "application/pdf", b"%PDF"),
                Attachment("inv2.pdf", "application/pdf", b"%PDF"),
            ],
        )
        MockGraph.return_value.fetch_emails_with_attachments.return_value = [email]

        # Should not raise even though process_attachment fails
        poll_inbox(self._base_config())
        # Email still marked as processed after all attachments attempted
        mock_db.mark_email_processed.assert_called_once()


# ---------------------------------------------------------------------------
# send_report
# ---------------------------------------------------------------------------

class TestSendReport:
    def _base_config(self):
        return {
            "microsoft": {"client_id": "cid"},
            "onedrive": {"folder_name": "Root"},
        }

    @patch("main.db")
    def test_skips_if_already_sent(self, mock_db, monkeypatch):
        monkeypatch.setenv("DATA_DIR", "/tmp/test-data")
        mock_db.has_monthly_report_been_sent.return_value = True

        send_report(self._base_config())
        mock_db.get_unreported_invoices.assert_not_called()

    @patch("main.db")
    def test_skips_if_no_invoices(self, mock_db, monkeypatch):
        monkeypatch.setenv("DATA_DIR", "/tmp/test-data")
        mock_db.has_monthly_report_been_sent.return_value = False
        mock_db.get_unreported_invoices.return_value = []

        send_report(self._base_config())
        mock_db.save_monthly_report.assert_called_once()

    @patch("main.upload_attachment", return_value=("fid", "https://link"))
    @patch("main.build_monthly_excel", return_value=b"xlsx-bytes")
    @patch("main.db")
    def test_builds_and_uploads_report(self, mock_db, mock_excel, mock_upload, monkeypatch):
        monkeypatch.setenv("DATA_DIR", "/tmp/test-data")
        mock_db.has_monthly_report_been_sent.return_value = False
        mock_db.get_unreported_invoices.return_value = [
            {"id": 1, "filename": "inv.pdf"},
            {"id": 2, "filename": "inv2.pdf"},
        ]

        send_report(self._base_config())
        mock_excel.assert_called_once()
        mock_upload.assert_called_once()
        mock_db.mark_invoices_reported.assert_called_once_with(
            "/tmp/test-data", [1, 2]
        )
        mock_db.save_monthly_report.assert_called_once()
