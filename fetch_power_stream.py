from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Set

import pandas as pd

from garmin_streams import fetch_power_stream as fetch_garmin_power_stream

# --------- CONFIG ---------
TOKEN_PATH = Path("strava_tokens.json")
CSV_PATH = Path("activities_clean.csv")
POWER_DIR = Path("power_streams")

# Inclusive window in days ago, e.g., 7 to 30 fetches a last-month slice.
FETCH_START_DAYS_AGO = 0
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
# --------------------------


def fetch_power_stream(activity_id: int):
    time_s, watts = fetch_garmin_power_stream(activity_id)
    if not time_s or not watts:
        raise KeyError(f"Power stream missing for activity {activity_id}")
    return time_s, watts


def get_power_stream_cached(activity_id: int, use_cache: bool = True):
    """Return power/time streams for an activity, using a cached CSV when available."""
    POWER_DIR.mkdir(exist_ok=True)
    cached_path = POWER_DIR / f"power_stream_{activity_id}.csv"

    if use_cache and cached_path.exists():
        df = pd.read_csv(cached_path)
        return df["t_s"].tolist(), df["watts"].tolist(), cached_path

    time_s, watts = fetch_power_stream(activity_id)
    df = pd.DataFrame({"t_s": time_s, "watts": watts})
    df.to_csv(cached_path, index=False)
    return time_s, watts, cached_path


def load_and_filter_activities(start_days_ago: int, end_days_ago: int) -> pd.DataFrame:
    """Load CSV and filter on sport + date window (no HR requirement)."""
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

    # Optionally skip known non-power activities to save API calls.
    if "average_watts" in df.columns:
        power_mask = df["average_watts"].notna()
    else:
        power_mask = True

    mask = (
        df[sport_col].isin(TARGET_SPORTS)
        & power_mask
        & df["activity_date"].between(window_start, window_end)
    )
    sub = df.loc[mask, ["id", sport_col, "activity_date"]].copy()
    sub["id"] = sub["id"].astype(int)
    return sub.sort_values("activity_date")


def fetch_power_streams_for_range(
    start_days_ago: int = FETCH_START_DAYS_AGO,
    end_days_ago: int = FETCH_END_DAYS_AGO,
    use_cache: bool = True,
) -> None:
    """Download power streams for all matching activities in the date window."""
    df = load_and_filter_activities(start_days_ago, end_days_ago)
    if df.empty:
        print("No activities matching filters (sport + date window).")
        return

    POWER_DIR.mkdir(exist_ok=True)
    fetched = 0
    cached = 0
    skipped = 0

    for _, row in df.iterrows():
        act_id = int(row["id"])
        sport = str(row.iloc[1])
        act_date = row["activity_date"]
        cached_path = POWER_DIR / f"power_stream_{act_id}.csv"
        was_cached = cached_path.exists() if use_cache else False

        print(f"{act_id} ({sport}, {act_date}): fetching power stream...")
        try:
            _, _, path = get_power_stream_cached(act_id, use_cache=use_cache)
            if use_cache and was_cached:
                cached += 1
                print(f"  -> cached at {path.name}")
            else:
                fetched += 1
                print(f"  -> saved {path.name}")
        except KeyError as e:
            print(f"  -> skipped (no power stream: {e})")
            skipped += 1
            continue
        except Exception as e:
            print(f"  -> skipped (stream error: {e})")
            skipped += 1
            continue

    total = len(df)
    print(
        f"Done. Downloaded {fetched}, cached {cached}, skipped {skipped}, total considered {total}."
    )


if __name__ == "__main__":
    fetch_power_streams_for_range(
        start_days_ago=FETCH_START_DAYS_AGO,
        end_days_ago=FETCH_END_DAYS_AGO,
        use_cache=True,
    )
