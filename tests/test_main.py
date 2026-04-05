"""Tests for src/main.py — source discovery and send_report orchestration."""

import os
from unittest.mock import patch, MagicMock, call
from datetime import datetime, timezone

import pytest

from main import _load_source_module, _build_instance_config, _register_sources, send_report


# ---------------------------------------------------------------------------
# Source discovery
# ---------------------------------------------------------------------------

class TestLoadSourceModule:
    def test_loads_existing_module(self):
        run_fn = _load_source_module("email_source")
        assert run_fn is not None
        assert callable(run_fn)

    def test_returns_none_for_missing_module(self):
        run_fn = _load_source_module("nonexistent_module_xyz")
        assert run_fn is None

    def test_returns_none_for_module_without_run(self):
        # sources/__init__.py exists but has no run()
        run_fn = _load_source_module("__init__")
        assert run_fn is None


class TestRegisterSources:
    def test_registers_source_with_interval(self):
        scheduler = MagicMock()
        config = {
            "sources": {
                "test_inbox": {
                    "module": "email_source",
                    "interval_minutes": 10,
                    "client_id": "cid",
                    "onedrive_folder_name": "Root",
                },
            },
        }
        _register_sources(scheduler, config, "/tmp/data")
        scheduler.add_job.assert_called_once()
        call_kwargs = scheduler.add_job.call_args
        assert call_kwargs.kwargs["id"] == "source_test_inbox"

    def test_skips_source_without_interval(self):
        scheduler = MagicMock()
        config = {
            "sources": {
                "cegedim": {
                    "module": "email_source",  # exists, but no interval
                    "client_id": "cid",
                    "onedrive_folder_name": "Root",
                },
            },
        }
        _register_sources(scheduler, config, "/tmp/data")
        scheduler.add_job.assert_not_called()

    def test_skips_missing_module(self):
        scheduler = MagicMock()
        config = {
            "sources": {
                "broken": {
                    "module": "nonexistent_xyz",
                    "interval_minutes": 10,
                },
            },
        }
        _register_sources(scheduler, config, "/tmp/data")
        scheduler.add_job.assert_not_called()

    def test_two_instances_same_module(self):
        scheduler = MagicMock()
        config = {
            "sources": {
                "inbox_a": {
                    "module": "email_source",
                    "interval_minutes": 10,
                    "client_id": "cid-a",
                    "onedrive_folder_name": "Root",
                },
                "inbox_b": {
                    "module": "email_source",
                    "interval_minutes": 30,
                    "client_id": "cid-b",
                    "onedrive_folder_name": "Root",
                },
            },
        }
        _register_sources(scheduler, config, "/tmp/data")
        assert scheduler.add_job.call_count == 2

    def test_source_name_in_runner_config(self):
        """Verify source_name is set when the runner builds instance config at runtime."""
        from main import _build_instance_config
        config = {
            "sources": {
                "my_inbox": {
                    "module": "email_source",
                    "interval_minutes": 10,
                    "client_id": "cid",
                    "onedrive_folder_name": "Root",
                },
            },
        }
        instance_config = _build_instance_config(config, "my_inbox")
        assert instance_config["source_name"] == "my_inbox"
        # Original config should not be mutated
        assert "source_name" not in config["sources"]["my_inbox"]

    def test_no_sources_configured(self):
        scheduler = MagicMock()
        _register_sources(scheduler, {}, "/tmp/data")
        scheduler.add_job.assert_not_called()

    def test_source_runner_catches_exceptions(self):
        """Verify the runner wrapper catches source exceptions."""
        scheduler = MagicMock()
        config = {
            "sources": {
                "test_inbox": {
                    "module": "email_source",
                    "interval_minutes": 10,
                    "client_id": "cid",
                    "onedrive_folder_name": "Root",
                },
            },
        }
        _register_sources(scheduler, config, "/tmp/data")
        # Get the runner function that was registered
        runner = scheduler.add_job.call_args[0][0]
        # Patch load_config (runner reloads config) and the source to raise
        with patch("main.load_config", return_value=config), \
             patch("sources.email_source.run", side_effect=Exception("boom")):
            runner()  # Should not raise


# ---------------------------------------------------------------------------
# send_report
# ---------------------------------------------------------------------------

class TestSendReport:
    _BASE_CONFIG = {
        "microsoft": {"client_id": "cid"},
        "onedrive": {"folder_name": "Root"},
    }

    @patch("main.load_config", return_value=_BASE_CONFIG)
    @patch("main.db")
    def test_skips_if_already_sent(self, mock_db, _mock_cfg, monkeypatch):
        monkeypatch.setenv("DATA_DIR", "/tmp/test-data")
        mock_db.has_monthly_report_been_sent.return_value = True

        send_report()
        mock_db.get_unreported_invoices.assert_not_called()

    @patch("main.load_config", return_value=_BASE_CONFIG)
    @patch("main.db")
    def test_skips_if_no_invoices(self, mock_db, _mock_cfg, monkeypatch):
        monkeypatch.setenv("DATA_DIR", "/tmp/test-data")
        mock_db.has_monthly_report_been_sent.return_value = False
        mock_db.get_unreported_invoices.return_value = []

        send_report()
        mock_db.save_monthly_report.assert_called_once()

    @patch("main.load_config", return_value=_BASE_CONFIG)
    @patch("main.upload_attachment", return_value=("fid", "https://link"))
    @patch("main.build_monthly_excel", return_value=b"xlsx-bytes")
    @patch("main.db")
    def test_builds_and_uploads_report(self, mock_db, mock_excel, mock_upload, _mock_cfg, monkeypatch):
        monkeypatch.setenv("DATA_DIR", "/tmp/test-data")
        mock_db.has_monthly_report_been_sent.return_value = False
        mock_db.get_unreported_invoices.return_value = [
            {"id": 1, "filename": "inv.pdf"},
            {"id": 2, "filename": "inv2.pdf"},
        ]

        send_report()
        mock_excel.assert_called_once()
        mock_upload.assert_called_once()
        mock_db.mark_invoices_reported.assert_called_once_with(
            "/tmp/test-data", [1, 2]
        )
        mock_db.save_monthly_report.assert_called_once()
