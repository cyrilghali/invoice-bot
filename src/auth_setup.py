"""
One-time Microsoft authentication setup using Device Code Flow.

Run this once to authenticate the Outlook/Hotmail account:
    docker exec -it invoice-bot python src/auth_setup.py

The token cache is saved to /app/data/ms_token_cache.json and reused
automatically. Tokens are silently refreshed; re-authentication is only
needed if the refresh token expires (typically after 90 days of inactivity).
"""

import os
import sys
import logging

import msal

logger = logging.getLogger(__name__)

# Scopes required by the application
SCOPES = [
    "Mail.Read",
    "Files.ReadWrite",
]


def get_config() -> dict:
    from utils import load_config
    return load_config()


def get_token_cache_path() -> str:
    from utils import DEFAULT_DATA_DIR
    data_dir = os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)
    return os.path.join(data_dir, "ms_token_cache.json")


def load_token_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    cache_path = get_token_cache_path()
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            cache.deserialize(f.read())
    return cache


def save_token_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        cache_path = get_token_cache_path()
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        # Write with restrictive permissions (owner-only read/write)
        fd = os.open(cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(cache.serialize())
        except Exception:
            os.close(fd)
            raise
        logger.info("Token cache saved to %s", cache_path)


def build_app(client_id: str, cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
    return msal.PublicClientApplication(
        client_id=client_id,
        authority="https://login.microsoftonline.com/consumers",
        token_cache=cache,
    )


def get_access_token(client_id: str) -> str:
    """
    Return a valid access token, using cache if possible,
    otherwise trigger Device Code Flow.
    """
    cache = load_token_cache()
    app = build_app(client_id, cache)

    # Try silent acquisition first (uses refresh token)
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            save_token_cache(cache)
            return result["access_token"]

    # Fall back to Device Code Flow
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Failed to initiate device flow: {flow.get('error_description')}")

    print("\n" + "=" * 60)
    print("ACTION REQUIRED - Microsoft Account Login")
    print("=" * 60)
    print(flow["message"])
    print("=" * 60 + "\n")

    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        error = result.get("error_description", result.get("error", "Unknown error"))
        raise RuntimeError(f"Authentication failed: {error}")

    save_token_cache(cache)
    logger.info("Authentication successful. Token cached.")
    return result["access_token"]


if __name__ == "__main__":
    from utils import DEFAULT_DATA_DIR, setup_logging
    data_dir = os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)
    setup_logging(data_dir=data_dir, log_level="INFO")

    config = get_config()
    client_id = config["microsoft"]["client_id"]

    if not client_id or client_id in ("YOUR_CLIENT_ID_HERE", "your-client-id"):
        logger.error("Please set AZURE_CLIENT_ID in .env (or microsoft.client_id in config.yaml).")
        sys.exit(1)

    logger.info("Starting Microsoft authentication setup...")
    token = get_access_token(client_id)
    logger.info("Setup complete. The bot will now run without any further interaction.")
    logger.info("Note: Re-run this script if the bot reports authentication errors after 90 days of inactivity.")
