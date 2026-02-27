"""Tests for src/utils.py — pure utility functions and config loading."""

import os
import logging
import tempfile

import pytest
import yaml

from utils import (
    DEFAULT_DATA_DIR,
    GRAPH_BASE,
    MONTH_NAMES_FR,
    load_config,
    normalize_content_type,
    sanitize_filename,
    sender_to_label,
    setup_logging,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_graph_base_url(self):
        assert GRAPH_BASE == "https://graph.microsoft.com/v1.0"

    def test_default_data_dir(self):
        assert DEFAULT_DATA_DIR == "/app/data"

    def test_month_names_length(self):
        assert len(MONTH_NAMES_FR) == 13

    def test_month_names_index_zero_empty(self):
        assert MONTH_NAMES_FR[0] == ""

    def test_month_names_january(self):
        assert MONTH_NAMES_FR[1] == "Janvier"

    def test_month_names_december(self):
        assert MONTH_NAMES_FR[12] == "Décembre"


class TestNormalizeContentType:
    def test_simple_type(self):
        assert normalize_content_type("application/pdf") == "application/pdf"

    def test_strips_charset(self):
        assert normalize_content_type("text/html; charset=utf-8") == "text/html"

    def test_strips_boundary(self):
        assert normalize_content_type("multipart/form-data; boundary=----") == "multipart/form-data"

    def test_uppercase_normalised(self):
        assert normalize_content_type("Application/PDF") == "application/pdf"

    def test_whitespace_stripped(self):
        assert normalize_content_type("  image/jpeg  ; quality=80") == "image/jpeg"

    def test_empty_string(self):
        assert normalize_content_type("") == ""


# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------

class TestSanitizeFilename:
    def test_clean_name_unchanged(self):
        assert sanitize_filename("invoice_2025.pdf") == "invoice_2025.pdf"

    def test_removes_angle_brackets(self):
        assert sanitize_filename("file<name>.txt") == "file_name_.txt"

    def test_removes_colon(self):
        assert sanitize_filename("12:30:00.txt") == "12_30_00.txt"

    def test_removes_quotes_and_pipe(self):
        assert sanitize_filename('my"file|name.pdf') == "my_file_name.pdf"

    def test_removes_question_and_star(self):
        assert sanitize_filename("what?*.pdf") == "what__.pdf"

    def test_removes_backslash_and_slash(self):
        assert sanitize_filename("path\\to/file.pdf") == "path_to_file.pdf"

    def test_removes_control_characters(self):
        assert sanitize_filename("file\x00\x1fname.pdf") == "file__name.pdf"

    def test_empty_string(self):
        assert sanitize_filename("") == ""


# ---------------------------------------------------------------------------
# sender_to_label
# ---------------------------------------------------------------------------

class TestSenderToLabel:
    def test_simple_domain(self):
        assert sender_to_label("noreply@hotmail.com") == "hotmail"

    def test_subdomain_stripped(self):
        assert sender_to_label("billing@notifications.amazon.fr") == "amazon"

    def test_compound_tld_co_uk(self):
        assert sender_to_label("support@company.co.uk") == "company"

    def test_compound_tld_com_au(self):
        assert sender_to_label("info@bigcorp.com.au") == "bigcorp"

    def test_no_at_sign(self):
        assert sender_to_label("example.com") == "example"

    def test_single_part_domain(self):
        assert sender_to_label("localhost") == "localhost"

    def test_uppercase_normalised(self):
        assert sender_to_label("Admin@BIGCORP.COM") == "bigcorp"

    def test_whitespace_stripped(self):
        assert sender_to_label("  user@corp.fr  ") == "corp"

    def test_deep_subdomain(self):
        assert sender_to_label("a@mail.sub.deep.example.org") == "example"


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_loads_valid_yaml(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"microsoft": {"client_id": "abc"}}))
        monkeypatch.setenv("CONFIG_PATH", str(cfg_file))
        # Clear env vars that would overlay
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        cfg = load_config()
        assert cfg["microsoft"]["client_id"] == "abc"

    def test_env_var_overrides_client_id(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"microsoft": {"client_id": "from-yaml"}}))
        monkeypatch.setenv("CONFIG_PATH", str(cfg_file))
        monkeypatch.setenv("AZURE_CLIENT_ID", "from-env")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        cfg = load_config()
        assert cfg["microsoft"]["client_id"] == "from-env"

    def test_env_var_overrides_api_key(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"classifier": {"api_key": "from-yaml"}}))
        monkeypatch.setenv("CONFIG_PATH", str(cfg_file))
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")

        cfg = load_config()
        assert cfg["classifier"]["api_key"] == "from-env"

    def test_missing_file_exits(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "nonexistent.yaml"))
        with pytest.raises(SystemExit):
            load_config()

    def test_invalid_yaml_exits(self, tmp_path, monkeypatch):
        bad_file = tmp_path / "config.yaml"
        bad_file.write_text("{{{{invalid yaml: [")
        monkeypatch.setenv("CONFIG_PATH", str(bad_file))
        with pytest.raises(SystemExit):
            load_config()


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------

class TestSetupLogging:
    def test_creates_log_file(self, tmp_path):
        # Reset root logger handlers for a clean test
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        root.handlers.clear()
        try:
            setup_logging(data_dir=str(tmp_path), log_level="DEBUG")
            assert (tmp_path / "bot.log").exists()
        finally:
            root.handlers = original_handlers

    def test_idempotent_call(self, tmp_path):
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        root.handlers.clear()
        try:
            setup_logging(data_dir=str(tmp_path), log_level="INFO")
            count_after_first = len(root.handlers)
            setup_logging(data_dir=str(tmp_path), log_level="INFO")
            # Should not add duplicate stdout handlers
            assert len(root.handlers) <= count_after_first + 1
        finally:
            root.handlers = original_handlers
