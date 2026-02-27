"""Tests for src/auth_setup.py — token cache and authentication flow."""

import os
import json
from unittest.mock import patch, MagicMock

import pytest

from auth_setup import get_token_cache_path, load_token_cache, save_token_cache, get_access_token


# ---------------------------------------------------------------------------
# get_token_cache_path
# ---------------------------------------------------------------------------

class TestGetTokenCachePath:
    def test_default_path(self, monkeypatch):
        monkeypatch.delenv("DATA_DIR", raising=False)
        path = get_token_cache_path()
        assert path.endswith("ms_token_cache.json")
        assert "/app/data/" in path

    def test_custom_data_dir(self, monkeypatch):
        monkeypatch.setenv("DATA_DIR", "/custom/dir")
        path = get_token_cache_path()
        assert path == "/custom/dir/ms_token_cache.json"


# ---------------------------------------------------------------------------
# load_token_cache / save_token_cache
# ---------------------------------------------------------------------------

class TestTokenCacheIO:
    def test_load_empty_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        cache = load_token_cache()
        # Should return an empty cache without error
        assert cache is not None

    def test_save_and_reload(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        cache = load_token_cache()

        # Simulate a state change so save_token_cache writes
        cache.has_state_changed = True
        # Force some internal state so serialize returns non-empty
        cache.deserialize('{"dummy": "data"}')
        cache.has_state_changed = True

        save_token_cache(cache)

        cache_path = tmp_path / "ms_token_cache.json"
        assert cache_path.exists()

    def test_save_skips_unchanged_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        cache = load_token_cache()
        cache.has_state_changed = False

        save_token_cache(cache)

        cache_path = tmp_path / "ms_token_cache.json"
        assert not cache_path.exists()


# ---------------------------------------------------------------------------
# get_access_token
# ---------------------------------------------------------------------------

class TestGetAccessToken:
    @patch("auth_setup.save_token_cache")
    @patch("auth_setup.build_app")
    @patch("auth_setup.load_token_cache")
    def test_silent_acquisition(self, mock_load, mock_build, mock_save):
        mock_cache = MagicMock()
        mock_load.return_value = mock_cache

        mock_app = MagicMock()
        mock_app.get_accounts.return_value = [{"username": "user@test.com"}]
        mock_app.acquire_token_silent.return_value = {"access_token": "cached-token"}
        mock_build.return_value = mock_app

        token = get_access_token("test-client-id")
        assert token == "cached-token"
        mock_app.initiate_device_flow.assert_not_called()

    @patch("auth_setup.save_token_cache")
    @patch("auth_setup.build_app")
    @patch("auth_setup.load_token_cache")
    def test_device_code_fallback(self, mock_load, mock_build, mock_save):
        mock_cache = MagicMock()
        mock_load.return_value = mock_cache

        mock_app = MagicMock()
        mock_app.get_accounts.return_value = []  # No cached accounts
        mock_app.initiate_device_flow.return_value = {
            "user_code": "ABC123",
            "message": "Go to https://microsoft.com/devicelogin and enter ABC123",
        }
        mock_app.acquire_token_by_device_flow.return_value = {"access_token": "new-token"}
        mock_build.return_value = mock_app

        token = get_access_token("test-client-id")
        assert token == "new-token"
        mock_app.initiate_device_flow.assert_called_once()

    @patch("auth_setup.build_app")
    @patch("auth_setup.load_token_cache")
    def test_device_flow_error_raises(self, mock_load, mock_build):
        mock_cache = MagicMock()
        mock_load.return_value = mock_cache

        mock_app = MagicMock()
        mock_app.get_accounts.return_value = []
        mock_app.initiate_device_flow.return_value = {
            "error_description": "Invalid client ID",
        }
        mock_build.return_value = mock_app

        with pytest.raises(RuntimeError, match="Failed to initiate device flow"):
            get_access_token("bad-client-id")

    @patch("auth_setup.save_token_cache")
    @patch("auth_setup.build_app")
    @patch("auth_setup.load_token_cache")
    def test_auth_failure_raises(self, mock_load, mock_build, mock_save):
        mock_cache = MagicMock()
        mock_load.return_value = mock_cache

        mock_app = MagicMock()
        mock_app.get_accounts.return_value = []
        mock_app.initiate_device_flow.return_value = {
            "user_code": "ABC",
            "message": "Go to...",
        }
        mock_app.acquire_token_by_device_flow.return_value = {
            "error": "auth_failed",
            "error_description": "User denied",
        }
        mock_build.return_value = mock_app

        with pytest.raises(RuntimeError, match="Authentication failed"):
            get_access_token("test-client-id")
