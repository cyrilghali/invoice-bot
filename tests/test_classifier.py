"""Tests for src/classifier.py — response parsing, classification logic."""

import json
import math
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from classifier import (
    _parse_amount,
    _parse_response,
    _classify_text,
    _classify_image,
    is_invoice,
)
from poller import Attachment


# ---------------------------------------------------------------------------
# _parse_amount
# ---------------------------------------------------------------------------

class TestParseAmount:
    def test_valid_float(self):
        assert _parse_amount(42.5) == 42.5

    def test_valid_int(self):
        assert _parse_amount(100) == 100.0

    def test_valid_string_number(self):
        assert _parse_amount("99.9") == 99.9

    def test_none_returns_none(self):
        assert _parse_amount(None) is None

    def test_nan_returns_none(self):
        assert _parse_amount(float("nan")) is None

    def test_inf_returns_none(self):
        assert _parse_amount(float("inf")) is None

    def test_neg_inf_returns_none(self):
        assert _parse_amount(float("-inf")) is None

    def test_non_numeric_string_returns_none(self):
        assert _parse_amount("not-a-number") is None

    def test_negative_value_for_credit_note(self):
        assert _parse_amount(-50.0) == -50.0


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------

class TestParseResponse:
    def _make_json(self, **overrides):
        base = {
            "is_invoice": True,
            "confidence": 0.95,
            "reason": "Document is an invoice",
            "invoice_date": "2025-03-15",
            "supplier": "Acme Corp",
            "amount_ht": 100.0,
            "amount_tva": 20.0,
            "amount_ttc": 120.0,
            "currency": "EUR",
        }
        base.update(overrides)
        return json.dumps(base)

    def test_valid_json(self):
        raw = self._make_json()
        is_inv, conf, reason, date, supplier, ht, ttc, tva, cur = _parse_response(raw)
        assert is_inv is True
        assert conf == 0.95
        assert date == "2025-03-15"
        assert supplier == "Acme Corp"
        assert ht == 100.0
        assert ttc == 120.0
        assert tva == 20.0
        assert cur == "EUR"

    def test_markdown_wrapped_json(self):
        raw = "```json\n" + self._make_json() + "\n```"
        is_inv, conf, *_ = _parse_response(raw)
        assert is_inv is True
        assert conf == 0.95

    def test_malformed_json_returns_fallback(self):
        is_inv, conf, reason, *_ = _parse_response("{bad json!!")
        # Fallback: is_invoice=True, confidence=0.0, reason starts with "Parse error"
        assert is_inv is True
        assert conf == 0.0
        assert "Parse error" in reason

    def test_invalid_date_format_ignored(self):
        raw = self._make_json(invoice_date="15/03/2025")
        _, _, _, date, *_ = _parse_response(raw)
        assert date is None

    def test_null_date_string(self):
        raw = self._make_json(invoice_date="null")
        _, _, _, date, *_ = _parse_response(raw)
        assert date is None

    def test_supplier_null_string_cleaned(self):
        for null_val in ("null", "None", "n/a", ""):
            raw = self._make_json(supplier=null_val)
            _, _, _, _, supplier, *_ = _parse_response(raw)
            assert supplier is None, f"supplier should be None for value {null_val!r}"

    def test_supplier_truncated_at_80(self):
        long_name = "A" * 100
        raw = self._make_json(supplier=long_name)
        _, _, _, _, supplier, *_ = _parse_response(raw)
        assert len(supplier) == 80

    def test_owner_name_filtered(self):
        raw = self._make_json(supplier="My Own Company SAS")
        _, _, _, _, supplier, *_ = _parse_response(raw, owner_names={"my own company"})
        assert supplier is None

    def test_currency_normalised_uppercase(self):
        raw = self._make_json(currency="eur")
        *_, cur = _parse_response(raw)
        assert cur == "EUR"

    def test_currency_null_string(self):
        raw = self._make_json(currency="null")
        *_, cur = _parse_response(raw)
        assert cur is None


# ---------------------------------------------------------------------------
# _classify_text
# ---------------------------------------------------------------------------

class TestClassifyText:
    def test_empty_text_returns_review(self):
        client = MagicMock()
        is_inv, conf, reason, *_ = _classify_text(client, "")
        assert is_inv is False
        assert conf == 0.0
        assert "No text extracted" in reason
        client.messages.create.assert_not_called()

    def test_calls_api_with_text(self):
        mock_response = MagicMock()
        mock_response.content = [
            SimpleNamespace(text='{"is_invoice": true, "confidence": 0.9, "reason": "ok"}')
        ]
        client = MagicMock()
        client.messages.create.return_value = mock_response

        is_inv, conf, reason, *_ = _classify_text(client, "FACTURE #123")
        assert is_inv is True
        assert conf == 0.9
        client.messages.create.assert_called_once()
        call_kwargs = client.messages.create.call_args
        assert "FACTURE #123" in call_kwargs.kwargs.get("messages", call_kwargs[1].get("messages", [{}]))[0].get("content", "")

    def test_hint_supplier_appended_to_prompt(self):
        mock_response = MagicMock()
        mock_response.content = [
            SimpleNamespace(text='{"is_invoice": true, "confidence": 0.8, "reason": "ok"}')
        ]
        client = MagicMock()
        client.messages.create.return_value = mock_response

        _classify_text(client, "Some text", hint_supplier="Amazon")
        call_args = client.messages.create.call_args
        messages = call_args.kwargs.get("messages") or call_args[1].get("messages")
        prompt_text = messages[0]["content"]
        assert "Amazon" in prompt_text


# ---------------------------------------------------------------------------
# _classify_image
# ---------------------------------------------------------------------------

class TestClassifyImage:
    def test_calls_api_with_base64_image(self):
        mock_response = MagicMock()
        mock_response.content = [
            SimpleNamespace(text='{"is_invoice": false, "confidence": 0.85, "reason": "photo"}')
        ]
        client = MagicMock()
        client.messages.create.return_value = mock_response

        is_inv, conf, reason, *_ = _classify_image(
            client, b"\x89PNG fake image", "image/png"
        )
        assert is_inv is False
        assert conf == 0.85
        client.messages.create.assert_called_once()


# ---------------------------------------------------------------------------
# is_invoice (public interface)
# ---------------------------------------------------------------------------

class TestIsInvoice:
    def test_missing_api_key_returns_review(self):
        att = Attachment(name="test.pdf", content_type="application/pdf", content_bytes=b"data")
        config = {"classifier": {"api_key": ""}}
        status, *_ = is_invoice(att, config)
        assert status == "review"

    def test_placeholder_api_key_returns_review(self):
        att = Attachment(name="test.pdf", content_type="application/pdf", content_bytes=b"data")
        config = {"classifier": {"api_key": "YOUR_ANTHROPIC_API_KEY_HERE"}}
        status, *_ = is_invoice(att, config)
        assert status == "review"

    def test_unsupported_mime_returns_review(self):
        att = Attachment(name="doc.docx", content_type="application/msword", content_bytes=b"data")
        config = {"classifier": {"api_key": "sk-real-key"}}
        with patch("classifier.anthropic.Anthropic"):
            status, *_ = is_invoice(att, config)
        assert status == "review"

    @patch("classifier._extract_pdf_text", return_value="FACTURE #123 Total: 100 EUR")
    @patch("classifier._classify_text")
    @patch("classifier.anthropic.Anthropic")
    def test_pdf_invoice_confirmed(self, mock_anthropic, mock_classify, mock_extract):
        mock_classify.return_value = (True, 0.9, "Invoice", "2025-03-15", "Acme", 100.0, 120.0, 20.0, "EUR")
        att = Attachment(name="facture.pdf", content_type="application/pdf", content_bytes=b"%PDF")
        config = {"classifier": {"api_key": "sk-key", "confidence_threshold": 0.5}}

        status, date, supplier, ht, ttc, tva, cur = is_invoice(att, config)
        assert status == "invoice"
        assert date == "2025-03-15"
        assert supplier == "Acme"

    @patch("classifier._extract_pdf_text", return_value="Contract agreement terms")
    @patch("classifier._classify_text")
    @patch("classifier.anthropic.Anthropic")
    def test_pdf_rejected(self, mock_anthropic, mock_classify, mock_extract):
        mock_classify.return_value = (False, 0.9, "Not an invoice", None, None, None, None, None, None)
        att = Attachment(name="contract.pdf", content_type="application/pdf", content_bytes=b"%PDF")
        config = {"classifier": {"api_key": "sk-key", "confidence_threshold": 0.5}}

        status, *_ = is_invoice(att, config)
        assert status == "rejected"

    @patch("classifier._extract_pdf_text", return_value="Ambiguous document")
    @patch("classifier._classify_text")
    @patch("classifier.anthropic.Anthropic")
    def test_low_confidence_returns_review(self, mock_anthropic, mock_classify, mock_extract):
        mock_classify.return_value = (True, 0.3, "Uncertain", None, None, None, None, None, None)
        att = Attachment(name="maybe.pdf", content_type="application/pdf", content_bytes=b"%PDF")
        config = {"classifier": {"api_key": "sk-key", "confidence_threshold": 0.5}}

        status, *_ = is_invoice(att, config)
        assert status == "review"

    @patch("classifier._classify_image")
    @patch("classifier.anthropic.Anthropic")
    def test_image_jpeg_classified(self, mock_anthropic, mock_classify_img):
        mock_classify_img.return_value = (True, 0.95, "Receipt image", "2025-01-10", "Shop", 50.0, 60.0, 10.0, "EUR")
        att = Attachment(name="receipt.jpg", content_type="image/jpeg", content_bytes=b"\xff\xd8")
        config = {"classifier": {"api_key": "sk-key", "confidence_threshold": 0.5}}

        status, date, supplier, *_ = is_invoice(att, config)
        assert status == "invoice"
        assert supplier == "Shop"

    @patch("classifier._extract_pdf_text", return_value="text")
    @patch("classifier._classify_text", side_effect=Exception("API timeout"))
    @patch("classifier.anthropic.Anthropic")
    def test_api_error_returns_review(self, mock_anthropic, mock_classify, mock_extract):
        att = Attachment(name="file.pdf", content_type="application/pdf", content_bytes=b"%PDF")
        config = {"classifier": {"api_key": "sk-key", "confidence_threshold": 0.5}}

        status, *_ = is_invoice(att, config)
        assert status == "review"
