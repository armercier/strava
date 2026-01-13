"""
strava_fetch.py

Description:
    Utilities for loading OAuth tokens, refreshing a Strava access token when expired,
    fetching all athlete activities via the Strava API (paginated), and saving the
    resulting activities JSON to disk. Designed as a small CLI-style script: it reads
    a local tokens file, ensures a valid access token, downloads activities, and writes
    them to a file.

Public functions and behavior:
    load_tokens() -> dict
        Load and return the OAuth token dictionary from TOKEN_PATH (defaults to
        "strava_tokens.json"). The returned dict is expected to contain at least the
        keys: "access_token", "refresh_token", and "expires_at".
        Raises:
            RuntimeError: if the token file does not exist.
            json.JSONDecodeError: if the file contents are not valid JSON.

    save_tokens(tokens: dict) -> None
        Persist the given token dictionary to TOKEN_PATH using pretty JSON formatting.
        Side effects:
            Overwrites TOKEN_PATH on disk.

    refresh_access_token(tokens: dict) -> (str, dict)
        Ensure the access token is valid. If the stored "expires_at" indicates the token
        is (almost) still valid, returns the existing access token and the unchanged
        tokens dict. If expired, performs a POST to Strava's OAuth token endpoint to
        exchange the refresh token for a new access token, updates and saves the tokens
        file, and returns the new access token and the updated tokens dict.
        Notes:
            - Uses CLIENT_ID and CLIENT_SECRET to perform the refresh. These must be
              configured appropriately before calling.
            - On network or API errors, requests exceptions (e.g. requests.HTTPError)
              will be propagated.

    fetch_all_activities(access_token: str, per_page: int = 100) -> list[dict]
        Retrieve all activities for the authenticated athlete by iterating over pages
        of the Strava "athlete/activities" endpoint. Returns a list of activity objects
        (raw JSON-decoded dictionaries).
        Parameters:
            access_token: OAuth bearer token for authorization.
            per_page: number of activities to request per page (defaults to 100).
        Raises:
            requests.HTTPError: if any API request returns an error status.
        Notes:
            - Stops when an empty page is returned.
            - Prints progress (fetched page X) to stdout.

    main() -> None
        High-level orchestration: load tokens, refresh access token if needed,
        fetch all activities, and save the raw activities JSON to ACTIVITIES_PATH
        (defaults to "strava_activities.json"). Prints a summary on completion.

Files and constants:
    TOKEN_PATH (default: "strava_tokens.json")
        Input file expected to contain the OAuth token dict produced by a prior auth flow.
    ACTIVITIES_PATH (default: "strava_activities.json")
        Output file that will be written with the fetched activities JSON.
    CLIENT_ID / CLIENT_SECRET
        Credentials used to refresh the access token against Strava. Do not hardcode
        secrets in source control; prefer environment variables or a secrets manager.

Dependencies:
    - requests
    - Python standard library: time, json, pathlib

Error handling and limitations:
    - Network or API errors will raise exceptions from requests (caller can catch them).
    - The script uses simple printing for progress and basic file I/O; consider adding
      structured logging and backoff/retry logic for production use.
    - Respect Strava API rate limits; this implementation does not include rate-limit
      handling or retry-after behavior.

Example usage:
    Run as a script after obtaining and saving a valid refresh token to TOKEN_PATH:
        python strava_fetch.py
"""

import time

import json
from pathlib import Path

import requests

from strava_config import load_client_credentials


CLIENT_ID, CLIENT_SECRET = load_client_credentials()

TOKEN_PATH = Path("strava_tokens.json")
ACTIVITIES_PATH = Path("strava_activities.json")


def load_tokens():
    if not TOKEN_PATH.exists():
        raise RuntimeError(
            f"{TOKEN_PATH} not found. Run strava_auth.py first to create it."
        )
    return json.loads(TOKEN_PATH.read_text())


def save_tokens(tokens):
    TOKEN_PATH.write_text(json.dumps(tokens, indent=2))


def refresh_access_token(tokens):
    """Refresh token if expired; return valid access_token and updated tokens dict."""
    now = int(time.time())
    if tokens["expires_at"] > now + 60:
        # still valid
        return tokens["access_token"], tokens

    print("Access token expired, refreshing...")
    url = "https://www.strava.com/oauth/token"
    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
    }
    resp = requests.post(url, data=payload)
    resp.raise_for_status()
    data = resp.json()

    tokens["access_token"] = data["access_token"]
    tokens["refresh_token"] = data["refresh_token"]
    tokens["expires_at"] = data["expires_at"]
    save_tokens(tokens)
    print("Tokens refreshed and saved.")
    return tokens["access_token"], tokens


def fetch_all_activities(access_token, per_page=100):
    """Fetch all activities via pagination and return a list of dicts."""
    all_acts = []
    page = 1
    while True:
        url = "https://www.strava.com/api/v3/athlete/activities"
        params = {"page": page, "per_page": per_page}
        headers = {"Authorization": f"Bearer {access_token}"}
        resp = requests.get(url, params=params, headers=headers)
        resp.raise_for_status()
        activities = resp.json()
        if not activities:
            break
        all_acts.extend(activities)
        print(f"Fetched page {page}, {len(activities)} activities")
        page += 1
    return all_acts


def main():
    tokens = load_tokens()
    access_token, tokens = refresh_access_token(tokens)
    activities = fetch_all_activities(access_token)

    # Save raw JSON for now
    ACTIVITIES_PATH.write_text(json.dumps(activities, indent=2))
    print(f"Saved {len(activities)} activities to {ACTIVITIES_PATH.resolve()}")


if __name__ == "__main__":
    main()
