from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from garmin_client import activity_id, get_garmin_client
from garmin_normalize import GARMIN_CUTOVER_DATE, GARMIN_RAW_PATH


def _load_existing(path: Path = GARMIN_RAW_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _parse_start_date(activity: dict[str, Any]) -> str:
    return str(
        activity.get("startTimeLocal")
        or activity.get("startTimeGMT")
        or activity.get("summaryDTO", {}).get("startTimeLocal")
        or ""
    )


def fetch_garmin_activities(
    start_date: date = GARMIN_CUTOVER_DATE,
    end_date: date | None = None,
    raw_path: Path = GARMIN_RAW_PATH,
) -> list[dict[str, Any]]:
    """Fetch Garmin activities from start_date through end_date and merge cache."""
    end_date = end_date or date.today()
    api = get_garmin_client()

    fresh = api.get_activities_by_date(
        start_date.isoformat(),
        end_date.isoformat(),
        sortorder="desc",
    )
    if not isinstance(fresh, list):
        fresh = []

    merged_by_id: dict[int, dict[str, Any]] = {}
    for activity in _load_existing(raw_path):
        act_id = activity_id(activity)
        if act_id is not None:
            merged_by_id[act_id] = activity
    for activity in fresh:
        act_id = activity_id(activity)
        if act_id is not None:
            merged_by_id[act_id] = activity

    merged = list(merged_by_id.values())
    merged.sort(key=_parse_start_date, reverse=True)
    raw_path.write_text(json.dumps(merged, indent=2, default=str))
    print(
        f"Fetched {len(fresh)} Garmin activities from {start_date.isoformat()} "
        f"to {end_date.isoformat()}; cached {len(merged)} in {raw_path}."
    )
    return merged


def fetch_garmin_activities_since(
    start_date: str = GARMIN_CUTOVER_DATE.isoformat(),
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    start = datetime.fromisoformat(start_date).date()
    end = datetime.fromisoformat(end_date).date() if end_date else date.today()
    return fetch_garmin_activities(start, end)


if __name__ == "__main__":
    fetch_garmin_activities()
