"""Tests for src/sources/telegram_bot.py — dedup pre-check and formatting."""

import hashlib
from unittest.mock import patch

import pytest

import db
from sources import telegram_bot


@pytest.fixture()
def sample_cfg():
    return {
        "microsoft": {"client_id": "test-client"},
        "telegram": {
            "allowed_senders": [1],
            "onedrive_folder_name": "Factures-TEST",
            "onedrive_account": "test@example.com",
        },
    }


@pytest.fixture()
def tmp_file(tmp_path):
    p = tmp_path / "photo.jpg"
    p.write_bytes(b"hello-world-bytes")
    return str(p)


def _hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ---------------------------------------------------------------------------
# _check_existing_on_drive
# ---------------------------------------------------------------------------


class TestCheckExistingOnDrive:
    def test_new_file(self, initialized_db, tmp_file, sample_cfg):
        state, msg = telegram_bot._check_existing_on_drive(tmp_file, sample_cfg, initialized_db)
        assert state == "new"
        assert msg is None

    def test_existing_on_drive_returns_formatted_message(
        self, initialized_db, tmp_file, sample_cfg
    ):
        source_id = _hash(tmp_file)
        db.mark_document_processed(
            initialized_db, "telegram", source_id, "cyril", "sub", "2025-01-01T00:00:00Z"
        )
        db.save_invoice(
            initialized_db,
            filename="f.pdf",
            sender="cyril",
            received_at="2025-01-01T00:00:00Z",
            year=2025, month=1,
            source_name="telegram",
            source_document_id=source_id,
            drive_file_id="drive-id-xyz",
            drive_web_link="https://onedrive.example/live",
        )
        with patch("sources.telegram_bot.onedrive_uploader.file_exists_by_id", return_value=True) as mock:
            state, msg = telegram_bot._check_existing_on_drive(tmp_file, sample_cfg, initialized_db)
        mock.assert_called_once_with("test-client", "drive-id-xyz", account_hint="test@example.com")
        assert state == "exists"
        assert "https://onedrive.example/live" in msg
        assert "<a href=" in msg  # HTML link
        # Dedup row must still be there — we didn't purge
        assert db.is_document_processed(initialized_db, "telegram", source_id) is True

    def test_stale_when_drive_file_missing_purges_and_reprocesses(
        self, initialized_db, tmp_file, sample_cfg
    ):
        source_id = _hash(tmp_file)
        db.mark_document_processed(
            initialized_db, "telegram", source_id, "cyril", "sub", "2025-01-01T00:00:00Z"
        )
        db.save_invoice(
            initialized_db,
            filename="gone.pdf",
            sender="cyril",
            received_at="2025-01-01T00:00:00Z",
            year=2025, month=1,
            source_name="telegram",
            source_document_id=source_id,
            drive_file_id="drive-id-gone",
            drive_web_link="https://onedrive.example/gone",
        )
        with patch("sources.telegram_bot.onedrive_uploader.file_exists_by_id", return_value=False):
            state, msg = telegram_bot._check_existing_on_drive(tmp_file, sample_cfg, initialized_db)
        assert state == "stale"
        assert msg is None
        # Purged — bot will re-process on the next path
        assert db.is_document_processed(initialized_db, "telegram", source_id) is False
        assert db.get_invoice_by_source_document_id(initialized_db, "telegram", source_id) is None

    def test_stale_when_prior_run_had_no_invoice_row(
        self, initialized_db, tmp_file, sample_cfg
    ):
        """Previous send classified as review/rejected → dedup row only, no invoice."""
        source_id = _hash(tmp_file)
        db.mark_document_processed(
            initialized_db, "telegram", source_id, "cyril", "sub", "2025-01-01T00:00:00Z"
        )
        with patch("sources.telegram_bot.onedrive_uploader.file_exists_by_id") as mock_graph:
            state, msg = telegram_bot._check_existing_on_drive(tmp_file, sample_cfg, initialized_db)
        # Must not hit Graph if there was no invoice row to verify against
        mock_graph.assert_not_called()
        assert state == "stale"
        assert db.is_document_processed(initialized_db, "telegram", source_id) is False

    def test_stale_when_drive_link_missing(self, initialized_db, tmp_file, sample_cfg):
        """Invoice row exists but drive_web_link is NULL — can't link the user,
        so treat as stale instead of sending a broken message."""
        source_id = _hash(tmp_file)
        db.mark_document_processed(
            initialized_db, "telegram", source_id, "cyril", "sub", "2025-01-01T00:00:00Z"
        )
        db.save_invoice(
            initialized_db,
            filename="partial.pdf",
            sender="cyril",
            received_at="2025-01-01T00:00:00Z",
            year=2025, month=1,
            source_name="telegram",
            source_document_id=source_id,
            drive_file_id="some-id",
            drive_web_link=None,
        )
        with patch("sources.telegram_bot.onedrive_uploader.file_exists_by_id") as mock_graph:
            state, _ = telegram_bot._check_existing_on_drive(tmp_file, sample_cfg, initialized_db)
        mock_graph.assert_not_called()
        assert state == "stale"


# ---------------------------------------------------------------------------
# _format_existing_message
# ---------------------------------------------------------------------------


class TestFormatExistingMessage:
    def test_html_link(self):
        msg = telegram_bot._format_existing_message({
            "drive_web_link": "https://onedrive.example/abc?cid=1&id=2",
        })
        assert '<a href="https://onedrive.example/abc?cid=1&id=2">ici</a>' in msg
        assert "Déjà traité" in msg


# ---------------------------------------------------------------------------
# _extract_media
# ---------------------------------------------------------------------------


class TestExtractMedia:
    def test_document(self):
        msg = {"document": {"file_id": "F1", "file_name": "invoice.pdf"}}
        assert telegram_bot._extract_media(msg) == ("F1", "invoice.pdf")

    def test_document_without_filename(self):
        msg = {"document": {"file_id": "F1"}}
        assert telegram_bot._extract_media(msg) == ("F1", "F1.bin")

    def test_photo_picks_largest(self):
        msg = {"photo": [
            {"file_id": "small", "file_size": 100},
            {"file_id": "big", "file_size": 500},
            {"file_id": "medium", "file_size": 300},
        ]}
        fid, name = telegram_bot._extract_media(msg)
        assert fid == "big"
        assert name.endswith(".jpg")

    def test_text_message_returns_none(self):
        assert telegram_bot._extract_media({"text": "hi"}) is None
