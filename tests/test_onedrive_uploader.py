"""Tests for src/onedrive_uploader.py — filename building and supplier labels."""

from unittest.mock import patch, MagicMock

import pytest

from onedrive_uploader import _supplier_to_label, build_filename


# ---------------------------------------------------------------------------
# _supplier_to_label
# ---------------------------------------------------------------------------

class TestSupplierToLabel:
    def test_simple_name(self):
        assert _supplier_to_label("Amazon") == "amazon"

    def test_multi_word(self):
        assert _supplier_to_label("Free SAS") == "free-sas"

    def test_accents_stripped(self):
        result = _supplier_to_label("EDF Électricité de France")
        assert result == "edf-electricite-de-france"

    def test_dots_replaced(self):
        assert _supplier_to_label("Orange S.A.") == "orange-s-a"

    def test_truncated_at_40(self):
        long = "A Very Long Company Name That Exceeds Forty Characters Easily"
        result = _supplier_to_label(long)
        assert len(result) <= 40

    def test_empty_string(self):
        assert _supplier_to_label("") == ""

    def test_special_chars_removed(self):
        assert _supplier_to_label("O'Reilly & Co.") == "o-reilly-co"

    def test_leading_trailing_hyphens_stripped(self):
        result = _supplier_to_label("  --- Test ---  ")
        assert not result.startswith("-")
        assert not result.endswith("-")


# ---------------------------------------------------------------------------
# build_filename
# ---------------------------------------------------------------------------

class TestBuildFilename:
    def test_with_invoice_date_and_supplier(self):
        result = build_filename(
            received_at="2025-03-15T10:00:00Z",
            sender="billing@example.com",
            original_name="facture_123.pdf",
            invoice_date="2025-03-10",
            supplier="Acme Corp",
        )
        assert result.startswith("2025-03-10_")
        assert "acme-corp" in result
        assert "facture_123.pdf" in result

    def test_without_invoice_date_uses_received(self):
        result = build_filename(
            received_at="2025-06-20T14:30:00Z",
            sender="noreply@shop.com",
            original_name="receipt.pdf",
        )
        assert result.startswith("2025-06-20_")

    def test_without_supplier_uses_sender_domain(self):
        result = build_filename(
            received_at="2025-01-01T00:00:00Z",
            sender="invoices@bigcorp.com",
            original_name="file.pdf",
        )
        assert "bigcorp" in result

    def test_with_supplier_overrides_sender(self):
        result = build_filename(
            received_at="2025-01-01T00:00:00Z",
            sender="noreply@randomdomain.com",
            original_name="file.pdf",
            supplier="Specific Vendor",
        )
        assert "specific-vendor" in result
        assert "randomdomain" not in result

    def test_invalid_received_at_fallback(self):
        result = build_filename(
            received_at="not-a-date",
            sender="a@b.com",
            original_name="file.pdf",
        )
        assert result.startswith("0000-00-00_")

    def test_z_suffix_handled(self):
        result = build_filename(
            received_at="2025-12-31T23:59:59Z",
            sender="a@b.com",
            original_name="file.pdf",
        )
        assert result.startswith("2025-12-31_")

    def test_filename_sanitized(self):
        result = build_filename(
            received_at="2025-01-01T00:00:00Z",
            sender="a@b.com",
            original_name='bad<>file|"name.pdf',
            invoice_date="2025-01-01",
        )
        # Problematic chars should be replaced with _
        assert "<" not in result
        assert ">" not in result
        assert "|" not in result
        assert '"' not in result
