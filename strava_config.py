from __future__ import annotations

"""
strava_config.py

Central place for loading Strava API credentials without hardcoding them in
tracked source files. Supports two sources:

1) Environment variables (preferred for secrets):
       STRAVA_CLIENT_ID
       STRAVA_CLIENT_SECRET
       STRAVA_AUTH_CODE (only needed for the one-time auth exchange)
2) Optional JSON file strava_client.json (not to be committed) with:
       { "client_id": "...", "client_secret": "...", "auth_code": "..." }

Usage:
    from strava_config import load_client_credentials
    CLIENT_ID, CLIENT_SECRET = load_client_credentials()
    AUTH_CODE = load_auth_code()  # only for the auth exchange script
"""

import json
import os
from pathlib import Path
from typing import Tuple

CLIENT_JSON_PATH = Path("strava_client.json")
CLIENT_ID_ENV = "STRAVA_CLIENT_ID"
CLIENT_SECRET_ENV = "STRAVA_CLIENT_SECRET"
AUTH_CODE_ENV = "STRAVA_AUTH_CODE"


def load_client_credentials(
    *, json_path: Path = CLIENT_JSON_PATH
) -> Tuple[str, str]:
    """Return (client_id, client_secret) from env vars or a local JSON file."""
    env_id = os.getenv(CLIENT_ID_ENV)
    env_secret = os.getenv(CLIENT_SECRET_ENV)
    if env_id and env_secret:
        return env_id, env_secret

    if json_path.exists():
        data = json.loads(json_path.read_text())
        client_id = str(data.get("client_id") or "").strip()
        client_secret = str(data.get("client_secret") or "").strip()
        if client_id and client_secret:
            return client_id, client_secret

    raise RuntimeError(
        f"Missing Strava credentials. Set {CLIENT_ID_ENV} and {CLIENT_SECRET_ENV} "
        "environment variables, or create strava_client.json with "
        '{"client_id": "...", "client_secret": "..."} (do not commit secrets).'
    )


def load_auth_code(*, json_path: Path = CLIENT_JSON_PATH) -> str:
    """Return the one-time auth code from env or optional JSON; used only by strava_auth."""
    env_code = os.getenv(AUTH_CODE_ENV)
    if env_code:
        return env_code

    if json_path.exists():
        data = json.loads(json_path.read_text())
        code = str(data.get("auth_code") or "").strip()
        if code:
            return code

    raise RuntimeError(
        f"Missing Strava auth code. Set {AUTH_CODE_ENV} environment variable or add "
        '"auth_code" to strava_client.json (remove after use; do not commit secrets).'
    )
