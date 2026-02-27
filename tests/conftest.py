"""Shared fixtures for the invoice-bot test suite."""

import os
import tempfile

import pytest

# Ensure src/ is importable (pyproject.toml also sets pythonpath,
# but this is a safety net when running pytest from odd locations).
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture()
def tmp_data_dir(tmp_path):
    """Return a temporary directory path (str) for SQLite and token caches."""
    return str(tmp_path)


@pytest.fixture()
def initialized_db(tmp_data_dir):
    """Return a tmp data dir with an initialized SQLite database."""
    import db
    db.init_db(tmp_data_dir)
    return tmp_data_dir


@pytest.fixture()
def sample_config():
    """Minimal realistic config dict."""
    return {
        "microsoft": {"client_id": "test-client-id-000"},
        "onedrive": {"folder_name": "Factures-TEST"},
        "classifier": {
            "api_key": "sk-ant-test-key",
            "confidence_threshold": 0.5,
            "owner_business_names": ["My Own Company"],
        },
        "invoices": {
            "whitelisted_senders": ["billing@example.com"],
            "subject_keywords": ["facture", "invoice"],
            "sender_suppliers": {"billing@example.com": "Example Corp"},
        },
        "schedule": {
            "poll_interval_minutes": 10,
            "report_day_of_month": 1,
            "report_hour": 8,
        },
        "link_detection": {"keywords": ["download", "facture"]},
        "logging": {"log_level": "DEBUG"},
    }


@pytest.fixture()
def sample_attachment():
    """A minimal Attachment instance (PDF stub)."""
    from poller import Attachment
    return Attachment(
        name="invoice_2025.pdf",
        content_type="application/pdf",
        content_bytes=b"%PDF-1.4 fake content",
    )


@pytest.fixture()
def sample_email(sample_attachment):
    """A minimal Email instance with one attachment."""
    from poller import Email
    return Email(
        email_id="AAMkABC123",
        sender="billing@example.com",
        subject="Votre facture #1234",
        received_at="2025-03-15T10:30:00Z",
        attachments=[sample_attachment],
    )
