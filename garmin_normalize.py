from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd


GARMIN_CUTOVER_DATE = date(2026, 6, 30)
STRAVA_RAW_PATH = Path("strava_activities.json")
GARMIN_RAW_PATH = Path("garmin_activities.json")
OUT_CSV = Path("activities_clean.csv")


GARMIN_SPORT_MAP = {
    "running": "Run",
    "street_running": "Run",
    "track_running": "Run",
    "trail_running": "TrailRun",
    "treadmill_running": "Run",
    "virtual_running": "VirtualRun",
    "cycling": "Ride",
    "road_biking": "Ride",
    "gravel_cycling": "GravelRide",
    "mountain_biking": "MountainBikeRide",
    "indoor_cycling": "VirtualRide",
    "walking": "Walk",
    "hiking": "Hike",
    "backcountry_skiing": "BackcountrySki",
    "resort_skiing": "AlpineSki",
    "cross_country_skiing": "NordicSki",
    "stand_up_paddleboarding": "StandUpPaddling",
    "kayaking": "Kayaking",
    "other": "Workout",
}


def load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _camelize_type_key(type_key: str | None) -> str:
    if not type_key:
        return "Workout"
    return "".join(part.capitalize() for part in re.split(r"[_\s-]+", type_key) if part)


def _garmin_type_key(activity: dict[str, Any]) -> str | None:
    type_payload = activity.get("activityType") or activity.get("activityTypeDTO") or {}
    if isinstance(type_payload, dict):
        return type_payload.get("typeKey")
    return None


def _garmin_sport(activity: dict[str, Any]) -> str:
    type_key = _garmin_type_key(activity)
    return GARMIN_SPORT_MAP.get(str(type_key), _camelize_type_key(type_key))


def _summary(activity: dict[str, Any]) -> dict[str, Any]:
    summary = activity.get("summaryDTO")
    if isinstance(summary, dict):
        return summary
    return activity


def _first_number(*values: Any) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _to_datetime(values: Any) -> pd.Series | pd.Timestamp:
    return pd.to_datetime(values, errors="coerce", format="mixed", utc=True)


def normalize_garmin_activities(
    activities: list[dict[str, Any]],
    cutover_date: date = GARMIN_CUTOVER_DATE,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for activity in activities:
        summary = _summary(activity)
        act_id = activity.get("activityId", activity.get("id"))
        if act_id is None:
            continue

        start_local = (
            summary.get("startTimeLocal")
            or activity.get("startTimeLocal")
            or summary.get("startTimeGMT")
            or activity.get("startTimeGMT")
        )
        if not start_local:
            continue

        activity_date = _to_datetime(start_local)
        if pd.isna(activity_date):
            continue
        if activity_date.date() < cutover_date:
            continue

        sport = _garmin_sport(activity)
        avg_watts = _first_number(
            summary.get("averagePower"),
            summary.get("avgPower"),
            activity.get("averagePower"),
            activity.get("avgPower"),
            summary.get("normalizedPower"),
            activity.get("normalizedPower"),
        )
        avg_cadence = _first_number(
            summary.get("averageBikingCadenceInRevPerMinute"),
            activity.get("averageBikingCadenceInRevPerMinute"),
            summary.get("averageRunningCadenceInStepsPerMinute"),
            activity.get("averageRunningCadenceInStepsPerMinute"),
            summary.get("averageCadence"),
            activity.get("averageCadence"),
        )

        row = {
            "id": int(act_id),
            "name": activity.get("activityName") or activity.get("name") or f"Garmin {act_id}",
            "sport_type": sport,
            "type": sport,
            "start_date_local": activity_date.isoformat(),
            "distance": _first_number(summary.get("distance"), activity.get("distance")),
            "moving_time": _first_number(
                summary.get("movingDuration"),
                activity.get("movingDuration"),
                summary.get("duration"),
                activity.get("duration"),
            ),
            "elapsed_time": _first_number(
                summary.get("elapsedDuration"),
                activity.get("elapsedDuration"),
                summary.get("duration"),
                activity.get("duration"),
            ),
            "total_elevation_gain": _first_number(
                summary.get("elevationGain"),
                activity.get("elevationGain"),
            ),
            "average_heartrate": _first_number(
                summary.get("averageHR"),
                activity.get("averageHR"),
            ),
            "max_heartrate": _first_number(summary.get("maxHR"), activity.get("maxHR")),
            "average_speed": _first_number(
                summary.get("averageSpeed"),
                activity.get("averageSpeed"),
            ),
            "average_cadence": avg_cadence,
            "average_watts": avg_watts,
            "kilojoules": _first_number(
                summary.get("totalWork"),
                activity.get("totalWork"),
                summary.get("kilojoules"),
                activity.get("kilojoules"),
            ),
            "source": "garmin",
            "garmin_type_key": _garmin_type_key(activity),
        }
        metadata = activity.get("metadataDTO")
        if isinstance(metadata, dict):
            row["manual"] = bool(metadata.get("manualActivity", False))
        elif "manualActivity" in activity:
            row["manual"] = bool(activity.get("manualActivity"))
        rows.append(row)

    return pd.DataFrame(rows)


def normalize_strava_activities(
    activities: list[dict[str, Any]],
    cutover_date: date = GARMIN_CUTOVER_DATE,
) -> pd.DataFrame:
    if not activities:
        return pd.DataFrame()

    df = pd.DataFrame(activities)
    cols = [
        "id",
        "name",
        "sport_type",
        "type",
        "start_date_local",
        "distance",
        "moving_time",
        "elapsed_time",
        "total_elevation_gain",
        "average_heartrate",
        "max_heartrate",
        "average_speed",
        "average_cadence",
        "average_watts",
        "kilojoules",
    ]
    df = df[[col for col in cols if col in df.columns]].copy()
    if df.empty or "start_date_local" not in df.columns:
        return pd.DataFrame()

    activity_dates = _to_datetime(df["start_date_local"]).dt.date
    df = df.loc[activity_dates < cutover_date].copy()
    if df.empty:
        return df

    df["source"] = "strava"
    return df


def add_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["date"] = _to_datetime(out["start_date_local"]).dt.date
    if "distance" in out.columns:
        out["distance_km"] = pd.to_numeric(out["distance"], errors="coerce") / 1000.0
    if "moving_time" in out.columns:
        out["moving_time_h"] = pd.to_numeric(out["moving_time"], errors="coerce") / 3600.0
    if "total_elevation_gain" in out.columns:
        out["elev_km"] = pd.to_numeric(out["total_elevation_gain"], errors="coerce") / 1000.0
    if "sport_type" in out.columns:
        out["sport"] = out["sport_type"]
    elif "type" in out.columns:
        out["sport"] = out["type"]
    return out


def build_combined_activities_table(
    strava_raw_path: Path = STRAVA_RAW_PATH,
    garmin_raw_path: Path = GARMIN_RAW_PATH,
    out_csv: Path = OUT_CSV,
    cutover_date: date = GARMIN_CUTOVER_DATE,
) -> pd.DataFrame:
    strava_df = normalize_strava_activities(load_json_list(strava_raw_path), cutover_date)
    garmin_df = normalize_garmin_activities(load_json_list(garmin_raw_path), cutover_date)

    frames = [df.dropna(axis=1, how="all") for df in (strava_df, garmin_df) if not df.empty]
    if frames:
        df = pd.concat(frames, ignore_index=True, sort=False)
    else:
        df = pd.DataFrame()

    df = add_derived_columns(df)
    if not df.empty:
        df = df.drop_duplicates(subset=["id"], keep="last")
        df = df.sort_values(["date", "start_date_local", "id"], ascending=False)

    df.to_csv(out_csv, index=False)
    return df
