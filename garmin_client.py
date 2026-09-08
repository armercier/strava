from __future__ import annotations

import os
import sys
from getpass import getpass
from pathlib import Path
from typing import Any


DEFAULT_TOKENSTORE = os.getenv("GARMINTOKENS", "~/.garminconnect")


def get_garmin_client(tokenstore: str | None = None):
    """Return a logged-in Garmin Connect client.

    Saved tokens are preferred. If no valid token cache exists, credentials are
    read from GARMIN_EMAIL/GARMIN_PASSWORD or EMAIL/PASSWORD, then prompted.
    """
    try:
        from garminconnect import Garmin
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency: garminconnect. Install it in the strava env with "
            "`/Users/arno/miniforge3/envs/strava/bin/python -m pip install "
            "garminconnect curl_cffi fitparse`."
        ) from exc

    tokenstore_path = str(Path(tokenstore or DEFAULT_TOKENSTORE).expanduser())

    token_error: Exception | None = None
    client = Garmin()
    try:
        client.login(tokenstore_path)
        return client
    except Exception as exc:
        token_error = exc

    email = os.getenv("GARMIN_EMAIL") or os.getenv("EMAIL")
    password = os.getenv("GARMIN_PASSWORD") or os.getenv("PASSWORD")
    if not email and sys.stdin.isatty():
        email = input("Garmin email: ").strip()
    if not password and sys.stdin.isatty():
        password = getpass("Garmin password: ")
    if not email or not password:
        detail = f" Cached-token login failed: {token_error}" if token_error else ""
        raise RuntimeError(
            "No usable Garmin token cache and no GARMIN_EMAIL/GARMIN_PASSWORD "
            f"environment variables available.{detail}"
        )

    client = Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("Garmin MFA code: ").strip(),
    )
    client.login(tokenstore_path)
    return client


def activity_id(activity: dict[str, Any]) -> int | None:
    value = activity.get("activityId", activity.get("id"))
    if value is None:
        return None
    return int(value)
