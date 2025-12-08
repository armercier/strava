import json
"""
fetch_hr_stream.py

Module for retrieving heart rate (HR) and time streams from the Strava API for a
given activity, saving token state to disk, and providing a simple script
entrypoint that writes a CSV and plots the HR trace.

Description
-----------
This module performs the following tasks:
- Loads OAuth tokens from a local JSON file (TOKEN_PATH).
- Refreshes the Strava access token when it is expired or near expiry.
- Fetches the "time" and "heartrate" streams for a specified activity using the
    Strava API.
- Provides a __main__ script behavior that saves the fetched HR/time samples
    to CSV and displays a simple matplotlib plot.

Security & configuration
------------------------
- CLIENT_ID and CLIENT_SECRET are required to refresh tokens with Strava.
    These must be kept secret; do not commit real secrets into public repos.
- TOKEN_PATH (default "strava_tokens.json") should contain a JSON object with at
    least the fields: "access_token", "refresh_token", and "expires_at" (epoch
    seconds). Example token file contents:
        {
            "access_token": "<short-lived token>",
            "refresh_token": "<refresh token>",
            "expires_at": 1700000000

Functions
---------
load_tokens() -> dict
        Read and parse the JSON token file from TOKEN_PATH and return the token
        dictionary.

save_tokens(tokens: dict) -> None
        Write the given token dictionary to TOKEN_PATH as pretty-printed JSON.
        This updates persisted tokens after a refresh.

refresh_access_token(tokens: dict) -> tuple[str, dict]
        Ensure the provided tokens dictionary contains a valid (not expiring within
        ~60s) access token. If the token is still valid, return (access_token, tokens)
        unchanged. Otherwise, call Strava's token refresh endpoint to obtain a new
        access_token, new refresh_token and new expires_at, persist the updated
        tokens via save_tokens and return (new_access_token, updated_tokens).

        Raises:
            requests.HTTPError if the token refresh HTTP request fails.

fetch_hr_stream(activity_id: int) -> tuple[list[int], list[int]]
        Fetch the "time" and "heartrate" streams for the activity with the given
        activity_id. This function will:
            - load tokens from disk
            - refresh the access token if necessary
            - call GET /api/v3/activities/{id}/streams with keys "heartrate" and "time"
            - return two parallel lists:
                 * time_s: list of seconds since activity start (integers)
                 * hr_bpm: list of heart rate samples (integers, bpm)

        The Strava API returns a JSON mapping stream types to objects that include a
        "data" list; this function extracts the "data" arrays for "time" and
        "heartrate". It will raise requests.HTTPError for non-2xx HTTP responses.

Script usage (when run as __main__)
-----------------------------------
- Set ACTIVITY_ID to a desired Strava activity id.
- The script will fetch streams, write them to a CSV named
    "hr_stream_{ACTIVITY_ID}.csv" with columns "t_s" and "hr_bpm", print a short
    summary of sample count and the first 10 points, and display a matplotlib
    plot of HR vs time.

Notes & edge cases
------------------
- The module expects the Strava API to return both "time" and "heartrate"
    streams. If a given activity lacks a heart rate stream or the stream is
    missing, a KeyError will be raised when attempting to index streams["heartrate"].
- Network errors and API errors propagate as requests exceptions to the caller.
- Token expiry is checked with a 60 second safety margin to avoid races.
"""


from pathlib import Path
import time
import pandas as pd
import matplotlib.pyplot as plt

import requests



CLIENT_ID = "127989"
CLIENT_SECRET = "bdec5d496e731b5fd70526c0215d466b3fe70df7"

TOKEN_PATH = Path("strava_tokens.json")


def load_tokens():
    return json.loads(TOKEN_PATH.read_text())


def save_tokens(tokens):
    TOKEN_PATH.write_text(json.dumps(tokens, indent=2))


def refresh_access_token(tokens):
    now = int(time.time())
    if tokens["expires_at"] > now + 60:
        return tokens["access_token"], tokens

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
    return tokens["access_token"], tokens


def fetch_hr_stream(activity_id: int):
    tokens = load_tokens()
    access_token, _ = refresh_access_token(tokens)

    url = f"https://www.strava.com/api/v3/activities/{activity_id}/streams"
    params = {
        "keys": "heartrate,time",
        "key_by_type": "true",
    }
    headers = {"Authorization": f"Bearer {access_token}"}

    resp = requests.get(url, params=params, headers=headers)
    resp.raise_for_status()
    streams = resp.json()

    # streams is a dict like:
    # {
    #   "time": {"data": [0, 1, 2, ...], "type": "time", ...},
    #   "heartrate": {"data": [100, 101, 102, ...], "type": "heartrate", ...}
    # }

    time_s = streams["time"]["data"]
    hr_bpm = streams["heartrate"]["data"]
    return time_s, hr_bpm


if __name__ == "__main__":
    ACTIVITY_ID = 16673694374  # <--- put one of your activity ids here
    t, hr = fetch_hr_stream(ACTIVITY_ID)
    df = pd.DataFrame({"t_s": t, "hr_bpm": hr})
    df.to_csv(f"hr_stream_{ACTIVITY_ID}.csv", index=False)
    print(f"Got {len(hr)} HR samples.")
    print("First 10 points:")
    for ti, hi in list(zip(t, hr))[:10]:
        print(ti, "sec ->", hi, "bpm")

    plt.plot(t, hr)
    plt.xlabel("Time (s)")
    plt.ylabel("Heart rate (bpm)")
    plt.show()