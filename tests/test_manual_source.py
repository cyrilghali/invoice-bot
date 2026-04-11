"""Tests for manual_source — local file processing through the invoice pipeline."""

import hashlib
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

# Ensure src/ is on the path
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sources.manual_source import run, _file_hash, _guess_content_type


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------

class TestFileHash:
    def test_deterministic(self):
        data = b"hello invoice"
        assert _file_hash(data) == hashlib.sha256(data).hexdigest()

    def test_different_content_different_hash(self):
        assert _file_hash(b"file1") != _file_hash(b"file2")


class TestGuessContentType:
    def test_pdf(self):
        assert _guess_content_type("invoice.pdf") == "application/pdf"

    def test_jpeg(self):
        assert _guess_content_type("photo.jpg") in ("image/jpeg",)

    def test_png(self):
        assert _guess_content_type("scan.png") == "image/png"

    def test_unknown_defaults_to_pdf(self):
        assert _guess_content_type("weirdfile.xyz123") == "application/pdf"


# ---------------------------------------------------------------------------
# Integration-style tests for run()
# ---------------------------------------------------------------------------

class TestManualSourceRun:
    @pytest.fixture
    def data_dir(self, tmp_path):
        """Temporary data dir with initialized DB."""
        dd = str(tmp_path / "data")
        os.makedirs(dd)
        # Init DB
        import db
        db.init_db(dd)
        return dd

    @pytest.fixture
    def sample_pdf(self, tmp_path):
        """Create a minimal PDF file."""
        pdf_path = str(tmp_path / "test-invoice.pdf")
        # Minimal valid-ish PDF content (enough for our pipeline mock)
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF-1.4 fake invoice content for testing")
        return pdf_path

    def _base_config(self):
        return {
            "source_name": "manual_upload",
            "client_id": "fake-client-id",
            "onedrive_folder_name": "Factures-GHALI",
            "onedrive_account": "colisee.ghali@hotmail.com",
            "default_sender": "Dad (manual)",
        }

    @patch("sources.manual_source.process_attachment")
    def test_processes_single_file(self, mock_process, data_dir, sample_pdf):
        """A valid file should be passed to process_attachment."""
        mock_process.return_value = {"status": "invoice", "existing": None}

        config = self._base_config()
        config["_files"] = [sample_pdf]

        run(config, data_dir)

        mock_process.assert_called_once()
        call_kwargs = mock_process.call_args
        att = call_kwargs.kwargs.get("attachment") or call_kwargs[1].get("attachment") or call_kwargs[0][0]
        assert att.name == "test-invoice.pdf"
        assert att.content_type == "application/pdf"

    @patch("sources.manual_source.process_attachment")
    def test_dedup_skips_same_file(self, mock_process, data_dir, sample_pdf):
        """Processing the same file twice should skip the second time."""
        mock_process.return_value = {"status": "invoice", "existing": None}

        config = self._base_config()
        config["_files"] = [sample_pdf]

        run(config, data_dir)
        assert mock_process.call_count == 1

        # Second run — same file, should be skipped
        run(config, data_dir)
        assert mock_process.call_count == 1  # still 1, not 2

    @patch("sources.manual_source.process_attachment")
    def test_custom_sender(self, mock_process, data_dir, sample_pdf):
        """--sender should override default_sender."""
        mock_process.return_value = {"status": "invoice", "existing": None}

        config = self._base_config()
        config["_files"] = [sample_pdf]
        config["_sender"] = "Papa Ihab"

        run(config, data_dir)

        call_kwargs = mock_process.call_args
        sender = call_kwargs.kwargs.get("sender") or call_kwargs[1].get("sender")
        assert sender == "Papa Ihab"

    def test_missing_file_does_not_crash(self, data_dir, capsys):
        """A non-existent file path should log an error but not crash."""
        config = self._base_config()
        config["_files"] = ["/tmp/nonexistent-invoice-xyz.pdf"]

        run(config, data_dir)

        captured = capsys.readouterr()
        assert "error" in captured.out.lower() or "not found" in captured.out.lower()

    def test_no_files_prints_error(self, data_dir, capsys):
        """No files at all should print an error message."""
        config = self._base_config()
        config["_files"] = []

        run(config, data_dir)

        captured = capsys.readouterr()
        assert "no files" in captured.err.lower() or "error" in captured.err.lower()

    @patch("sources.manual_source.process_attachment")
    def test_unsupported_type_rejected(self, mock_process, data_dir, tmp_path):
        """A .txt file should be rejected without calling process_attachment."""
        txt_file = str(tmp_path / "notes.txt")
        with open(txt_file, "w") as f:
            f.write("just some notes, not an invoice")

        config = self._base_config()
        config["_files"] = [txt_file]

        run(config, data_dir)

        mock_process.assert_not_called()

    @patch("sources.manual_source.process_attachment")
    def test_source_run_saved(self, mock_process, data_dir, sample_pdf):
        """Each run should be tracked in source_runs table."""
        mock_process.return_value = {"status": "invoice", "existing": None}

        config = self._base_config()
        config["_files"] = [sample_pdf]

        run(config, data_dir)

        import db
        runs = db.get_recent_runs(data_dir, source_name="manual_upload")
        assert len(runs) == 1
        assert runs[0]["status"] == "ok"
        assert runs[0]["invoices_saved"] == 1

    @patch("sources.manual_source.process_attachment")
    def test_multiple_files(self, mock_process, data_dir, tmp_path):
        """Multiple files should each be processed."""
        mock_process.return_value = {"status": "invoice", "existing": None}

        files = []
        for i in range(3):
            p = str(tmp_path / f"invoice-{i}.pdf")
            with open(p, "wb") as f:
                f.write(f"%PDF-1.4 invoice {i}".encode())
            files.append(p)

        config = self._base_config()
        config["_files"] = files

        run(config, data_dir)

        assert mock_process.call_count == 3
