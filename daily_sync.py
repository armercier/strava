from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

import requests

from build_table import main as build_table_main
from fetch_gpx_range import fetch_gpx_for_range
from fetch_hr_stream import fetch_hr_streams_for_range
from fetch_power_stream import fetch_power_streams_for_range
from strava_fetch import (
    ACTIVITIES_PATH,
    load_tokens as load_strava_tokens,
    refresh_access_token as refresh_strava_token,
)

# --------- CONFIG ---------
STATE_PATH = Path("sync_state.json")     # tracks the last successful sync date
BOOTSTRAP_END_DAYS_AGO = 120             # initial window if no state exists
START_DAYS_AGO = 10                       # always pull most recent to oldest
# --------------------------


def load_last_fetch_date() -> Optional[date]:
    if not STATE_PATH.exists():
        return None
    data = json.loads(STATE_PATH.read_text())
    val = data.get("last_fetch_date")
    if not val:
        return None
    try:
        return datetime.fromisoformat(val).date()
    except ValueError:
        return None


def save_last_fetch_date(d: date) -> None:
    STATE_PATH.write_text(json.dumps({"last_fetch_date": d.isoformat()}, indent=2))


def compute_window(last_date: Optional[date]) -> Tuple[int, int]:
    """Return (start_days_ago, end_days_ago) for the sync window."""
    today = date.today()
    if last_date is None:
        return START_DAYS_AGO, BOOTSTRAP_END_DAYS_AGO
    delta_days = max((today - last_date).days, 0)
    return START_DAYS_AGO, delta_days


def fetch_recent_activities(access_token: str, after_ts: int, per_page: int = 100):
    """Fetch activities updated after the given epoch timestamp."""
    all_acts = []
    page = 1
    while True:
        url = "https://www.strava.com/api/v3/athlete/activities"
        params = {"page": page, "per_page": per_page, "after": after_ts}
        headers = {"Authorization": f"Bearer {access_token}"}
        resp = requests.get(url, params=params, headers=headers)
        resp.raise_for_status()
        activities = resp.json()
        if not activities:
            break
        all_acts.extend(activities)
        if len(activities) < per_page:
            break
        page += 1
    return all_acts


def refresh_activities_table(end_days_ago: int) -> None:
    """Fetch recent activities JSON and rebuild activities_clean.csv (merge)."""
    existing = []
    if ACTIVITIES_PATH.exists():
        try:
            existing = json.loads(ACTIVITIES_PATH.read_text())
        except json.JSONDecodeError:
            existing = []
    existing_by_id = {act.get("id"): act for act in existing if "id" in act}

    # Use latest known activity start time as baseline to avoid missing new ones.
    latest_start = None
    for act in existing_by_id.values():
        ts = act.get("start_date_local") or act.get("start_date")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if latest_start is None or dt > latest_start:
                latest_start = dt
        except ValueError:
            continue

    tokens = load_strava_tokens()
    access_token, _ = refresh_strava_token(tokens)
    if latest_start:
        after_dt = latest_start - timedelta(hours=12)  # small buffer
    else:
        start_date = date.today() - timedelta(days=end_days_ago)
        after_dt = datetime.combine(start_date, datetime.min.time())

    after_ts = int(after_dt.timestamp())
    fresh = fetch_recent_activities(access_token, after_ts)

    merged_by_id = existing_by_id.copy()
    for act in fresh:
        act_id = act.get("id")
        if act_id is None:
            continue
        merged_by_id[act_id] = act  # prefer latest API copy

    merged = list(merged_by_id.values())
    # Sort by start_date_local if present, else fallback to id descending
    merged.sort(
        key=lambda a: (
            a.get("start_date_local") or "",
            a.get("id", 0),
        ),
        reverse=True,
    )

    ACTIVITIES_PATH.write_text(json.dumps(merged, indent=2))
    print(
        f"Merged {len(merged)} activities (added/updated {len(fresh)}) to {ACTIVITIES_PATH}"
    )
    build_table_main()


def main() -> None:
    last_date = load_last_fetch_date()
    start_days_ago, end_days_ago = compute_window(last_date)
    today = date.today()

    if last_date:
        print(
            f"Last sync: {last_date.isoformat()}. "
            f"Fetching activities from {end_days_ago} to {start_days_ago} days ago."
        )
    else:
        print(
            f"No previous sync found. Bootstrapping window "
            f"{end_days_ago} to {start_days_ago} days ago."
        )

    print("Step 0/3: Refresh activities table (recent only)")
    try:
        refresh_activities_table(end_days_ago=end_days_ago)
    except (requests.HTTPError, requests.RequestException) as e:
        raise SystemExit(f"Failed to refresh activities table: {e}")

    print("Step 1/3: GPX files")
    fetch_gpx_for_range(start_days_ago=start_days_ago, end_days_ago=end_days_ago)

    print("Step 2/3: Heart rate streams")
    fetch_hr_streams_for_range(
        start_days_ago=start_days_ago,
        end_days_ago=end_days_ago,
        use_cache=True,
    )

    print("Step 3/3: Power streams")
    fetch_power_streams_for_range(
        start_days_ago=start_days_ago,
        end_days_ago=end_days_ago,
        use_cache=True,
    )

    save_last_fetch_date(today)
    print(f"Sync complete. Updated last_fetch_date to {today.isoformat()}.")


if __name__ == "__main__":
    main()
