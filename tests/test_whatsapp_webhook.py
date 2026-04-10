"""Tests for the WhatsApp Cloud API webhook receiver."""

import hashlib
import hmac
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi.testclient import TestClient  # noqa: E402

from sources import whatsapp_webhook  # noqa: E402


APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
ACCESS_TOKEN = "test-access-token"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Set required env vars and a minimal config.yaml for startup."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
microsoft:
  client_id: test-client-id
onedrive:
  folder_name: Test-Folder
whatsapp:
  phone_number_id: "999999"
  allowed_senders:
    - "33611111111"
  onedrive_folder_name: Test-Folder
logging:
  log_level: WARNING
"""
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("WHATSAPP_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", VERIFY_TOKEN)
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", ACCESS_TOKEN)
    return {"config_path": str(config_path), "data_dir": str(data_dir)}


@pytest.fixture
def client(env):
    with TestClient(whatsapp_webhook.app) as c:
        yield c


def _document_payload(from_number: str = "33611111111", msg_type: str = "document") -> dict:
    media = {"id": "media-abc", "mime_type": "application/pdf"}
    if msg_type == "document":
        media["filename"] = "facture.pdf"
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": from_number, "profile": {"name": "Papa"}}],
                            "messages": [
                                {
                                    "from": from_number,
                                    "type": msg_type,
                                    msg_type: media,
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


# ---------------------------------------------------------------------------
# GET /webhook — Meta verification challenge
# ---------------------------------------------------------------------------


class TestVerify:
    def test_valid_challenge_returns_echo(self, client):
        r = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "12345",
            },
        )
        assert r.status_code == 200
        assert r.json() == 12345

    def test_wrong_token_returns_403(self, client):
        r = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong-token",
                "hub.challenge": "12345",
            },
        )
        assert r.status_code == 403

    def test_missing_params_returns_403(self, client):
        r = client.get("/webhook")
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /webhook — signature verification
# ---------------------------------------------------------------------------


class TestSignature:
    def test_missing_signature_header_is_rejected(self, client):
        r = client.post("/webhook", json=_document_payload())
        assert r.status_code == 403

    def test_wrong_signature_is_rejected(self, client):
        body = json.dumps(_document_payload()).encode()
        r = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": "sha256=deadbeef",
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 403

    def test_valid_signature_accepted(self, client):
        body = json.dumps(_document_payload()).encode()
        with patch("sources.whatsapp_webhook._download_media") as mock_dl, patch(
            "sources.whatsapp_webhook.manual_source"
        ) as mock_ms, patch("sources.whatsapp_webhook._send_text"):
            mock_dl.return_value = "/tmp/wa_fake/facture.pdf"
            # Keep the filesystem cleanup in _handle_message from crashing
            with patch("sources.whatsapp_webhook.os.unlink"), patch(
                "sources.whatsapp_webhook.os.rmdir"
            ):
                r = client.post(
                    "/webhook",
                    content=body,
                    headers={
                        "X-Hub-Signature-256": _sign(body),
                        "Content-Type": "application/json",
                    },
                )
            assert r.status_code == 200
            mock_ms.run.assert_called_once()


# ---------------------------------------------------------------------------
# POST /webhook — message dispatch
# ---------------------------------------------------------------------------


class TestMessageDispatch:
    def _post(self, client, payload):
        body = json.dumps(payload).encode()
        return client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": _sign(body),
                "Content-Type": "application/json",
            },
        )

    def test_document_message_reaches_manual_source(self, client):
        with patch("sources.whatsapp_webhook._download_media") as mock_dl, patch(
            "sources.whatsapp_webhook.manual_source"
        ) as mock_ms, patch("sources.whatsapp_webhook._send_text") as mock_reply, patch(
            "sources.whatsapp_webhook.os.unlink"
        ), patch("sources.whatsapp_webhook.os.rmdir"):
            mock_dl.return_value = "/tmp/wa_fake/facture.pdf"

            r = self._post(client, _document_payload())

            assert r.status_code == 200
            mock_dl.assert_called_once_with("media-abc", "facture.pdf", ACCESS_TOKEN)
            mock_ms.run.assert_called_once()

            call_config, call_data_dir = mock_ms.run.call_args[0]
            assert call_config["source_name"] == "whatsapp"
            assert call_config["_files"] == ["/tmp/wa_fake/facture.pdf"]
            assert call_config["_sender"] == "Papa"
            assert call_config["onedrive_folder_name"] == "Test-Folder"

            mock_reply.assert_called_once()
            assert "Reçu" in mock_reply.call_args[0][1]

    def test_image_message_gets_synthetic_filename(self, client):
        with patch("sources.whatsapp_webhook._download_media") as mock_dl, patch(
            "sources.whatsapp_webhook.manual_source"
        ), patch("sources.whatsapp_webhook._send_text"), patch(
            "sources.whatsapp_webhook.os.unlink"
        ), patch("sources.whatsapp_webhook.os.rmdir"):
            mock_dl.return_value = "/tmp/wa_fake/image-media-abc.jpg"

            r = self._post(client, _document_payload(msg_type="image"))

            assert r.status_code == 200
            mock_dl.assert_called_once()
            filename_arg = mock_dl.call_args[0][1]
            assert "media-abc" in filename_arg

    def test_non_allowlisted_sender_rejected(self, client):
        with patch("sources.whatsapp_webhook._download_media") as mock_dl, patch(
            "sources.whatsapp_webhook.manual_source"
        ) as mock_ms:
            r = self._post(client, _document_payload(from_number="33699999999"))

            assert r.status_code == 200  # always 200 to Meta
            mock_dl.assert_not_called()
            mock_ms.run.assert_not_called()

    def test_text_message_ignored(self, client):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "contacts": [{"wa_id": "33611111111", "profile": {"name": "Papa"}}],
                                "messages": [
                                    {
                                        "from": "33611111111",
                                        "type": "text",
                                        "text": {"body": "bonjour"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        with patch("sources.whatsapp_webhook._download_media") as mock_dl, patch(
            "sources.whatsapp_webhook.manual_source"
        ) as mock_ms:
            r = self._post(client, payload)

            assert r.status_code == 200
            mock_dl.assert_not_called()
            mock_ms.run.assert_not_called()

    def test_download_failure_sends_error_reply_and_returns_200(self, client):
        with patch("sources.whatsapp_webhook._download_media") as mock_dl, patch(
            "sources.whatsapp_webhook.manual_source"
        ) as mock_ms, patch("sources.whatsapp_webhook._send_text") as mock_reply:
            mock_dl.side_effect = RuntimeError("boom")

            r = self._post(client, _document_payload())

            assert r.status_code == 200
            mock_ms.run.assert_not_called()
            mock_reply.assert_called_once()
            assert "Erreur" in mock_reply.call_args[0][1]

    def test_pipeline_failure_sends_error_reply(self, client):
        with patch("sources.whatsapp_webhook._download_media") as mock_dl, patch(
            "sources.whatsapp_webhook.manual_source"
        ) as mock_ms, patch("sources.whatsapp_webhook._send_text") as mock_reply, patch(
            "sources.whatsapp_webhook.os.unlink"
        ), patch("sources.whatsapp_webhook.os.rmdir"):
            mock_dl.return_value = "/tmp/wa_fake/facture.pdf"
            mock_ms.run.side_effect = RuntimeError("pipeline crashed")

            r = self._post(client, _document_payload())

            assert r.status_code == 200
            mock_reply.assert_called_once()
            assert "Erreur" in mock_reply.call_args[0][1]


# ---------------------------------------------------------------------------
# _download_media — two-step Graph fetch
# ---------------------------------------------------------------------------


class TestDownloadMedia:
    def test_two_step_fetch_writes_temp_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "sources.whatsapp_webhook.tempfile.mkdtemp",
            lambda prefix="": str(tmp_path),
        )

        meta_resp = MagicMock()
        meta_resp.json.return_value = {"url": "https://cdn.whatsapp.com/secret"}
        meta_resp.raise_for_status = MagicMock()

        dl_resp = MagicMock()
        dl_resp.content = b"%PDF-1.4 fake invoice"
        dl_resp.raise_for_status = MagicMock()

        with patch("sources.whatsapp_webhook.requests.get", side_effect=[meta_resp, dl_resp]) as mock_get:
            path = whatsapp_webhook._download_media("media-xyz", "facture.pdf", "tok")

        assert os.path.isfile(path)
        assert open(path, "rb").read() == b"%PDF-1.4 fake invoice"
        # First call was metadata, second was actual bytes
        assert mock_get.call_args_list[0][0][0].endswith("/media-xyz")
        assert mock_get.call_args_list[1][0][0] == "https://cdn.whatsapp.com/secret"

    def test_missing_url_raises(self, monkeypatch):
        meta_resp = MagicMock()
        meta_resp.json.return_value = {}
        meta_resp.raise_for_status = MagicMock()

        with patch("sources.whatsapp_webhook.requests.get", return_value=meta_resp):
            with pytest.raises(ValueError, match="media URL missing"):
                whatsapp_webhook._download_media("media-xyz", "f.pdf", "tok")
