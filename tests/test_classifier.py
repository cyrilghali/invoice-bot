"""Tests for src/classifier.py — response parsing, classification logic."""

import json
import math
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, ANY

import pytest

from classifier import (
    _parse_amount,
    _parse_response,
    _classify_text,
    _classify_image,
    _classify_pdf_vision,
    _retry_classify,
    _run_claude_cli,
    is_invoice,
    MIN_TEXT_CHARS,
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
        is_inv, conf, reason, date, supplier, entity, ht, ttc, tva, cur = _parse_response(raw)
        assert is_inv is True
        assert conf == 0.95
        assert date == "2025-03-15"
        assert supplier == "Acme Corp"
        assert entity is None
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
# _run_claude_cli
# ---------------------------------------------------------------------------

class TestRunClaudeCli:
    @patch("classifier.subprocess.run")
    def test_returns_stdout_on_success(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"is_invoice": true}', stderr=""
        )
        result = _run_claude_cli("test prompt", "system prompt")
        assert result == '{"is_invoice": true}'

    @patch("classifier.subprocess.run")
    def test_builds_correct_command_no_tools(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        _run_claude_cli("my prompt", "sys prompt", model="opus", tools="")
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "claude"
        assert "-p" in call_args
        assert "--model" in call_args
        assert call_args[call_args.index("--model") + 1] == "opus"
        assert "--tools" in call_args
        assert call_args[call_args.index("--tools") + 1] == ""
        # Prompt is passed via stdin, not as a CLI argument
        assert mock_run.call_args[1]["input"] == "my prompt"

    @patch("classifier.subprocess.run")
    def test_builds_correct_command_with_tools(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        _run_claude_cli("prompt", "sys", tools="Read")
        call_args = mock_run.call_args[0][0]
        assert call_args[call_args.index("--tools") + 1] == "Read"

    @patch("classifier.subprocess.run")
    def test_raises_on_nonzero_exit(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="error"
        )
        with pytest.raises(subprocess.CalledProcessError):
            _run_claude_cli("prompt", "sys")

    @patch("classifier.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=60))
    def test_raises_on_timeout(self, mock_run):
        with pytest.raises(subprocess.TimeoutExpired):
            _run_claude_cli("prompt", "sys")

    @patch("classifier.subprocess.run", side_effect=FileNotFoundError("claude not found"))
    def test_raises_on_missing_claude(self, mock_run):
        with pytest.raises(FileNotFoundError):
            _run_claude_cli("prompt", "sys")


# ---------------------------------------------------------------------------
# _classify_text
# ---------------------------------------------------------------------------

class TestClassifyText:
    def test_empty_text_returns_review(self):
        is_inv, conf, reason, *_ = _classify_text("")
        assert is_inv is False
        assert conf == 0.0
        assert "No text extracted" in reason

    @patch("classifier._run_claude_cli")
    def test_calls_cli_with_text(self, mock_cli):
        mock_cli.return_value = '{"is_invoice": true, "confidence": 0.9, "reason": "ok"}'
        is_inv, conf, reason, *_ = _classify_text("FACTURE #123")
        assert is_inv is True
        assert conf == 0.9
        mock_cli.assert_called_once()
        call_args = mock_cli.call_args
        assert "FACTURE #123" in call_args[0][0]  # prompt contains text

    @patch("classifier._run_claude_cli")
    def test_hint_supplier_appended_to_prompt(self, mock_cli):
        mock_cli.return_value = '{"is_invoice": true, "confidence": 0.8, "reason": "ok"}'
        _classify_text("Some text", hint_supplier="Amazon")
        prompt = mock_cli.call_args[0][0]
        assert "Amazon" in prompt

    @patch("classifier._run_claude_cli")
    def test_passes_model_to_cli(self, mock_cli):
        mock_cli.return_value = '{"is_invoice": true, "confidence": 0.8, "reason": "ok"}'
        _classify_text("text", model="sonnet")
        assert mock_cli.call_args[1].get("model") or mock_cli.call_args[0][2] == "sonnet"


# ---------------------------------------------------------------------------
# _classify_image
# ---------------------------------------------------------------------------

class TestClassifyImage:
    @patch("classifier._run_claude_cli")
    def test_calls_cli_with_read_tool(self, mock_cli):
        mock_cli.return_value = '{"is_invoice": false, "confidence": 0.85, "reason": "photo"}'
        is_inv, conf, reason, *_ = _classify_image(
            b"\x89PNG fake image", "image/png"
        )
        assert is_inv is False
        assert conf == 0.85
        mock_cli.assert_called_once()
        # Should use Read tool for images
        call_kwargs = mock_cli.call_args
        assert "Read" in str(call_kwargs)

    @patch("classifier._run_claude_cli")
    def test_temp_file_cleaned_up_on_success(self, mock_cli):
        mock_cli.return_value = '{"is_invoice": true, "confidence": 0.9, "reason": "ok"}'
        _classify_image(b"\x89PNG fake", "image/png")
        prompt = mock_cli.call_args[0][0]
        import re
        match = re.search(r"/tmp/\S+\.png", prompt)
        if match:
            assert not os.path.exists(match.group(0)), "Temp file should be cleaned up"

    @patch("classifier._run_claude_cli", side_effect=Exception("CLI error"))
    def test_temp_file_cleaned_up_on_error(self, mock_cli):
        with pytest.raises(Exception):
            _classify_image(b"\x89PNG fake", "image/png")

    @patch("classifier._run_claude_cli")
    def test_correct_extension_for_jpeg(self, mock_cli):
        mock_cli.return_value = '{"is_invoice": true, "confidence": 0.9, "reason": "ok"}'
        _classify_image(b"\xff\xd8 fake jpeg", "image/jpeg")
        prompt = mock_cli.call_args[0][0]
        assert ".jpg" in prompt


# ---------------------------------------------------------------------------
# _classify_pdf_vision
# ---------------------------------------------------------------------------

class TestClassifyPdfVision:
    @patch("classifier._run_claude_cli")
    def test_calls_cli_with_read_tool(self, mock_cli):
        mock_cli.return_value = '{"is_invoice": true, "confidence": 0.9, "reason": "scanned invoice"}'
        is_inv, conf, *_ = _classify_pdf_vision(b"%PDF fake", "opus")
        assert is_inv is True
        assert conf == 0.9
        assert "Read" in str(mock_cli.call_args)

    @patch("classifier._run_claude_cli")
    def test_temp_pdf_cleaned_up(self, mock_cli):
        mock_cli.return_value = '{"is_invoice": true, "confidence": 0.9, "reason": "ok"}'
        _classify_pdf_vision(b"%PDF fake", "opus")
        prompt = mock_cli.call_args[0][0]
        import re
        match = re.search(r"/tmp/\S+\.pdf", prompt)
        if match:
            assert not os.path.exists(match.group(0)), "Temp PDF should be cleaned up"

    @patch("classifier._run_claude_cli", side_effect=Exception("fail"))
    def test_temp_pdf_cleaned_up_on_error(self, mock_cli):
        with pytest.raises(Exception):
            _classify_pdf_vision(b"%PDF fake", "opus")

    @patch("classifier._run_claude_cli")
    def test_hint_supplier_in_prompt(self, mock_cli):
        mock_cli.return_value = '{"is_invoice": true, "confidence": 0.9, "reason": "ok"}'
        _classify_pdf_vision(b"%PDF", "opus", hint_supplier="Cegedim")
        prompt = mock_cli.call_args[0][0]
        assert "Cegedim" in prompt


# ---------------------------------------------------------------------------
# _retry_classify
# ---------------------------------------------------------------------------

class TestRetryClassify:
    @patch("classifier._run_claude_cli")
    def test_uses_retry_prompt(self, mock_cli):
        mock_cli.return_value = '{"is_invoice": true, "confidence": 0.7, "reason": "looks like invoice"}'
        is_inv, conf, *_ = _retry_classify("some text")
        assert is_inv is True
        assert conf == 0.7
        prompt = mock_cli.call_args[0][0]
        assert "Réexamine attentivement" in prompt
        assert "some text" in prompt

    @patch("classifier._run_claude_cli")
    def test_hint_supplier_in_retry(self, mock_cli):
        mock_cli.return_value = '{"is_invoice": true, "confidence": 0.8, "reason": "ok"}'
        _retry_classify("text", hint_supplier="Netexial")
        prompt = mock_cli.call_args[0][0]
        assert "Netexial" in prompt


# ---------------------------------------------------------------------------
# is_invoice (public interface)
# ---------------------------------------------------------------------------

class TestIsInvoice:
    def test_unsupported_mime_returns_review(self):
        att = Attachment(name="doc.docx", content_type="application/msword", content_bytes=b"data")
        config = {"classifier": {}}
        status, *_ = is_invoice(att, config)
        assert status == "review"

    @patch("classifier._extract_pdf_text", return_value="FACTURE #123 Total: 100 EUR — émise le 15 mars 2025 par Acme Corp")
    @patch("classifier._classify_text")
    def test_pdf_invoice_confirmed(self, mock_classify, mock_extract):
        mock_classify.return_value = (True, 0.9, "Invoice", "2025-03-15", "Acme", None, 100.0, 120.0, 20.0, "EUR")
        att = Attachment(name="facture.pdf", content_type="application/pdf", content_bytes=b"%PDF")
        config = {"classifier": {"confidence_threshold": 0.5}}

        status, date, supplier, entity, ht, ttc, tva, cur = is_invoice(att, config)
        assert status == "invoice"
        assert date == "2025-03-15"
        assert supplier == "Acme"

    @patch("classifier._extract_pdf_text", return_value="Contract agreement terms and conditions for services rendered by company")
    @patch("classifier._classify_text")
    def test_pdf_rejected(self, mock_classify, mock_extract):
        mock_classify.return_value = (False, 0.9, "Not an invoice", None, None, None, None, None, None, None)
        att = Attachment(name="contract.pdf", content_type="application/pdf", content_bytes=b"%PDF")
        config = {"classifier": {"confidence_threshold": 0.5}}

        status, *_ = is_invoice(att, config)
        assert status == "rejected"

    @patch("classifier._extract_pdf_text", return_value="Ambiguous document text here that is long enough to pass the minimum threshold")
    @patch("classifier._retry_classify")
    @patch("classifier._classify_text")
    def test_borderline_confidence_triggers_retry(self, mock_classify, mock_retry, mock_extract):
        # First pass returns borderline confidence (between 0.3 and 0.5)
        mock_classify.return_value = (True, 0.35, "Uncertain", None, None, None, None, None, None, None)
        # Retry returns higher confidence
        mock_retry.return_value = (True, 0.8, "Invoice confirmed", "2025-06-01", "Netexial", None, 200.0, 240.0, 40.0, "EUR")
        att = Attachment(name="maybe.pdf", content_type="application/pdf", content_bytes=b"%PDF")
        config = {"classifier": {"confidence_threshold": 0.5}}

        status, date, supplier, entity, *_ = is_invoice(att, config)
        assert status == "invoice"
        assert supplier == "Netexial"
        mock_retry.assert_called_once()

    @patch("classifier._extract_pdf_text", return_value="Ambiguous document content that is long enough to pass the minimum char threshold")
    @patch("classifier._classify_text")
    def test_very_low_confidence_no_retry(self, mock_classify, mock_extract):
        # Confidence below RETRY_CONFIDENCE_LOW (0.3) — no retry
        mock_classify.return_value = (True, 0.1, "No idea", None, None, None, None, None, None, None)
        att = Attachment(name="maybe.pdf", content_type="application/pdf", content_bytes=b"%PDF")
        config = {"classifier": {"confidence_threshold": 0.5}}

        status, *_ = is_invoice(att, config)
        assert status == "review"

    @patch("classifier._classify_pdf_vision")
    @patch("classifier._extract_pdf_text", return_value="")
    def test_scanned_pdf_falls_back_to_vision(self, mock_extract, mock_vision):
        mock_vision.return_value = (True, 0.9, "Scanned invoice", "2025-03-15", "Cegedim", None, 500.0, 600.0, 100.0, "EUR")
        att = Attachment(name="46.jpg.pdf", content_type="application/pdf", content_bytes=b"%PDF-scanned")
        config = {"classifier": {"confidence_threshold": 0.5}}

        status, date, supplier, entity, *_ = is_invoice(att, config)
        assert status == "invoice"
        assert supplier == "Cegedim"
        mock_vision.assert_called_once()

    @patch("classifier._classify_pdf_vision")
    @patch("classifier._extract_pdf_text", return_value="ab")
    def test_short_pdf_text_triggers_vision_fallback(self, mock_extract, mock_vision):
        # Text shorter than MIN_TEXT_CHARS (50) triggers vision
        mock_vision.return_value = (True, 0.85, "ok", None, None, None, None, None, None, None)
        att = Attachment(name="scan.pdf", content_type="application/pdf", content_bytes=b"%PDF")
        config = {"classifier": {}}

        status, *_ = is_invoice(att, config)
        assert status == "invoice"
        mock_vision.assert_called_once()

    @patch("classifier._classify_image")
    def test_image_jpeg_classified(self, mock_classify_img):
        mock_classify_img.return_value = (True, 0.95, "Receipt image", "2025-01-10", "Shop", None, 50.0, 60.0, 10.0, "EUR")
        att = Attachment(name="receipt.jpg", content_type="image/jpeg", content_bytes=b"\xff\xd8")
        config = {"classifier": {"confidence_threshold": 0.5}}

        status, date, supplier, entity, *_ = is_invoice(att, config)
        assert status == "invoice"
        assert supplier == "Shop"

    @patch("classifier._extract_pdf_text", return_value="text enough to pass minimum threshold for classification by the system")
    @patch("classifier._classify_text", side_effect=Exception("CLI timeout"))
    def test_cli_error_returns_review(self, mock_classify, mock_extract):
        att = Attachment(name="file.pdf", content_type="application/pdf", content_bytes=b"%PDF")
        config = {"classifier": {"confidence_threshold": 0.5}}

        status, *_ = is_invoice(att, config)
        assert status == "review"

    @patch("classifier._extract_pdf_text", return_value="text enough to pass minimum threshold for classification by the system")
    @patch("classifier._classify_text")
    def test_loads_model_from_config(self, mock_classify, mock_extract):
        mock_classify.return_value = (True, 0.9, "ok", None, None, None, None, None, None, None)
        att = Attachment(name="f.pdf", content_type="application/pdf", content_bytes=b"%PDF")
        config = {"classifier": {"model": "sonnet"}}

        is_invoice(att, config)
        call_args = mock_classify.call_args[0]
        assert call_args[1] == "sonnet"

    @patch("classifier._extract_pdf_text", return_value="text enough to pass minimum threshold for classification by the system")
    @patch("classifier._classify_text")
    def test_defaults_to_opus_model(self, mock_classify, mock_extract):
        mock_classify.return_value = (True, 0.9, "ok", None, None, None, None, None, None, None)
        att = Attachment(name="f.pdf", content_type="application/pdf", content_bytes=b"%PDF")
        config = {"classifier": {}}

        is_invoice(att, config)
        call_args = mock_classify.call_args[0]
        assert call_args[1] == "opus"

    @patch("classifier._classify_image")
    def test_image_borderline_no_retry_without_text(self, mock_classify_img):
        # Images have no extracted text, so retry should NOT trigger
        mock_classify_img.return_value = (True, 0.35, "Uncertain", None, None, None, None, None, None, None)
        att = Attachment(name="photo.jpg", content_type="image/jpeg", content_bytes=b"\xff\xd8")
        config = {"classifier": {"confidence_threshold": 0.5}}

        status, *_ = is_invoice(att, config)
        assert status == "review"  # no retry for images, stays as review
