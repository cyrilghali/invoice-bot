"""Tests for src/sources/email_source.py — email inbox source."""

from unittest.mock import patch, MagicMock

import pytest

from poller import Attachment, Email
from sources.email_source import run


def _make_email(email_id="e1", sender="billing@example.com", subject="Facture",
                received_at="2025-03-15T10:00:00Z", internet_message_id="<msg-001@mail>"):
    return Email(
        email_id=email_id,
        sender=sender,
        subject=subject,
        received_at=received_at,
        internet_message_id=internet_message_id,
        attachments=[Attachment("inv.pdf", "application/pdf", b"%PDF")],
    )


def _base_config():
    return {
        "source_name": "test_inbox",
        "client_id": "test-client-id",
        "onedrive_folder_name": "Factures",
        "whitelisted_senders": [],
        "subject_keywords": [],
        "link_keywords": [],
    }


class TestEmailSourceRun:
    @patch("sources.email_source.db")
    @patch("sources.email_source.process_attachment", return_value="invoice")
    @patch("sources.email_source.GraphClient")
    def test_fetches_and_processes_new_emails(self, MockGraph, mock_process, mock_db):
        mock_db.is_document_processed.return_value = False
        email = _make_email()
        MockGraph.return_value.fetch_emails_with_attachments.return_value = [email]

        run(_base_config(), "/tmp/test-data")

        mock_process.assert_called_once()
        mock_db.mark_document_processed.assert_called_once_with(
            "/tmp/test-data",
            source_name="test_inbox",
            source_id="<msg-001@mail>",
            sender="billing@example.com",
            subject="Facture",
            received_at="2025-03-15T10:00:00Z",
        )
        mock_db.save_source_run.assert_called_once()
        run_kwargs = mock_db.save_source_run.call_args.kwargs
        assert run_kwargs["status"] == "ok"
        assert run_kwargs["documents_found"] == 1
        assert run_kwargs["documents_new"] == 1
        assert run_kwargs["invoices_saved"] == 1

    @patch("sources.email_source.db")
    @patch("sources.email_source.process_attachment")
    @patch("sources.email_source.GraphClient")
    def test_skips_already_processed(self, MockGraph, mock_process, mock_db):
        mock_db.is_document_processed.return_value = True
        email = _make_email()
        MockGraph.return_value.fetch_emails_with_attachments.return_value = [email]

        run(_base_config(), "/tmp/test-data")

        mock_process.assert_not_called()
        mock_db.mark_document_processed.assert_not_called()
        run_kwargs = mock_db.save_source_run.call_args.kwargs
        assert run_kwargs["documents_new"] == 0

    @patch("sources.email_source.db")
    @patch("sources.email_source.process_attachment")
    @patch("sources.email_source.GraphClient")
    def test_empty_inbox(self, MockGraph, mock_process, mock_db):
        MockGraph.return_value.fetch_emails_with_attachments.return_value = []

        run(_base_config(), "/tmp/test-data")

        mock_process.assert_not_called()
        run_kwargs = mock_db.save_source_run.call_args.kwargs
        assert run_kwargs["documents_found"] == 0

    @patch("sources.email_source.db")
    @patch("sources.email_source.process_attachment", side_effect=Exception("upload failed"))
    @patch("sources.email_source.GraphClient")
    def test_attachment_error_doesnt_prevent_marking(self, MockGraph, mock_process, mock_db):
        mock_db.is_document_processed.return_value = False
        email = _make_email()
        MockGraph.return_value.fetch_emails_with_attachments.return_value = [email]

        run(_base_config(), "/tmp/test-data")

        # Email still marked processed despite attachment failure
        mock_db.mark_document_processed.assert_called_once()
        run_kwargs = mock_db.save_source_run.call_args.kwargs
        assert run_kwargs["status"] == "ok"  # source-level succeeded, attachment error is per-item

    @patch("sources.email_source.db")
    @patch("sources.email_source.process_attachment", return_value="invoice")
    @patch("sources.email_source.GraphClient")
    def test_uses_internet_message_id_as_source_id(self, MockGraph, mock_process, mock_db):
        mock_db.is_document_processed.return_value = False
        email = _make_email(internet_message_id="<stable-id@mail>")
        MockGraph.return_value.fetch_emails_with_attachments.return_value = [email]

        run(_base_config(), "/tmp/test-data")

        mock_db.is_document_processed.assert_called_with("/tmp/test-data", "test_inbox", "<stable-id@mail>")

    @patch("sources.email_source.db")
    @patch("sources.email_source.process_attachment", return_value="invoice")
    @patch("sources.email_source.GraphClient")
    def test_falls_back_to_email_id_when_no_internet_message_id(self, MockGraph, mock_process, mock_db):
        mock_db.is_document_processed.return_value = False
        email = _make_email(internet_message_id="")
        MockGraph.return_value.fetch_emails_with_attachments.return_value = [email]

        run(_base_config(), "/tmp/test-data")

        mock_db.is_document_processed.assert_called_with("/tmp/test-data", "test_inbox", "e1")

    @patch("sources.email_source.db")
    @patch("sources.email_source.process_attachment", return_value="invoice")
    @patch("sources.email_source.GraphClient")
    def test_reads_config_params(self, MockGraph, mock_process, mock_db):
        mock_db.is_document_processed.return_value = False
        MockGraph.return_value.fetch_emails_with_attachments.return_value = []
        config = _base_config()
        config["whitelisted_senders"] = ["a@b.com"]
        config["subject_keywords"] = ["facture"]
        config["since_date"] = "2025-01-01"
        config["link_keywords"] = ["download"]

        run(config, "/tmp/test-data")

        call_kwargs = MockGraph.return_value.fetch_emails_with_attachments.call_args.kwargs
        assert call_kwargs["whitelisted_senders"] == ["a@b.com"]
        assert call_kwargs["subject_keywords"] == ["facture"]
        assert call_kwargs["since"] == "2025-01-01"
        assert call_kwargs["link_keywords"] == ["download"]
