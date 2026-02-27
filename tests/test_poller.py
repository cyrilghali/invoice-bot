"""Tests for src/poller.py — link extraction, filename derivation, Email properties."""

import re
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, PropertyMock

import pytest
import requests

from poller import (
    _AnchorExtractor,
    _filename_from_response,
    Attachment,
    Email,
    GraphClient,
    INVOICE_MIME_TYPES,
)


# ---------------------------------------------------------------------------
# _AnchorExtractor
# ---------------------------------------------------------------------------

class TestAnchorExtractor:
    def test_extracts_href(self):
        parser = _AnchorExtractor()
        parser.feed('<a href="https://example.com/invoice.pdf">Download</a>')
        assert "https://example.com/invoice.pdf" in parser.links

    def test_multiple_links(self):
        parser = _AnchorExtractor()
        parser.feed(
            '<a href="https://a.com">A</a>'
            '<a href="https://b.com">B</a>'
        )
        assert len(parser.links) == 2

    def test_no_links(self):
        parser = _AnchorExtractor()
        parser.feed("<p>No links here</p>")
        assert parser.links == []

    def test_ignores_non_a_tags(self):
        parser = _AnchorExtractor()
        parser.feed('<img src="https://img.com/logo.png">')
        assert parser.links == []

    def test_skips_empty_href(self):
        parser = _AnchorExtractor()
        parser.feed('<a href="">empty</a><a>no href</a>')
        assert parser.links == []


# ---------------------------------------------------------------------------
# Email.received_datetime
# ---------------------------------------------------------------------------

class TestEmailDatetime:
    def test_parses_z_suffix(self):
        email = Email(
            email_id="1", sender="a@b.com", subject="s",
            received_at="2025-03-15T10:30:00Z",
        )
        dt = email.received_datetime
        assert dt.year == 2025
        assert dt.month == 3
        assert dt.day == 15
        assert dt.hour == 10
        assert dt.minute == 30

    def test_parses_offset(self):
        email = Email(
            email_id="2", sender="a@b.com", subject="s",
            received_at="2025-06-01T08:00:00+02:00",
        )
        dt = email.received_datetime
        assert dt.year == 2025
        assert dt.month == 6


# ---------------------------------------------------------------------------
# _filename_from_response
# ---------------------------------------------------------------------------

class TestFilenameFromResponse:
    def _mock_response(self, headers=None, url="https://example.com/file.pdf"):
        resp = MagicMock(spec=requests.Response)
        resp.headers = headers or {}
        resp.url = url
        return resp

    def test_content_disposition_filename(self):
        resp = self._mock_response(
            headers={"Content-Disposition": 'attachment; filename="invoice_123.pdf"'}
        )
        assert _filename_from_response(resp, "https://example.com", "application/pdf") == "invoice_123.pdf"

    def test_content_disposition_filename_star(self):
        resp = self._mock_response(
            headers={"Content-Disposition": "attachment; filename*=UTF-8''facture%20mars.pdf"}
        )
        result = _filename_from_response(resp, "https://example.com", "application/pdf")
        assert result == "facture mars.pdf"

    def test_url_path_fallback(self):
        resp = self._mock_response(
            headers={},
            url="https://example.com/downloads/report_2025.pdf",
        )
        result = _filename_from_response(resp, "https://example.com", "application/pdf")
        assert result == "report_2025.pdf"

    def test_generic_fallback(self):
        resp = self._mock_response(
            headers={},
            url="https://example.com/api/download",  # no extension in URL
        )
        result = _filename_from_response(resp, "https://example.com", "application/pdf")
        assert result == "invoice.pdf"

    def test_generic_fallback_jpeg(self):
        resp = self._mock_response(
            headers={},
            url="https://example.com/api/image",
        )
        result = _filename_from_response(resp, "https://example.com", "image/jpeg")
        assert result == "invoice.jpg"


# ---------------------------------------------------------------------------
# GraphClient._extract_invoice_links
# ---------------------------------------------------------------------------

class TestExtractInvoiceLinks:
    def _make_client(self):
        with patch("poller.get_access_token", return_value="fake-token"):
            return GraphClient("test-client-id")

    def test_html_body_with_keywords(self):
        client = self._make_client()
        body = {
            "contentType": "html",
            "content": '<a href="https://billing.com/download/facture-123">Télécharger</a>',
        }
        urls = client._extract_invoice_links(body, ["facture"])
        assert len(urls) == 1
        assert "facture-123" in urls[0]

    def test_plain_text_body(self):
        client = self._make_client()
        body = {
            "contentType": "text",
            "content": "Download your invoice here: https://example.com/download/invoice-456",
        }
        urls = client._extract_invoice_links(body, ["invoice"])
        assert len(urls) == 1

    def test_no_matching_keywords(self):
        client = self._make_client()
        body = {
            "contentType": "html",
            "content": '<a href="https://example.com/unrelated">Click</a>',
        }
        urls = client._extract_invoice_links(body, ["facture", "invoice"])
        assert urls == []

    def test_deduplication(self):
        client = self._make_client()
        body = {
            "contentType": "html",
            "content": (
                '<a href="https://example.com/facture">Link1</a>'
                '<a href="https://example.com/facture">Link2</a>'
            ),
        }
        urls = client._extract_invoice_links(body, ["facture"])
        assert len(urls) == 1

    def test_empty_body(self):
        client = self._make_client()
        body = {"contentType": "html", "content": ""}
        urls = client._extract_invoice_links(body, ["facture"])
        assert urls == []


# ---------------------------------------------------------------------------
# GraphClient._download_link
# ---------------------------------------------------------------------------

class TestIsPrivateUrl:
    """Tests for SSRF protection in _download_link."""

    def _make_client(self):
        with patch("poller.get_access_token", return_value="fake-token"):
            return GraphClient("test-client-id")

    @patch("poller.socket.getaddrinfo", return_value=[
        (2, 1, 6, "", ("127.0.0.1", 0)),
    ])
    def test_loopback_rejected(self, mock_dns):
        client = self._make_client()
        assert client._is_private_url("http://localhost/invoice.pdf") is True

    @patch("poller.socket.getaddrinfo", return_value=[
        (2, 1, 6, "", ("10.0.0.1", 0)),
    ])
    def test_private_ip_rejected(self, mock_dns):
        client = self._make_client()
        assert client._is_private_url("http://internal.corp/invoice.pdf") is True

    @patch("poller.socket.getaddrinfo", return_value=[
        (2, 1, 6, "", ("169.254.1.1", 0)),
    ])
    def test_link_local_rejected(self, mock_dns):
        client = self._make_client()
        assert client._is_private_url("http://metadata.internal/latest") is True

    @patch("poller.socket.getaddrinfo", return_value=[
        (2, 1, 6, "", ("93.184.216.34", 0)),
    ])
    def test_public_ip_allowed(self, mock_dns):
        client = self._make_client()
        assert client._is_private_url("https://example.com/invoice.pdf") is False

    @patch("poller.socket.getaddrinfo", side_effect=Exception("DNS failure"))
    def test_dns_failure_rejected(self, mock_dns):
        client = self._make_client()
        assert client._is_private_url("http://unresolvable.test/x") is True


class TestDownloadLink:
    def _make_client(self):
        with patch("poller.get_access_token", return_value="fake-token"):
            return GraphClient("test-client-id")

    @patch("poller.requests.get")
    @patch.object(GraphClient, "_is_private_url", return_value=False)
    def test_successful_download(self, mock_ssrf, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {
            "Content-Type": "application/pdf",
            "Content-Disposition": 'attachment; filename="facture.pdf"',
        }
        mock_resp.content = b"%PDF-1.4 data"
        mock_resp.url = "https://example.com/facture.pdf"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        client = self._make_client()
        att = client._download_link("https://example.com/facture.pdf")
        assert att is not None
        assert att.name == "facture.pdf"
        assert att.content_type == "application/pdf"

    @patch("poller.requests.get")
    @patch.object(GraphClient, "_is_private_url", return_value=False)
    def test_unsupported_mime_returns_none(self, mock_ssrf, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "text/html"}
        mock_resp.content = b"<html>page</html>"
        mock_resp.url = "https://example.com/page"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        client = self._make_client()
        att = client._download_link("https://example.com/page")
        assert att is None

    @patch("poller.requests.get", side_effect=requests.RequestException("timeout"))
    @patch.object(GraphClient, "_is_private_url", return_value=False)
    def test_request_error_returns_none(self, mock_ssrf, mock_get):
        client = self._make_client()
        att = client._download_link("https://example.com/fail")
        assert att is None

    @patch("poller.requests.get")
    @patch.object(GraphClient, "_is_private_url", return_value=False)
    def test_oversized_content_length_returns_none(self, mock_ssrf, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {
            "Content-Type": "application/pdf",
            "Content-Length": str(25 * 1024 * 1024),  # 25 MB
        }
        mock_resp.url = "https://example.com/huge.pdf"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        client = self._make_client()
        att = client._download_link("https://example.com/huge.pdf")
        assert att is None

    @patch.object(GraphClient, "_is_private_url", return_value=True)
    def test_private_url_blocked(self, mock_ssrf):
        client = self._make_client()
        att = client._download_link("http://192.168.1.1/invoice.pdf")
        assert att is None
