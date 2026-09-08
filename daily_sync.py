from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

from build_table import main as build_table_main
from fetch_gpx_range import fetch_gpx_for_range
from fetch_hr_stream import fetch_hr_streams_for_range
from fetch_power_stream import fetch_power_streams_for_range
from garmin_fetch import fetch_garmin_activities
from garmin_normalize import GARMIN_CUTOVER_DATE

# --------- CONFIG ---------
STATE_PATH = Path("sync_state.json")     # tracks the last successful sync date
BOOTSTRAP_END_DAYS_AGO = (date.today() - GARMIN_CUTOVER_DATE).days
START_DAYS_AGO = 0                       # include activities through today
ACTIVITY_BACKFILL_DAYS = 30              # always re-fetch this rolling window
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


def refresh_activities_table(end_days_ago: int) -> None:
    """Fetch Garmin activities and rebuild combined activities_clean.csv."""
    lookback_days = max(int(end_days_ago), int(ACTIVITY_BACKFILL_DAYS))
    lookback_start_date = date.today() - timedelta(days=lookback_days)
    start_date = max(GARMIN_CUTOVER_DATE, lookback_start_date)

    fetch_garmin_activities(
        start_date=start_date,
        end_date=date.today(),
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
    except Exception as e:
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
