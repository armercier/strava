from datetime import datetime, timedelta
from pathlib import Path
from typing import Set

import pandas as pd

"""
fetch_hr_stream.py

Module for retrieving heart rate (HR) and time streams from the Strava API for a
given activity, saving token state to disk, and providing a simple script
entrypoint that writes cached CSVs for a date-filtered range of activities.

Description
-----------
This module performs the following tasks:
- Loads OAuth tokens from a local JSON file (TOKEN_PATH).
- Refreshes the Strava access token when it is expired or near expiry.
- Fetches the "time" and "heartrate" streams for a specified activity using the
    Strava API.
- Provides a __main__ script behavior that saves the fetched HR/time samples
    to CSV for all activities within a configurable date window.

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
- Configure FETCH_START_DAYS_AGO / FETCH_END_DAYS_AGO below.
- The script will fetch streams for matching activities in that window and write
    CSVs named "hr_stream_<id>.csv" with columns "t_s" and "hr_bpm".

Notes & edge cases
------------------
- The module expects the Strava API to return both "time" and "heartrate"
    streams. If a given activity lacks a heart rate stream or the stream is
    missing, a KeyError will be raised when attempting to index streams["heartrate"].
- Network errors and API errors propagate as requests exceptions to the caller.
- Token expiry is checked with a 60 second safety margin to avoid races.
"""


from garmin_streams import fetch_hr_stream as fetch_garmin_hr_stream


TOKEN_PATH = Path("strava_tokens.json")
HR_DIR = Path("hr_streams")
CSV_PATH = Path("activities_clean.csv")

# Inclusive window in days ago, e.g., 7 to 30 fetches last month's worth.
FETCH_START_DAYS_AGO = 2
FETCH_END_DAYS_AGO = 11 * 365

TARGET_SPORTS: Set[str] = {
    "Run",
    "Ride",
    "Walk",
    "Hike",
    "NordicSki",
    "BackcountrySki",
    "AlpineSki",
    "SkiTouring",
    "StandUpPaddling",
    "Kayaking",
    "MountainBikeRide",
    "TrailRun",
}


def fetch_hr_stream(activity_id: int):
    return fetch_garmin_hr_stream(activity_id)


def get_hr_stream_cached(activity_id: int, use_cache: bool = True):
    """
    Return HR/time streams for an activity, using a cached CSV when available.

    If use_cache is True and hr_streams/hr_stream_<id>.csv exists, reuse it to
    avoid an API call. Otherwise fetch from Strava, save to the cache folder,
    and return the data.
    """
    HR_DIR.mkdir(exist_ok=True)
    cached_path = HR_DIR / f"hr_stream_{activity_id}.csv"
    legacy_path = Path(f"hr_stream_{activity_id}.csv")

    if use_cache and cached_path.exists():
        df = pd.read_csv(cached_path)
        return df["t_s"].tolist(), df["hr_bpm"].tolist(), cached_path

    if use_cache and not cached_path.exists() and legacy_path.exists():
        df = pd.read_csv(legacy_path)
        df.to_csv(cached_path, index=False)  # normalize location
        return df["t_s"].tolist(), df["hr_bpm"].tolist(), cached_path

    time_s, hr_bpm = fetch_hr_stream(activity_id)
    df = pd.DataFrame({"t_s": time_s, "hr_bpm": hr_bpm})
    df.to_csv(cached_path, index=False)
    return time_s, hr_bpm, cached_path


def load_and_filter_activities(start_days_ago: int, end_days_ago: int) -> pd.DataFrame:
    """Load CSV and filter on sport + date window (only ones with HR data flagged)."""
    if not CSV_PATH.exists():
        raise SystemExit(f"CSV file not found: {CSV_PATH}")

    df = pd.read_csv(CSV_PATH)

    if "sport_type" in df.columns:
        sport_col = "sport_type"
    elif "sport" in df.columns:
        sport_col = "sport"
    elif "type" in df.columns:
        sport_col = "type"
    else:
        raise RuntimeError("No sport column found (expected sport_type/sport/type).")

    if "date" in df.columns:
        df["activity_date"] = pd.to_datetime(df["date"]).dt.date
    elif "start_date_local" in df.columns:
        df["activity_date"] = pd.to_datetime(df["start_date_local"]).dt.date
    else:
        raise RuntimeError(
            "No date column found (expected 'date' or 'start_date_local')."
        )

    recent_days, oldest_days = sorted((start_days_ago, end_days_ago))
    today = datetime.today().date()
    window_start = today - timedelta(days=oldest_days)
    window_end = today - timedelta(days=recent_days)

    if "has_heartrate" in df.columns:
        hr_mask = df["has_heartrate"] == True
    elif "average_heartrate" in df.columns:
        hr_mask = df["average_heartrate"].notna()
    else:
        hr_mask = True  # keep all if no HR indicator column is present

    mask = (
        df[sport_col].isin(TARGET_SPORTS)
        & hr_mask
        & df["activity_date"].between(window_start, window_end)
    )
    sub = df.loc[mask, ["id", sport_col, "activity_date"]].copy()
    sub["id"] = sub["id"].astype(int)
    return sub.sort_values("activity_date")


def fetch_hr_streams_for_range(
    start_days_ago: int = FETCH_START_DAYS_AGO,
    end_days_ago: int = FETCH_END_DAYS_AGO,
    use_cache: bool = True,
) -> None:
    """Download HR streams for all matching activities in the date window."""
    df = load_and_filter_activities(start_days_ago, end_days_ago)
    if df.empty:
        print("No activities matching filters (sport + date window).")
        return

    HR_DIR.mkdir(exist_ok=True)
    fetched = 0
    cached = 0
    skipped = 0

    for _, row in df.iterrows():
        act_id = int(row["id"])
        sport = str(row.iloc[1])
        act_date = row["activity_date"]
        cached_path = HR_DIR / f"hr_stream_{act_id}.csv"
        was_cached = cached_path.exists() if use_cache else False

        print(f"{act_id} ({sport}, {act_date}): fetching HR stream...")
        try:
            _, _, path = get_hr_stream_cached(act_id, use_cache=use_cache)
            if use_cache and was_cached:
                cached += 1
                print(f"  -> cached at {path.name}")
            else:
                fetched += 1
                print(f"  -> saved {path.name}")
        except Exception as e:
            print(f"  -> skipped (stream error: {e})")
            skipped += 1
            continue

    total = len(df)
    print(
        f"Done. Downloaded {fetched}, cached {cached}, skipped {skipped}, total considered {total}."
    )


if __name__ == "__main__":
    # Adjust FETCH_START_DAYS_AGO / FETCH_END_DAYS_AGO above to control the window.
    fetch_hr_streams_for_range(
        start_days_ago=FETCH_START_DAYS_AGO,
        end_days_ago=FETCH_END_DAYS_AGO,
        use_cache=True,
    )
