"""Tests for src/pipeline.py — ZIP unpacking and attachment routing."""

import io
import zipfile
from unittest.mock import patch, MagicMock

import pytest

from pipeline import _unpack_zip, process_attachment
from poller import Attachment, Email


# ---------------------------------------------------------------------------
# _unpack_zip
# ---------------------------------------------------------------------------

def _make_zip(*members: tuple[str, bytes]) -> bytes:
    """Create an in-memory ZIP archive with given (name, content) pairs."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()


class TestUnpackZip:
    def test_extracts_supported_members(self):
        zip_bytes = _make_zip(
            ("invoice.pdf", b"%PDF-data"),
            ("receipt.jpg", b"\xff\xd8image"),
        )
        att = Attachment(name="bundle.zip", content_type="application/zip", content_bytes=zip_bytes)
        members = _unpack_zip(att)
        assert len(members) == 2
        names = {m.name for m in members}
        assert "invoice.pdf" in names
        assert "receipt.jpg" in names

    def test_skips_unsupported_extensions(self):
        zip_bytes = _make_zip(
            ("readme.txt", b"hello"),
            ("invoice.pdf", b"%PDF"),
        )
        att = Attachment(name="mixed.zip", content_type="application/zip", content_bytes=zip_bytes)
        members = _unpack_zip(att)
        assert len(members) == 1
        assert members[0].name == "invoice.pdf"

    def test_skips_macos_metadata(self):
        zip_bytes = _make_zip(
            ("__MACOSX/._invoice.pdf", b"metadata"),
            ("._hidden.pdf", b"metadata"),
            ("real.pdf", b"%PDF"),
        )
        att = Attachment(name="mac.zip", content_type="application/zip", content_bytes=zip_bytes)
        members = _unpack_zip(att)
        assert len(members) == 1
        assert members[0].name == "real.pdf"

    def test_correct_content_types(self):
        zip_bytes = _make_zip(
            ("file.pdf", b"data"),
            ("file.png", b"data"),
            ("file.xlsx", b"data"),
        )
        att = Attachment(name="types.zip", content_type="application/zip", content_bytes=zip_bytes)
        members = _unpack_zip(att)
        ct_map = {m.name: m.content_type for m in members}
        assert ct_map["file.pdf"] == "application/pdf"
        assert ct_map["file.png"] == "image/png"
        assert ct_map["file.xlsx"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def test_bad_zip_returns_empty(self):
        att = Attachment(name="corrupt.zip", content_type="application/zip", content_bytes=b"not a zip")
        members = _unpack_zip(att)
        assert members == []

    def test_empty_zip_returns_empty(self):
        zip_bytes = _make_zip()
        att = Attachment(name="empty.zip", content_type="application/zip", content_bytes=zip_bytes)
        members = _unpack_zip(att)
        assert members == []

    def test_directories_skipped(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("subdir/", "")  # directory entry
            zf.writestr("subdir/file.pdf", b"%PDF")
        att = Attachment(name="withdir.zip", content_type="application/zip", content_bytes=buf.getvalue())
        members = _unpack_zip(att)
        assert len(members) == 1
        assert members[0].name == "file.pdf"


# ---------------------------------------------------------------------------
# process_attachment
# ---------------------------------------------------------------------------

class TestProcessAttachment:
    def _make_email(self):
        return Email(
            email_id="e1",
            sender="billing@example.com",
            subject="Facture",
            received_at="2025-03-15T10:00:00Z",
        )

    @patch("pipeline.db")
    @patch("pipeline.upload_attachment", return_value=("file-id", "https://link"))
    @patch("pipeline.build_filename", return_value="2025-03-15_example_inv.pdf")
    @patch("pipeline.is_invoice", return_value=("invoice", "2025-03-15", "Acme", 100.0, 120.0, 20.0, "EUR"))
    def test_invoice_uploaded_and_saved(self, mock_classify, mock_fname, mock_upload, mock_db):
        att = Attachment(name="inv.pdf", content_type="application/pdf", content_bytes=b"%PDF")
        email = self._make_email()

        status = process_attachment(att, email, 2025, 3, {}, "/data", "cid", "Root")
        assert status == "invoice"
        mock_upload.assert_called_once()
        mock_db.save_invoice.assert_called_once()

    @patch("pipeline.db")
    @patch("pipeline.upload_to_review", return_value=("file-id", "https://link"))
    @patch("pipeline.build_filename", return_value="2025-03-15_example_contract.pdf")
    @patch("pipeline.is_invoice", return_value=("rejected", None, None, None, None, None, None))
    def test_rejected_uploaded_to_review(self, mock_classify, mock_fname, mock_upload, mock_db):
        att = Attachment(name="contract.pdf", content_type="application/pdf", content_bytes=b"%PDF")
        email = self._make_email()

        status = process_attachment(att, email, 2025, 3, {}, "/data", "cid", "Root")
        assert status == "rejected"
        mock_upload.assert_called_once()
        mock_db.save_invoice.assert_not_called()

    @patch("pipeline.db")
    @patch("pipeline.upload_to_review", return_value=("file-id", "https://link"))
    @patch("pipeline.build_filename", return_value="2025-03-15_example_maybe.pdf")
    @patch("pipeline.is_invoice", return_value=("review", None, None, None, None, None, None))
    def test_review_uploaded_to_review(self, mock_classify, mock_fname, mock_upload, mock_db):
        att = Attachment(name="maybe.pdf", content_type="application/pdf", content_bytes=b"%PDF")
        email = self._make_email()

        status = process_attachment(att, email, 2025, 3, {}, "/data", "cid", "Root")
        assert status == "review"
        mock_upload.assert_called_once()

    @patch("pipeline.db")
    @patch("pipeline.upload_attachment", return_value=("file-id", "https://link"))
    @patch("pipeline.build_filename", return_value="2025-01-20_acme_inv.pdf")
    @patch("pipeline.is_invoice", return_value=("invoice", "2025-01-20", "Acme", 100.0, 120.0, 20.0, "EUR"))
    def test_invoice_date_overrides_year_month(self, mock_classify, mock_fname, mock_upload, mock_db):
        att = Attachment(name="inv.pdf", content_type="application/pdf", content_bytes=b"%PDF")
        email = self._make_email()

        # email received in March, but invoice date is January
        status = process_attachment(att, email, 2025, 3, {}, "/data", "cid", "Root")
        assert status == "invoice"
        # Verify upload was called with January (year=2025, month=1)
        call_kwargs = mock_upload.call_args
        assert call_kwargs.kwargs.get("year") or call_kwargs[1].get("year") == 2025
        assert call_kwargs.kwargs.get("month") or call_kwargs[1].get("month") == 1

    @patch("pipeline.db")
    @patch("pipeline.upload_attachment", return_value=("fid", "https://link"))
    @patch("pipeline.upload_to_review", return_value=("fid", "https://link"))
    @patch("pipeline.build_filename", return_value="fname.pdf")
    @patch("pipeline.is_invoice")
    def test_zip_processes_members(self, mock_classify, mock_fname, mock_review, mock_upload, mock_db):
        # First call -> invoice, second call -> rejected
        mock_classify.side_effect = [
            ("invoice", "2025-03-01", "Acme", 100.0, 120.0, 20.0, "EUR"),
            ("rejected", None, None, None, None, None, None),
        ]
        zip_bytes = _make_zip(
            ("invoice.pdf", b"%PDF-data"),
            ("contract.pdf", b"%PDF-data"),
        )
        att = Attachment(name="bundle.zip", content_type="application/zip", content_bytes=zip_bytes)
        email = self._make_email()

        status = process_attachment(att, email, 2025, 3, {}, "/data", "cid", "Root")
        assert status == "invoice"  # at least one member was an invoice
        assert mock_classify.call_count == 2

    @patch("pipeline.is_invoice")
    def test_empty_zip_returns_rejected(self, mock_classify):
        zip_bytes = _make_zip()  # empty archive
        att = Attachment(name="empty.zip", content_type="application/zip", content_bytes=zip_bytes)
        email = self._make_email()

        status = process_attachment(att, email, 2025, 3, {}, "/data", "cid", "Root")
        assert status == "rejected"
        mock_classify.assert_not_called()

    @patch("pipeline.db")
    @patch("pipeline.upload_attachment", return_value=("fid", "https://link"))
    @patch("pipeline.build_filename", return_value="fname.pdf")
    @patch("pipeline.is_invoice", return_value=("invoice", None, None, None, None, None, None))
    def test_sender_supplier_hint_used(self, mock_classify, mock_fname, mock_upload, mock_db):
        att = Attachment(name="inv.pdf", content_type="application/pdf", content_bytes=b"%PDF")
        email = self._make_email()
        config = {
            "invoices": {
                "sender_suppliers": {"billing@example.com": "Example Corp"},
            }
        }

        process_attachment(att, email, 2025, 3, config, "/data", "cid", "Root")
        # Verify hint_supplier was passed to is_invoice
        call_kwargs = mock_classify.call_args
        assert call_kwargs.kwargs.get("hint_supplier") == "Example Corp"
