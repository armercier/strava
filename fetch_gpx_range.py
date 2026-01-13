from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Set

import pandas as pd
import requests

from strava_tracks import export_gpx_for_activity

"""
fetch_gpx_range.py

Fetch GPX files for a filtered set of Strava activities. This script only
downloads GPX files (caching on disk) and does not build a map.

Configure FETCH_DAYS_BACK independently from any mapping script to control
how far back you want to sync activities.
"""

# --------- CONFIG ---------
CSV_PATH = Path("activities_clean.csv")   # Strava activities CSV export
GPX_DIR = Path("gpx")                     # where GPX files will be stored

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
    "TrailRun"   # include both variants, Strava naming can vary
}

# Inclusive window in days ago, e.g. 7 to 30 fetches last month's worth
FETCH_START_DAYS_AGO = 11 * 365          # most recent bound (0 = today)
FETCH_END_DAYS_AGO = 12 * 365      # oldest bound
# --------------------------


def load_and_filter_activities(start_days_ago: int, end_days_ago: int) -> pd.DataFrame:
    """Load CSV and filter on sport + date window."""
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

    # Normalize order so either 7-30 or 30-7 works; window is inclusive.
    recent_days, oldest_days = sorted((start_days_ago, end_days_ago))
    today = datetime.today().date()
    window_start = today - timedelta(days=oldest_days)   # older bound
    window_end = today - timedelta(days=recent_days)     # more recent bound

    mask = (
        df[sport_col].isin(TARGET_SPORTS)
        & df["activity_date"].between(window_start, window_end)
    )
    sub = df.loc[mask, ["id", sport_col, "activity_date"]].copy()
    sub["id"] = sub["id"].astype(int)
    return sub.sort_values("activity_date")


def fetch_gpx_for_range(
    start_days_ago: int = FETCH_START_DAYS_AGO,
    end_days_ago: int = FETCH_END_DAYS_AGO,
) -> None:
    """Download GPX files for activities in the selected window."""
    df = load_and_filter_activities(start_days_ago, end_days_ago)
    if df.empty:
        print("No activities matching filters (sport + date window).")
        return

    GPX_DIR.mkdir(exist_ok=True)
    fetched = 0
    skipped_cached = 0

    for _, row in df.iterrows():
        act_id = int(row["id"])
        sport = str(row.iloc[1])
        act_date = row["activity_date"]
        gpx_path = GPX_DIR / f"activity_{act_id}.gpx"

        if gpx_path.exists():
            skipped_cached += 1
            print(f"{act_id} ({sport}, {act_date}): cached")
            continue

        print(f"{act_id} ({sport}, {act_date}): downloading...")
        try:
            export_gpx_for_activity(act_id)
            fetched += 1
            print(f"  -> saved {gpx_path.name}")
        except RuntimeError as e:
            msg = str(e)
            if "No lat/lon data" in msg:
                print("  -> skipped (no GPS in streams)")
                continue
            raise
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status == 404:
                print("  -> skipped (Strava returned 404)")
                continue
            if status == 429:
                print("  -> hit Strava rate limit (429). Stopping further requests.")
                break
            if status is None or (isinstance(status, int) and status >= 500):
                print(f"  -> skipped (Strava error {status or 'unknown'})")
                continue
            print(f"  -> skipped (HTTP error {status})")
            continue
        except requests.RequestException as e:
            print(f"  -> skipped (request error: {e})")
            continue

    print(f"Done. Downloaded {fetched}, cached {skipped_cached}, total considered {len(df)}.")


if __name__ == "__main__":
    fetch_gpx_for_range()
