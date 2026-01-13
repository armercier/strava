import requests
import json
from pathlib import Path

from strava_config import load_client_credentials, load_auth_code

"""
Short description:
Utilities to exchange a Strava OAuth authorization code for access and refresh tokens
and persist them to disk for later use.

Detailed description:
This module encapsulates a minimal one-time flow to obtain OAuth tokens from Strava.
It posts an authorization code, along with client credentials, to Strava's token endpoint
and writes a small JSON file containing only the needed tokens and expiry timestamp.

Public API:
- exchange_code_for_tokens():
    Exchange an authorization code for access/refresh tokens and save them to disk.
    - Makes an HTTP POST to https://www.strava.com/oauth/token using the configured
      client ID, client secret and authorization code.
    - On success writes a JSON file with the following keys:
        {
            "access_token": "<short-lived access token>",
            "refresh_token": "<refresh token>",
            "expires_at": <unix timestamp>
    - The function calls resp.raise_for_status(), so HTTP errors propagate as requests.HTTPError.
    - Side effect: writes the token file to TOKEN_PATH.

Configuration:
- CLIENT_ID, CLIENT_SECRET, AUTH_CODE: must be set before calling the exchange function.
  For security, do not hardcode secrets or authorization codes in source control — prefer
  loading them from environment variables or a secure secrets manager.
- TOKEN_PATH: pathlib.Path where the resulting tokens JSON will be saved.

Dependencies:
- requests
- pathlib (stdlib)
- json (stdlib)

Usage notes:
- Intended to be run once to obtain tokens. After that, use the refresh token flow to
  obtain new access tokens when the saved expires_at timestamp elapses.
- Ensure the saved tokens file is protected and not checked into version control.
"""

# Loaded from env vars or strava_client.json (see strava_config.py)
CLIENT_ID, CLIENT_SECRET = load_client_credentials()
AUTH_CODE = load_auth_code()

TOKEN_PATH = Path("strava_tokens.json")


def exchange_code_for_tokens():
    """Run once: exchange auth code for access + refresh tokens."""
    url = "https://www.strava.com/oauth/token"
    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": AUTH_CODE,
        "grant_type": "authorization_code",
    }

    resp = requests.post(url, data=payload)
    resp.raise_for_status()
    data = resp.json()

    # Keep only what we need
    tokens = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": data["expires_at"],  # unix timestamp
    }

    TOKEN_PATH.write_text(json.dumps(tokens, indent=2))
    print("Saved tokens to", TOKEN_PATH.resolve())


if __name__ == "__main__":
    exchange_code_for_tokens()
