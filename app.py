from __future__ import annotations

from html import escape as html_escape
import json
import pickle
import subprocess
import re
import inspect
import sys
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import List, Tuple

import altair as alt
import folium
import gpxpy
import pandas as pd
import streamlit as st
from streamlit.components.v1 import html

from daily_sync import main as sync_latest
from fetch_gpx_range import GPX_DIR
from fetch_hr_stream import get_hr_stream_cached
from fetch_power_stream import get_power_stream_cached
from garmin_tracks import export_gpx_for_activity

CSV_PATH = Path("activities_clean.csv")
HR_ZONES_PATH = Path("hr_zones.json")
OVERRIDES_PATH = Path("activity_overrides.json")
MANUAL_ACTIVITIES_PATH = Path("manual_activities.json")
WEEKLY_NOTES_PATH = Path("weekly_notes.json")
HR_STREAMS_DIR = Path("hr_streams")
YEAR_OVERVIEW_CACHE_DIR = Path("year_overview_cache")
YEAR_SNAPSHOT_PREVIOUS_COUNT = 2
FULL_MAP_HTML_PATH = Path("gpx_sport_map.html")
FULL_MAP_BUILD_SCRIPT = Path("build_gpx_map.py")
TRAINING_PLAN_CSV_PATH = Path("hm_to_kima_workout_plan.csv")
HR_ZONE_COLORS = [
    "#9ca3af",  # Recovery
    "#3b82f6",  # Zone 1
    "#22c55e",  # Zone 2
    "#fd7e14",  # Zone 3
    "#b91c1c",  # Zone 4
    "#8b5cf6",  # Zone 5
]
WEEKLY_PANEL_HEIGHT_PX = 190
WEEKLY_ACTIVITY_BUTTON_SLOTS = 2
WEEKLY_ACTIVITY_BUTTON_SLOT_PX = 50

_FULL_MAP_BUILD_LOCK = threading.Lock()
_FULL_MAP_BUILD_STATE = {
    "running": False,
    "last_started": "",
    "last_finished": "",
    "last_error": "",
}


# ---------- DATA HELPERS ----------


def load_activity_overrides() -> dict:
    if not OVERRIDES_PATH.exists():
        return {}
    try:
        data = json.loads(OVERRIDES_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_weekly_notes() -> dict:
    if not WEEKLY_NOTES_PATH.exists():
        return {}
    try:
        data = json.loads(WEEKLY_NOTES_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_weekly_note(week_start_date: date, note: str | None) -> None:
    notes = load_weekly_notes()
    key = week_start_date.isoformat()
    text = (note or "").strip()
    if text:
        notes[key] = text
    else:
        notes.pop(key, None)
    WEEKLY_NOTES_PATH.write_text(json.dumps(notes, indent=2, sort_keys=True))


NUMERIC_OVERRIDE_FIELDS = {
    "distance",
    "moving_time",
    "average_speed",
    "total_elevation_gain",
    "average_watts",
    "average_heartrate",
}
TEXT_OVERRIDE_FIELDS = {
    "sport",
    "sport_type",
    "type",
}


DEFAULT_MANUAL_SPORTS = [
    "Run",
    "TrailRun",
    "Ride",
    "MountainBikeRide",
    "Walk",
    "Hike",
    "Workout",
    "StrengthTraining",
    "Swim",
    "AlpineSki",
    "BackcountrySki",
    "NordicSki",
]


def load_manual_activities() -> list[dict]:
    if not MANUAL_ACTIVITIES_PATH.exists():
        return []
    try:
        data = json.loads(MANUAL_ACTIVITIES_PATH.read_text())
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def save_manual_activities(activities: list[dict]) -> None:
    MANUAL_ACTIVITIES_PATH.write_text(
        json.dumps(activities, indent=2, sort_keys=True)
    )


def _next_manual_activity_id(existing: list[dict]) -> int:
    ids = []
    for activity in existing:
        try:
            ids.append(int(activity.get("id")))
        except (TypeError, ValueError):
            continue
    manual_ids = [activity_id for activity_id in ids if activity_id < 0]
    return (min(manual_ids) - 1) if manual_ids else -1


def create_manual_activity(
    *,
    activity_date: date,
    name: str,
    sport: str,
    distance_km: float | None,
    moving_time_h: float | None,
    elapsed_time_h: float | None,
    elevation_m: float | None,
    average_heartrate: float | None,
    max_heartrate: float | None,
    average_cadence: float | None,
    average_watts: float | None,
    kilojoules: float | None,
    notes: str,
) -> int:
    activities = load_manual_activities()
    activity_id = _next_manual_activity_id(activities)
    distance_m = float(distance_km or 0.0) * 1000.0
    moving_s = float(moving_time_h or 0.0) * 3600.0
    elapsed_s = float(elapsed_time_h if elapsed_time_h is not None else moving_time_h or 0.0) * 3600.0
    sport = str(sport or "Workout").strip() or "Workout"
    title = str(name or "").strip() or f"Manual {sport}"
    activity = {
        "id": activity_id,
        "name": title,
        "sport_type": sport,
        "type": sport,
        "sport": sport,
        "start_date_local": activity_date.isoformat(),
        "date": activity_date.isoformat(),
        "distance": distance_m,
        "moving_time": moving_s,
        "elapsed_time": elapsed_s,
        "total_elevation_gain": float(elevation_m or 0.0),
        "average_heartrate": average_heartrate,
        "max_heartrate": max_heartrate,
        "average_speed": (distance_m / moving_s) if moving_s > 0 else None,
        "average_cadence": average_cadence,
        "average_watts": average_watts,
        "kilojoules": kilojoules,
        "source": "manual",
        "manual": True,
    }
    activities.append(activity)
    save_manual_activities(activities)

    note_text = (notes or "").strip()
    if note_text:
        save_activity_overrides(activity_id, {"notes": note_text})
    return activity_id


def delete_manual_activity(activity_id: int) -> bool:
    activities = load_manual_activities()
    kept = []
    for activity in activities:
        try:
            current_id = int(activity.get("id", 0))
        except (TypeError, ValueError):
            kept.append(activity)
            continue
        if current_id != int(activity_id):
            kept.append(activity)
    if len(kept) == len(activities):
        return False
    save_manual_activities(kept)
    save_activity_overrides(
        activity_id,
        {
            "distance": None,
            "moving_time": None,
            "average_speed": None,
            "total_elevation_gain": None,
            "average_watts": None,
            "average_heartrate": None,
            "notes": None,
            "sport": None,
            "sport_type": None,
            "type": None,
        },
    )
    return True


def manual_activities_frame() -> pd.DataFrame:
    activities = load_manual_activities()
    if not activities:
        return pd.DataFrame()
    df = pd.DataFrame(activities)
    if "start_date_local" not in df.columns and "date" in df.columns:
        df["start_date_local"] = df["date"]
    if "date" not in df.columns:
        df["date"] = pd.to_datetime(df["start_date_local"], errors="coerce").dt.date
    if "distance_km" not in df.columns and "distance" in df.columns:
        df["distance_km"] = pd.to_numeric(df["distance"], errors="coerce") / 1000.0
    if "moving_time_h" not in df.columns and "moving_time" in df.columns:
        df["moving_time_h"] = pd.to_numeric(df["moving_time"], errors="coerce") / 3600.0
    if "elev_km" not in df.columns and "total_elevation_gain" in df.columns:
        df["elev_km"] = pd.to_numeric(df["total_elevation_gain"], errors="coerce") / 1000.0
    if "sport" not in df.columns:
        if "sport_type" in df.columns:
            df["sport"] = df["sport_type"]
        elif "type" in df.columns:
            df["sport"] = df["type"]
        else:
            df["sport"] = "Workout"
    return df


def apply_activity_overrides(df: pd.DataFrame) -> pd.DataFrame:
    overrides = load_activity_overrides()
    if not overrides:
        return df

    for raw_id, fields in overrides.items():
        if not isinstance(fields, dict):
            continue
        try:
            activity_id = int(raw_id)
        except (TypeError, ValueError):
            continue

        mask = df["id"] == activity_id
        if not mask.any():
            continue

        for col, raw_val in fields.items():
            if col in TEXT_OVERRIDE_FIELDS:
                text_val = str(raw_val).strip()
                if not text_val:
                    continue
                for target_col in (
                    ["sport", "sport_type", "type"] if col == "sport" else [col]
                ):
                    if target_col not in df.columns:
                        df[target_col] = pd.NA
                    df.loc[mask, target_col] = text_val
                continue

            if col not in NUMERIC_OVERRIDE_FIELDS:
                continue
            try:
                val = float(raw_val)
            except (TypeError, ValueError):
                continue
            if col not in df.columns:
                df[col] = pd.NA
            df.loc[mask, col] = val

        # Keep the display/aggregate columns in sync with Strava's base-unit
        # fields. Weekly and yearly totals prefer these convenience columns
        # when they are present in the CSV.
        if "distance" in fields:
            distance_m = pd.to_numeric(df.loc[mask, "distance"], errors="coerce")
            if "distance_km" not in df.columns:
                df["distance_km"] = pd.NA
            df.loc[mask, "distance_km"] = distance_m / 1000.0
        if "moving_time" in fields:
            moving_s = pd.to_numeric(df.loc[mask, "moving_time"], errors="coerce")
            if "moving_time_h" not in df.columns:
                df["moving_time_h"] = pd.NA
            df.loc[mask, "moving_time_h"] = moving_s / 3600.0

        # An explicit speed wins. Otherwise a distance/time edit should also
        # refresh the activity's displayed speed or running pace.
        if (
            "average_speed" not in fields
            and ("distance" in fields or "moving_time" in fields)
        ):
            if "average_speed" not in df.columns:
                df["average_speed"] = pd.NA
            distance_m = pd.to_numeric(df.loc[mask, "distance"], errors="coerce")
            moving_s = pd.to_numeric(df.loc[mask, "moving_time"], errors="coerce")
            derived_speed = distance_m.div(moving_s.where(moving_s > 0))
            df.loc[mask, "average_speed"] = derived_speed

    return df


def save_activity_overrides(
    activity_id: int, updates: dict[str, float | str | None]
) -> None:
    overrides = load_activity_overrides()
    key = str(int(activity_id))
    current = overrides.get(key, {})
    if not isinstance(current, dict):
        current = {}

    for field, value in updates.items():
        if value is None:
            current.pop(field, None)
        elif field in NUMERIC_OVERRIDE_FIELDS:
            current[field] = float(value)
        elif field in TEXT_OVERRIDE_FIELDS:
            text_value = str(value).strip()
            if text_value:
                current[field] = text_value
            else:
                current.pop(field, None)
        else:
            text_value = str(value).strip()
            if text_value:
                current[field] = text_value
            else:
                current.pop(field, None)

    if current:
        overrides[key] = current
    else:
        overrides.pop(key, None)

    OVERRIDES_PATH.write_text(json.dumps(overrides, indent=2, sort_keys=True))


def parse_optional_float(value: str) -> float | None:
    v = value.strip()
    if not v:
        return None
    return float(v.replace(",", "."))


def _path_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _dir_latest_mtime_ns(path: Path, pattern: str = "*") -> int:
    latest = _path_mtime_ns(path)
    if latest == 0:
        return 0
    try:
        for p in path.glob(pattern):
            latest = max(latest, _path_mtime_ns(p))
    except OSError:
        pass
    return latest


def _year_slice(df: pd.DataFrame, year: int, selected_sports: tuple[str, ...]) -> pd.DataFrame:
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    return df[
        (df["activity_date"] >= year_start)
        & (df["activity_date"] <= year_end)
        & (df["sport"].isin(selected_sports))
    ].copy()


def _year_df_fingerprint(year_df: pd.DataFrame) -> int:
    if year_df.empty:
        return 0
    fp_cols = [
        "id",
        "activity_date",
        "sport",
        "moving_time",
        "moving_time_h",
        "distance",
        "distance_km",
        "total_elevation_gain",
        "average_heartrate",
    ]
    cols = [c for c in fp_cols if c in year_df.columns]
    fp = year_df[cols].copy()
    if "activity_date" in fp.columns:
        fp["activity_date"] = fp["activity_date"].astype(str)
    fp = fp.fillna("")
    return int(pd.util.hash_pandas_object(fp, index=False).sum())


def _hr_streams_signature_for_activities(activity_ids: list[int]) -> tuple[int, int]:
    latest_mtime_ns = 0
    stream_count = 0
    for activity_id in sorted(set(int(a) for a in activity_ids)):
        p = HR_STREAMS_DIR / f"hr_stream_{activity_id}.csv"
        if p.exists():
            stream_count += 1
            latest_mtime_ns = max(latest_mtime_ns, _path_mtime_ns(p))
    return stream_count, latest_mtime_ns


def _year_bundle_revision(
    year_df: pd.DataFrame,
    selected_sports: tuple[str, ...],
    overrides_mtime_ns: int,
    zones_mtime_ns: int,
) -> dict:
    activity_ids = (
        year_df["id"].astype(int).tolist() if "id" in year_df.columns else []
    )
    stream_count, streams_latest_mtime_ns = _hr_streams_signature_for_activities(
        activity_ids
    )
    return {
        "schema_version": 1,
        "sports": list(selected_sports),
        "rows": int(len(year_df)),
        "df_fingerprint": _year_df_fingerprint(year_df),
        "overrides_mtime_ns": int(overrides_mtime_ns),
        "zones_mtime_ns": int(zones_mtime_ns),
        "hr_stream_count": int(stream_count),
        "hr_streams_latest_mtime_ns": int(streams_latest_mtime_ns),
    }


def _year_bundle_cache_path(year: int) -> Path:
    YEAR_OVERVIEW_CACHE_DIR.mkdir(exist_ok=True)
    return YEAR_OVERVIEW_CACHE_DIR / f"year_overview_{year}.pkl"


def load_saved_year_overview_bundle(year: int, revision: dict) -> dict | None:
    path = _year_bundle_cache_path(year)
    if not path.exists():
        return None
    try:
        payload = pickle.loads(path.read_bytes())
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("revision") != revision:
        return None
    bundle = payload.get("bundle")
    return bundle if isinstance(bundle, dict) else None


def load_saved_year_overview_bundle_any_revision(year: int) -> dict | None:
    path = _year_bundle_cache_path(year)
    if not path.exists():
        return None
    try:
        payload = pickle.loads(path.read_bytes())
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    bundle = payload.get("bundle")
    return bundle if isinstance(bundle, dict) else None


def save_year_overview_bundle(year: int, revision: dict, bundle: dict) -> None:
    path = _year_bundle_cache_path(year)
    payload = {"revision": revision, "bundle": bundle}
    path.write_bytes(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))


def _full_map_source_mtime_ns() -> int:
    return max(
        _path_mtime_ns(CSV_PATH),
        _path_mtime_ns(GPX_DIR),
        _path_mtime_ns(FULL_MAP_BUILD_SCRIPT),
    )


def _full_map_needs_rebuild() -> bool:
    if not FULL_MAP_HTML_PATH.exists():
        return True
    return _path_mtime_ns(FULL_MAP_HTML_PATH) < _full_map_source_mtime_ns()


def _run_full_map_build() -> None:
    try:
        proc = subprocess.run(
            [sys.executable, str(FULL_MAP_BUILD_SCRIPT)],
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            check=True,
        )
        err = ""
        if proc.stderr and proc.stderr.strip():
            err = proc.stderr.strip().splitlines()[-1][:300]
        with _FULL_MAP_BUILD_LOCK:
            _FULL_MAP_BUILD_STATE["last_error"] = err
    except Exception as exc:
        with _FULL_MAP_BUILD_LOCK:
            _FULL_MAP_BUILD_STATE["last_error"] = str(exc)[:500]
    finally:
        with _FULL_MAP_BUILD_LOCK:
            _FULL_MAP_BUILD_STATE["running"] = False
            _FULL_MAP_BUILD_STATE["last_finished"] = date.today().isoformat()


def start_full_map_build(force: bool = False) -> bool:
    with _FULL_MAP_BUILD_LOCK:
        if _FULL_MAP_BUILD_STATE["running"]:
            return False
        if not force and not _full_map_needs_rebuild():
            return False
        _FULL_MAP_BUILD_STATE["running"] = True
        _FULL_MAP_BUILD_STATE["last_started"] = date.today().isoformat()
        _FULL_MAP_BUILD_STATE["last_error"] = ""
        worker = threading.Thread(target=_run_full_map_build, daemon=True)
        worker.start()
        return True


def get_full_map_status() -> dict:
    with _FULL_MAP_BUILD_LOCK:
        state = dict(_FULL_MAP_BUILD_STATE)
    state["exists"] = FULL_MAP_HTML_PATH.exists()
    state["needs_rebuild"] = _full_map_needs_rebuild()
    return state

@st.cache_data
def load_activities() -> pd.DataFrame:
    df = pd.read_csv(CSV_PATH)
    manual_df = manual_activities_frame()
    if not manual_df.empty:
        manual_df = manual_df.dropna(axis=1, how="all")
        df = pd.concat([df, manual_df], ignore_index=True, sort=False)

    if "date" in df.columns:
        df["activity_date"] = pd.to_datetime(df["date"]).dt.date
    elif "start_date_local" in df.columns:
        df["activity_date"] = pd.to_datetime(df["start_date_local"]).dt.date
    else:
        raise RuntimeError("No 'date' or 'start_date_local' column in CSV.")

    if "sport_type" in df.columns:
        df["sport"] = df["sport_type"]
    elif "sport" in df.columns:
        df["sport"] = df["sport"]
    elif "type" in df.columns:
        df["sport"] = df["type"]
    else:
        raise RuntimeError("No sport column found (sport_type/sport/type).")

    df["id"] = df["id"].astype(int)
    df = apply_activity_overrides(df)
    if "date" in df.columns:
        df["activity_date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    elif "start_date_local" in df.columns:
        df["activity_date"] = pd.to_datetime(
            df["start_date_local"], errors="coerce", format="mixed"
        ).dt.date
    return df


@st.cache_data
def load_training_plan() -> pd.DataFrame:
    if not TRAINING_PLAN_CSV_PATH.exists():
        raise FileNotFoundError(f"Training plan file not found: {TRAINING_PLAN_CSV_PATH}")

    df = pd.read_csv(TRAINING_PLAN_CSV_PATH)

    # Accept both plan schemas (v1 and v2) by normalizing core fields used by UI.
    if "iso_week" not in df.columns and "week" in df.columns:
        df["iso_week"] = df["week"]
    if "session_order" not in df.columns and "session_id" in df.columns:
        df["session_order"] = df["session_id"]
    if "session_name" not in df.columns and "type" in df.columns:
        df["session_name"] = df["type"]
    if "session_description" not in df.columns and "description" in df.columns:
        df["session_description"] = df["description"]
    if "weekly_total_hours" not in df.columns and "total_hours" in df.columns:
        df["weekly_total_hours"] = df["total_hours"]
    if "weekly_run_sessions" not in df.columns and "runs" in df.columns:
        df["weekly_run_sessions"] = df["runs"]
    if "weekly_total_sessions" not in df.columns and "row_type" in df.columns:
        session_counts = (
            df[df["row_type"].astype(str) == "session"]
            .assign(iso_week_num=pd.to_numeric(df["iso_week"], errors="coerce"))
            .groupby("iso_week_num")
            .size()
            .to_dict()
        )
        iso_week_num = pd.to_numeric(df["iso_week"], errors="coerce")
        df["weekly_total_sessions"] = [
            int(session_counts.get(w, 0)) if not pd.isna(w) else pd.NA
            for w in iso_week_num
        ]
    if "sport" not in df.columns:
        if "type" in df.columns:
            df["sport"] = df["type"]
        else:
            df["sport"] = ""

    for col in ("week_start", "week_end"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date

    if "iso_week" in df.columns:
        df["iso_week"] = pd.to_numeric(df["iso_week"], errors="coerce").astype("Int64")
    if "session_order" in df.columns:
        df["session_order"] = pd.to_numeric(df["session_order"], errors="coerce")
    if "duration_min" in df.columns:
        df["duration_min"] = pd.to_numeric(df["duration_min"], errors="coerce")
    else:
        # v2 fallback: derive per-session duration from weekly total hours.
        if "row_type" in df.columns and "weekly_total_hours" in df.columns:
            week_hours = (
                df[df["row_type"].astype(str) == "week_summary"]
                .copy()
                .assign(iso_week_num=lambda t: pd.to_numeric(t["iso_week"], errors="coerce"))
            )
            week_hours["weekly_total_hours_num"] = pd.to_numeric(
                week_hours["weekly_total_hours"], errors="coerce"
            )
            week_hours_map = (
                week_hours.dropna(subset=["iso_week_num"])
                .set_index("iso_week_num")["weekly_total_hours_num"]
                .to_dict()
            )

            session_mask = df["row_type"].astype(str) == "session"
            week_session_counts = (
                df.loc[session_mask]
                .assign(iso_week_num=lambda t: pd.to_numeric(t["iso_week"], errors="coerce"))
                .groupby("iso_week_num")
                .size()
                .to_dict()
            )

            duration_vals = []
            iso_week_num = pd.to_numeric(df["iso_week"], errors="coerce")
            for idx, row in df.iterrows():
                if str(row.get("row_type", "")) != "session":
                    duration_vals.append(pd.NA)
                    continue
                w = iso_week_num.loc[idx]
                weekly_h = week_hours_map.get(w)
                session_count = week_session_counts.get(w, 0)
                if pd.isna(weekly_h) or session_count <= 0:
                    duration_vals.append(pd.NA)
                else:
                    duration_vals.append((float(weekly_h) * 60.0) / float(session_count))
            df["duration_min"] = pd.to_numeric(pd.Series(duration_vals), errors="coerce")

    return df


def save_training_plan(df: pd.DataFrame) -> None:
    df.to_csv(TRAINING_PLAN_CSV_PATH, index=False)


def get_year_overview_bundle(
    year: int,
    selected_sports: tuple[str, ...],
) -> dict:
    df = load_activities()
    year_df = _year_slice(df, year, selected_sports)

    sport_order = [
        "Biking",
        "Nordic skiing",
        "Trail running",
        "Running",
        "Swimming",
        "WeightTraining",
        "BackcountrySki",
        "Other",
    ]

    if year_df.empty:
        return {
            "empty": True,
            "year_df": year_df,
            "sport_order": sport_order,
        }

    if "moving_time_h" in year_df.columns:
        time_h = year_df["moving_time_h"]
    else:
        time_h = year_df["moving_time"] / 3600.0

    if "distance_km" in year_df.columns:
        distance_km = year_df["distance_km"].fillna(0.0)
    else:
        distance_km = year_df["distance"].fillna(0.0) / 1000.0

    total_time_h = float(time_h.sum())
    total_distance_km = float(distance_km.sum())
    total_elev_m = float(year_df["total_elevation_gain"].fillna(0).sum())

    zones = load_hr_zones(HR_ZONES_PATH)
    zone_payload = {
        "zones": zones,
        "zone_totals": None,
        "missing_hr": 0,
        "avg_only_count": 0,
        "has_zone_data": False,
    }
    if zones:
        zone_totals, missing_hr, avg_only_count = compute_zone_totals_for_activities(
            year_df["id"].astype(int).tolist(), zones, activities_df=year_df
        )
        zone_payload = {
            "zones": zones,
            "zone_totals": zone_totals,
            "missing_hr": missing_hr,
            "avg_only_count": avg_only_count,
            "has_zone_data": sum(zone_totals.values()) > 0,
        }

    totals_by_sport = {label: 0.0 for label in sport_order}
    totals_km_by_sport = {label: 0.0 for label in sport_order}
    for ((_, row), hours, km) in zip(year_df.iterrows(), time_h, distance_km):
        bucket = _resolve_sport_bucket(row.get("sport", ""), sport_order)
        totals_by_sport[bucket] += float(hours)
        totals_km_by_sport[bucket] += float(km)

    weekly_df = year_df.copy()
    weekly_df["week"] = pd.to_datetime(weekly_df["activity_date"]).dt.isocalendar().week
    weekly_df["time_h"] = time_h.values
    if "distance_km" in weekly_df.columns:
        weekly_df["distance_km"] = weekly_df["distance_km"].fillna(0.0)
    else:
        weekly_df["distance_km"] = weekly_df["distance"].fillna(0.0) / 1000.0
    weekly_df["elev_m"] = weekly_df["total_elevation_gain"].fillna(0.0)
    weekly_df["sport_bucket"] = weekly_df["sport"].apply(
        lambda s: _resolve_sport_bucket(s, sport_order)
    )
    all_weeks = pd.DataFrame({"week": list(range(1, 54))})

    all_sports_week = weekly_df.groupby("week", as_index=False)["time_h"].sum()
    all_sports_week = all_weeks.merge(all_sports_week, on="week", how="left")
    all_sports_week["time_h"] = all_sports_week["time_h"].fillna(0.0)

    elev_week = weekly_df.groupby("week", as_index=False)["elev_m"].sum()
    elev_week = all_weeks.merge(elev_week, on="week", how="left")
    elev_week["elev_m"] = elev_week["elev_m"].fillna(0.0)

    running_week = (
        weekly_df[weekly_df["sport_bucket"].isin(["Running", "Trail running"])]
        .groupby("week", as_index=False)[["time_h", "distance_km"]]
        .sum()
    )
    running_week = all_weeks.merge(running_week, on="week", how="left")
    running_week["time_h"] = running_week["time_h"].fillna(0.0)
    running_week["distance_km"] = running_week["distance_km"].fillna(0.0)

    sport_weeks = {}
    for sport_label in sport_order:
        sport_week = (
            weekly_df[weekly_df["sport_bucket"] == sport_label]
            .groupby("week", as_index=False)[["time_h", "distance_km"]]
            .sum()
        )
        sport_week = all_weeks.merge(sport_week, on="week", how="left")
        sport_week["time_h"] = sport_week["time_h"].fillna(0.0)
        sport_week["distance_km"] = sport_week["distance_km"].fillna(0.0)
        sport_weeks[sport_label] = sport_week

    return {
        "empty": False,
        "year_df": year_df,
        "total_time_h": total_time_h,
        "total_distance_km": total_distance_km,
        "total_elev_m": total_elev_m,
        "sport_order": sport_order,
        "totals_by_sport": totals_by_sport,
        "totals_km_by_sport": totals_km_by_sport,
        "all_sports_week": all_sports_week,
        "elev_week": elev_week,
        "running_week": running_week,
        "sport_weeks": sport_weeks,
        **zone_payload,
    }


def ensure_gpx(activity_id: int) -> Path:
    """Return path to GPX for this activity; export from Garmin if missing."""
    GPX_DIR.mkdir(exist_ok=True)
    gpx_path = GPX_DIR / f"activity_{activity_id}.gpx"
    if not gpx_path.exists():
        gpx_path = export_gpx_for_activity(activity_id)
    return gpx_path


def read_gpx_points(path: Path) -> List[Tuple[float, float]]:
    with path.open("r") as f:
        gpx = gpxpy.parse(f)

    pts: List[Tuple[float, float]] = []
    for track in gpx.tracks:
        for segment in track.segments:
            for p in segment.points:
                pts.append((p.latitude, p.longitude))

    if not pts:
        raise RuntimeError(f"No points in GPX file: {path}")
    return pts


def read_gpx_elevation_trace(path: Path):
    """Return (time_s, elevation_m) if GPX has time/elevation, else (None, None)."""
    with path.open("r") as f:
        gpx = gpxpy.parse(f)

    times = []
    elevs = []
    start_time = None

    for track in gpx.tracks:
        for segment in track.segments:
            for p in segment.points:
                if p.elevation is None or p.time is None:
                    continue
                if start_time is None:
                    start_time = p.time
                times.append((p.time - start_time).total_seconds())
                elevs.append(p.elevation)

    if not times or not elevs:
        return None, None
    return times, elevs


def read_gpx_track_with_time(path: Path):
    """Return (times_s, lats, lons) from GPX track points that have timestamps."""
    with path.open("r") as f:
        gpx = gpxpy.parse(f)

    times = []
    lats = []
    lons = []
    start_time = None

    for track in gpx.tracks:
        for segment in track.segments:
            for p in segment.points:
                if p.time is None:
                    continue
                if start_time is None:
                    start_time = p.time
                times.append((p.time - start_time).total_seconds())
                lats.append(p.latitude)
                lons.append(p.longitude)

    if not times or not lats or not lons:
        return None, None, None
    return times, lats, lons


def _normalize_thresholds(raw: dict) -> dict:
    normalized = {}
    for key, val in raw.items():
        if val is None:
            continue
        norm_key = str(key).strip().lower()
        normalized[norm_key] = float(val)
    return normalized


def _eval_zone_expr(expr: object, thresholds: dict) -> float | None:
    if expr is None:
        return None
    if isinstance(expr, (int, float)):
        return float(expr)
    s = str(expr).strip()
    if not s:
        return None

    lower = s.lower()
    if lower in thresholds:
        return thresholds[lower]

    m = re.match(r"^([a-z_]+)\s*-\s*([0-9]+(?:\.[0-9]+)?)%$", lower)
    if m:
        base_key = m.group(1)
        pct = float(m.group(2)) / 100.0
        base_val = thresholds.get(base_key)
        if base_val is not None:
            return base_val * (1.0 - pct)
    return None


def load_hr_zones(path: Path):
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    raw_thresholds = data.get("tresholds") or data.get("thresholds") or {}
    if isinstance(raw_thresholds, list):
        raw_thresholds = raw_thresholds[0] if raw_thresholds else {}
    thresholds = _normalize_thresholds(raw_thresholds)
    zones = []
    for zone in data.get("zones", []):
        min_hr = _eval_zone_expr(zone.get("min_hr"), thresholds)
        max_hr = _eval_zone_expr(zone.get("max_hr"), thresholds)
        zones.append(
            {
                "name": zone.get("name", "Zone"),
                "min_hr": min_hr,
                "max_hr": max_hr,
            }
        )
    return zones


def format_zone_label(name: str, min_hr: float | None, max_hr: float | None) -> str:
    if min_hr is None and max_hr is None:
        return name
    if min_hr is not None and max_hr is not None and max_hr > min_hr:
        return f"{name} ({min_hr:.0f}-{max_hr:.0f})"
    if min_hr is not None:
        return f"{name} (≥{min_hr:.0f})"
    return f"{name} (≤{max_hr:.0f})"


def compute_avg_hr_zone_seconds(avg_hr, moving_time_s, zones):
    totals = {z["name"]: 0.0 for z in zones}
    if avg_hr is None or moving_time_s is None:
        return totals
    try:
        hr = float(avg_hr)
        duration = float(moving_time_s)
    except (TypeError, ValueError):
        return totals
    if duration <= 0:
        return totals

    matched = False
    for idx, zone in enumerate(zones):
        zmin = zone.get("min_hr")
        zmax = zone.get("max_hr")
        if zmin is None:
            continue
        if idx == len(zones) - 1:
            in_zone = hr >= zmin
        elif zmax is None:
            in_zone = hr >= zmin
        else:
            in_zone = zmin <= hr < zmax
        if in_zone:
            totals[zone["name"]] += duration
            matched = True
            break

    if not matched and zones:
        totals[zones[0]["name"]] += duration
    return totals


def _get_moving_time_seconds_from_row(row: pd.Series | None) -> float | None:
    if row is None:
        return None
    moving_s = row.get("moving_time", None)
    if moving_s is not None and pd.notna(moving_s):
        try:
            v = float(moving_s)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    moving_h = row.get("moving_time_h", None)
    if moving_h is not None and pd.notna(moving_h):
        try:
            v = float(moving_h) * 3600.0
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return None


def compute_zone_totals_for_activities(activity_ids, zones, activities_df: pd.DataFrame | None = None):
    totals = {z["name"]: 0.0 for z in zones}
    missing = 0
    avg_only = 0
    overrides = load_activity_overrides()
    rows_by_id = {}
    if activities_df is not None and not activities_df.empty and "id" in activities_df.columns:
        for _, row in activities_df.iterrows():
            try:
                rows_by_id[int(row["id"])] = row
            except (TypeError, ValueError):
                continue

    for act_id in activity_ids:
        row = rows_by_id.get(int(act_id))

        manual_avg_hr = None
        per_activity_override = overrides.get(str(int(act_id)), {})
        if isinstance(per_activity_override, dict):
            raw_manual_hr = per_activity_override.get("average_heartrate")
            if raw_manual_hr is not None:
                try:
                    manual_avg_hr = float(raw_manual_hr)
                except (TypeError, ValueError):
                    manual_avg_hr = None

        if manual_avg_hr is not None:
            moving_s = _get_moving_time_seconds_from_row(row)
            per_act = compute_avg_hr_zone_seconds(manual_avg_hr, moving_s, zones)
            if sum(per_act.values()) > 0:
                for name, secs in per_act.items():
                    totals[name] += secs
                avg_only += 1
                continue

        try:
            t_hr, hr, _ = get_hr_stream_cached(act_id, use_cache=True)
        except Exception:
            t_hr, hr = None, None

        per_act = compute_hr_zone_seconds(t_hr, hr, zones)
        if sum(per_act.values()) > 0:
            for name, secs in per_act.items():
                totals[name] += secs
            continue

        avg_hr = row.get("average_heartrate", None) if row is not None else None
        moving_s = _get_moving_time_seconds_from_row(row)
        per_act = compute_avg_hr_zone_seconds(avg_hr, moving_s, zones)
        if sum(per_act.values()) > 0:
            for name, secs in per_act.items():
                totals[name] += secs
            avg_only += 1
            continue

        missing += 1
    return totals, missing, avg_only


def build_zone_chart_df(zone_totals, zones):
    df = pd.DataFrame(
        {
            "zone": [z["name"] for z in zones],
            "label": [
                format_zone_label(z["name"], z["min_hr"], z["max_hr"]) for z in zones
            ],
            "minutes": [zone_totals.get(z["name"], 0.0) / 60.0 for z in zones],
        }
    )
    df["display"] = df["minutes"].apply(
        lambda m: f"{m / 60.0:.2f} h" if m >= 60.0 else f"{m:.0f} min"
    )
    return df


def compute_hr_zone_seconds(times, hr_values, zones):
    totals = {z["name"]: 0.0 for z in zones}
    if not times or not hr_values or len(times) < 2:
        return totals

    n = min(len(times), len(hr_values))
    for i in range(n - 1):
        t0 = times[i]
        t1 = times[i + 1]
        if t0 is None or t1 is None:
            continue
        dt = max(0.0, float(t1) - float(t0))
        hr = hr_values[i]
        if hr is None:
            continue
        hr = float(hr)
        for idx, zone in enumerate(zones):
            zmin = zone.get("min_hr")
            zmax = zone.get("max_hr")
            if zmin is None:
                continue
            if idx == len(zones) - 1:
                in_zone = hr >= zmin
            elif zmax is None:
                in_zone = hr >= zmin
            else:
                in_zone = zmin <= hr < zmax
            if in_zone:
                totals[zone["name"]] += dt
                break
    return totals


def make_osm_map(points: List[Tuple[float, float]]) -> str:
    mid_idx = len(points) // 2
    center = points[mid_idx]

    m = folium.Map(location=center, zoom_start=13, tiles="OpenStreetMap")

    folium.PolyLine(
        locations=points,
        weight=3,
        opacity=0.9,
        color="#FC4C02",
    ).add_to(m)

    lats = [lat for lat, lon in points]
    lons = [lon for lat, lon in points]
    bounds = [[min(lats), min(lons)], [max(lats), max(lons)]]
    m.fit_bounds(bounds)

    return m._repr_html_()


def make_osm_map_mini(points: List[Tuple[float, float]], height_px: int = WEEKLY_PANEL_HEIGHT_PX) -> str:
    """Small-height map for thumbnails."""
    lats = [lat for lat, _ in points]
    lons = [lon for _, lon in points]
    center = [(min(lats) + max(lats)) / 2.0, (min(lons) + max(lons)) / 2.0]

    m = folium.Map(
        location=center,
        zoom_start=8,
        tiles="OpenStreetMap",
        width="100%",
        height=height_px,
        zoom_control=False,
    )
    folium.PolyLine(
        locations=points,
        weight=3,
        opacity=0.9,
        color="#FC4C02",
    ).add_to(m)
    bounds = [[min(lats)-0.003, min(lons)-0.002], [max(lats)+0.002, max(lons)+0.002]]
    m.fit_bounds(bounds)
    return m._repr_html_()


def downsample_series(x, y, max_points=2000):
    if x is None or y is None:
        return x, y
    if len(x) <= max_points:
        return x, y
    step = max(1, len(x) // max_points)
    return x[::step], y[::step]


def downsample_triplet(x, y, z, max_points=2000):
    if x is None or y is None or z is None:
        return x, y, z
    if len(x) <= max_points:
        return x, y, z
    step = max(1, len(x) // max_points)
    return x[::step], y[::step], z[::step]

def _is_bike_sport(sport: str) -> bool:
    s = (sport or "").strip().lower()
    return "ride" in s or "bike" in s or "cycling" in s


def _is_run_sport(sport: str) -> bool:
    s = (sport or "").strip().lower()
    return "run" in s or "nordic" in s


def _get_avg_speed_mps(act_row: pd.Series) -> float | None:
    avg_speed = act_row.get("average_speed", None)
    if avg_speed is not None and pd.notna(avg_speed):
        return float(avg_speed)
    distance_m = act_row.get("distance", None)
    moving_s = act_row.get("moving_time", None)
    if distance_m and moving_s:
        try:
            return float(distance_m) / float(moving_s)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    return None


def _format_pace_from_speed(speed_mps: float | None) -> str | None:
    if speed_mps is None or speed_mps <= 0:
        return None
    pace_s = 1000.0 / speed_mps
    minutes = int(pace_s // 60)
    seconds = int(round(pace_s % 60))
    if seconds == 60:
        minutes += 1
        seconds = 0
    return f"{minutes}:{seconds:02d} min/km"


def _bucket_main_sport(sport: str) -> str | None:
    s = str(sport or "").strip().lower()
    if "nordic" in s:
        return "Nordic skiing"
    if "trail" in s:
        return "Trail running"
    if "ride" in s or "bike" in s or "cycling" in s:
        return "Biking"
    if "run" in s:
        return "Running"
    if "swim" in s:
        return "Swimming"
    if "weight" in s or "strength" in s:
        return "Weight training"
    return None


def _resolve_sport_bucket(sport: str, known_labels: list[str]) -> str:
    s = re.sub(r"[^a-z0-9]+", "", str(sport or "").lower())
    for label in known_labels:
        if s == re.sub(r"[^a-z0-9]+", "", label.lower()):
            return label
    bucket = _bucket_main_sport(sport)
    if bucket is not None:
        bucket_key = re.sub(r"[^a-z0-9]+", "", bucket.lower())
        for label in known_labels:
            if bucket_key == re.sub(r"[^a-z0-9]+", "", label.lower()):
                return label
    return next(
        (label for label in known_labels if label.strip().lower() == "other"),
        "Other",
    )


def _weekly_area_point_spec(y_field: str, y_title: str, height: int):
    return {
        "layer": [
            {
                "mark": {
                    "type": "area",
                    "opacity": 0.25,
                    "color": "#FC4C02",
                },
                "encoding": {
                    "x": {
                        "field": "week",
                        "type": "quantitative",
                        "axis": {"title": "Week of year"},
                    },
                    "y": {
                        "field": y_field,
                        "type": "quantitative",
                        "axis": {"title": y_title},
                    },
                },
            },
            {
                "mark": {
                    "type": "point",
                    "filled": True,
                    "size": 60,
                    "color": "#FC4C02",
                },
                "encoding": {
                    "x": {"field": "week", "type": "quantitative"},
                    "y": {"field": y_field, "type": "quantitative"},
                    "tooltip": [
                        {"field": "week", "type": "quantitative"},
                        {"field": y_field, "type": "quantitative"},
                    ],
                },
            },
        ],
        "width": "container",
        "height": height,
    }


def _bump_year_refresh():
    st.session_state["year_refresh"] = st.session_state.get("year_refresh", 0) + 1


def _weekly_area_point_chart(df: pd.DataFrame, y_field: str, y_title: str, height: int):
    selection = alt.selection_point(fields=["week"], on="click", empty="none")
    return (
        alt.Chart(df)
        .mark_area(
            opacity=0.25,
            color="#FC4C02",
            line={"color": "#FC4C02"},
            point={"filled": True, "size": 60, "color": "#FC4C02"},
        )
        .add_params(selection)
        .encode(
            x=alt.X("week:Q", title="Week of year"),
            y=alt.Y(f"{y_field}:Q", title=y_title),
            tooltip=[
                alt.Tooltip("week:Q", title="Week"),
                alt.Tooltip(f"{y_field}:Q", title=y_title),
            ],
        )
        .properties(height=height)
    )


def _weekly_hours_km_chart(
    df: pd.DataFrame, hours_field: str, km_field: str, height: int
):
    base = alt.Chart(df).encode(x=alt.X("week:Q", title="Week of year"))
    hours_area = base.mark_area(opacity=0.25, color="#FC4C02").encode(
        y=alt.Y(
            f"{hours_field}:Q",
            title="Hours",
            axis=alt.Axis(orient="left"),
        ),
        tooltip=[
            alt.Tooltip("week:Q", title="Week"),
            alt.Tooltip(f"{hours_field}:Q", title="Hours"),
            alt.Tooltip(f"{km_field}:Q", title="Km"),
        ],
    )
    hours_points = base.mark_point(filled=True, size=60, color="#FC4C02").encode(
        y=alt.Y(f"{hours_field}:Q", axis=None)
    )
    km_line = base.mark_line(color="#0ea5e9", strokeWidth=2).encode(
        y=alt.Y(f"{km_field}:Q", title="Km", axis=alt.Axis(orient="right"))
    )
    return (
        alt.layer(hours_area, hours_points, km_line)
        .resolve_scale(y="independent")
        .properties(height=height)
    )


def _extract_week_from_selection(selection: object) -> int | None:
    def _find_week(obj: object) -> int | None:
        if isinstance(obj, dict):
            if "week" in obj:
                val = obj["week"]
                if isinstance(val, list) and val:
                    val = val[0]
                if isinstance(val, (int, float)):
                    return int(round(val))
            for val in obj.values():
                found = _find_week(val)
                if found is not None:
                    return found
        if isinstance(obj, list):
            for item in obj:
                found = _find_week(item)
                if found is not None:
                    return found
        return None

    return _find_week(selection)


def _week_start_from_year_week(year: int, week: int) -> date:
    try:
        return date.fromisocalendar(year, week, 1)
    except ValueError:
        max_week = date(year, 12, 28).isocalendar().week
        week = min(max(1, week), max_week)
        return date.fromisocalendar(year, week, 1)


def _render_weekly_chart(chart: alt.Chart, key: str, year: int, supports_select: bool):
    try:
        supports_select = supports_select and (
            "on_select" in inspect.signature(st.altair_chart).parameters
        )
    except (TypeError, ValueError):
        supports_select = False

    if supports_select:
        selection = st.altair_chart(
            chart,
            width='stretch',
            key=key,
            on_select="rerun",
        )
        week = _extract_week_from_selection(selection)
        if week is None:
            return
        last_pick = st.session_state.get("last_selected_week")
        current_pick = (year, week)
        if last_pick == current_pick:
            return
        st.session_state["last_selected_week"] = current_pick
        week_start = _week_start_from_year_week(year, week)
        st.session_state["week_pick"] = week_start
        st.session_state["calendar_month"] = date(week_start.year, week_start.month, 1)
        st.session_state["pending_view"] = "Week overview"
        try:
            st.experimental_rerun()
        except AttributeError:
            st.rerun()
    else:
        st.altair_chart(chart, width='stretch', key=key)


def build_interactive_map_plot_html(
    *,
    activity_id: int,
    gpx_times,
    gpx_lats,
    gpx_lons,
    t_hr,
    hr,
    t_power,
    power,
    elev_t,
    elev,
):
    """Return HTML with a Leaflet map + Plotly chart linked by hover."""
    map_times, map_lats, map_lons = downsample_triplet(
        gpx_times, gpx_lats, gpx_lons, max_points=2000
    )
    t_hr, hr = downsample_series(t_hr, hr, max_points=4000)
    t_power, power = downsample_series(t_power, power, max_points=4000)
    elev_t, elev = downsample_series(elev_t, elev, max_points=4000)

    data = {
        "activity_id": activity_id,
        "gpx_times": map_times,
        "gpx_lats": map_lats,
        "gpx_lons": map_lons,
        "t_hr": t_hr,
        "hr": hr,
        "t_power": t_power,
        "power": power,
        "elev_t": elev_t,
        "elev": elev,
    }
    payload = json.dumps(data)

    return f"""
    <div id="wrap-{activity_id}" style="display:flex; gap:16px;">
      <div id="map-{activity_id}" style="flex:1; height:520px;"></div>
      <div style="flex:1; display:flex; flex-direction:column; min-width:0;">
        <div id="plot-{activity_id}" style="height:520px;"></div>
        <div id="plot-controls-{activity_id}" style="display:flex; flex-wrap:wrap; gap:10px; margin-top:6px; font-size:12px;"></div>
      </div>
    </div>
    <link
      rel="stylesheet"
      href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
      crossorigin=""
    />
    <script
      src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
      integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
      crossorigin=""
    ></script>
    <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
    <script>
      const data = {payload};
      const mapId = "map-{activity_id}";
      const plotId = "plot-{activity_id}";
      const controlsId = "plot-controls-{activity_id}";

      const map = L.map(mapId);
      L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
        maxZoom: 19,
        attribution: "&copy; OpenStreetMap contributors"
      }}).addTo(map);

      const latlngs = data.gpx_lats.map((lat, i) => [lat, data.gpx_lons[i]]);
      if (latlngs.length > 0) {{
        const poly = L.polyline(latlngs, {{color: "#FC4C02", weight: 3, opacity: 0.9}});
        poly.addTo(map);
        map.fitBounds(poly.getBounds());
      }}

      const marker = latlngs.length > 0
        ? L.circleMarker(latlngs[0], {{radius: 6, color: "#c2410c", fillOpacity: 0.9}}).addTo(map)
        : null;

      function computeVam(times, elevations) {{
        if (!times || !elevations || times.length !== elevations.length || times.length < 2) return null;
        const vam = new Array(times.length).fill(null);
        for (let i = 1; i < times.length; i++) {{
          const dt = Number(times[i]) - Number(times[i - 1]);
          const de = Number(elevations[i]) - Number(elevations[i - 1]);
          if (!Number.isFinite(dt) || !Number.isFinite(de) || dt <= 0) continue;
          // Instant VAM in m/h from elevation deltas.
          vam[i] = (de / dt) * 3600.0;
        }}
        return vam;
      }}

      function rollingMeanByTime(values, times, windowSeconds) {{
        if (!values || !times || values.length === 0 || values.length !== times.length) return null;
        const windowSize = Math.max(1, Number(windowSeconds) || 1);
        const halfWindow = windowSize / 2.0;
        const sums = new Array(values.length + 1).fill(0.0);
        const counts = new Array(values.length + 1).fill(0);
        const out = new Array(values.length).fill(null);

        for (let i = 0; i < values.length; i++) {{
          const v = values[i];
          const isValid = v !== null && v !== undefined && Number.isFinite(v);
          sums[i + 1] = sums[i] + (isValid ? v : 0.0);
          counts[i + 1] = counts[i] + (isValid ? 1 : 0);
        }}

        let lo = 0;
        let hi = -1;
        for (let i = 0; i < values.length; i++) {{
          const centerTime = Number(times[i]);
          if (!Number.isFinite(centerTime)) continue;
          while (lo < values.length && Number(times[lo]) < centerTime - halfWindow) lo += 1;
          while (hi + 1 < values.length && Number(times[hi + 1]) <= centerTime + halfWindow) hi += 1;
          const count = counts[hi + 1] - counts[lo];
          out[i] = count > 0 ? (sums[hi + 1] - sums[lo]) / count : null;
        }}
        return out;
      }}

      function clampAbs(values, maxAbs) {{
        if (!values) return null;
        return values.map((v) => {{
          if (v === null || v === undefined || !Number.isFinite(v)) return null;
          if (v > maxAbs) return maxAbs;
          if (v < -maxAbs) return -maxAbs;
          return v;
        }});
      }}

      const defaultVamSmoothingSeconds = 30;
      let vamRaw = null;
      let vamTraceIndex = -1;
      const traces = [];
      if (data.hr && data.t_hr) {{
        traces.push({{
          x: data.t_hr,
          y: data.hr,
          mode: "lines",
          name: "HR (bpm)",
          line: {{color: "#FC4C02"}},
          meta: "hr",
        }});
      }}
      if (data.elev && data.elev_t) {{
        traces.push({{
          x: data.elev_t,
          y: data.elev,
          mode: "lines",
          name: "Elevation (m)",
          yaxis: "y2",
          line: {{color: "#d94b0b"}},
          meta: "elevation",
        }});
        vamRaw = computeVam(data.elev_t, data.elev);
        const vamSmoothed = vamRaw
          ? rollingMeanByTime(vamRaw, data.elev_t, defaultVamSmoothingSeconds)
          : null;
        const vam = vamSmoothed ? clampAbs(vamSmoothed, 2500) : null;
        if (vam) {{
          vamTraceIndex = traces.length;
          traces.push({{
            x: data.elev_t,
            y: vam,
            mode: "lines",
            name: "VAM (m/h)",
            yaxis: "y4",
            line: {{color: "#16a34a", width: 1.6}},
            meta: "vam",
          }});
        }}
      }}
      if (data.power && data.t_power) {{
        traces.push({{
          x: data.t_power,
          y: data.power,
          mode: "lines",
          name: "Power (W)",
          yaxis: "y3",
          line: {{color: "#0ea5e9"}},
          meta: "power",
        }});
      }}

      const layout = {{
        paper_bgcolor: "#0d1016",
        plot_bgcolor: "#0d1016",
        font: {{color: "#f5f6f8", family: "Inter, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"}},
        margin: {{l: 40, r: 40, t: 20, b: 40}},
        xaxis: {{
          title: "Time (s)",
          gridcolor: "#252b36",
          zerolinecolor: "#343b49",
        }},
        yaxis: {{
          title: "HR",
          gridcolor: "#252b36",
          zerolinecolor: "#343b49",
        }},
        yaxis2: {{
          title: "Elevation (m)",
          overlaying: "y",
          side: "right",
          anchor: "free",
          position: 0.90,
          showgrid: false,
          zeroline: false,
          titlefont: {{color: "#d94b0b"}},
          tickfont: {{color: "#d94b0b"}},
        }},
        yaxis3: {{
          title: "Power (W)",
          overlaying: "y",
          side: "right",
          anchor: "free",
          position: 0.95,
          showgrid: false,
          zeroline: false,
          titlefont: {{color: "#0ea5e9"}},
          tickfont: {{color: "#0ea5e9"}},
        }},
        yaxis4: {{
          title: "VAM (m/h)",
          overlaying: "y",
          side: "right",
          anchor: "free",
          position: 1.0,
          showgrid: false,
          zeroline: false,
          titlefont: {{color: "#16a34a"}},
          tickfont: {{color: "#16a34a"}},
        }},
        legend: {{
          orientation: "h",
          y: 1.1,
          bgcolor: "rgba(13, 16, 22, 0)",
          font: {{color: "#f5f6f8"}},
        }},
        hovermode: "x",
      }};

      Plotly.newPlot(plotId, traces, layout, {{displayModeBar: false, responsive: true}})
        .then((gd) => {{
          const controls = document.getElementById(controlsId);
          if (controls) {{
            if (vamRaw && vamTraceIndex >= 0) {{
              const smoothingControl = document.createElement("label");
              smoothingControl.style.display = "inline-flex";
              smoothingControl.style.alignItems = "center";
              smoothingControl.style.gap = "7px";
              smoothingControl.style.color = "#f3f4f6";
              smoothingControl.style.paddingRight = "12px";
              smoothingControl.style.borderRight = "1px solid #343b49";

              const smoothingText = document.createElement("span");
              smoothingText.textContent = `VAM smoothing: ${{defaultVamSmoothingSeconds}} s`;
              smoothingText.style.minWidth = "126px";

              const smoothingSlider = document.createElement("input");
              smoothingSlider.type = "range";
              smoothingSlider.min = "1";
              smoothingSlider.max = "300";
              smoothingSlider.step = "1";
              smoothingSlider.value = String(defaultVamSmoothingSeconds);
              smoothingSlider.setAttribute("aria-label", "VAM rolling-average smoothing in seconds");
              smoothingSlider.style.width = "150px";
              smoothingSlider.style.accentColor = "#16a34a";
              smoothingSlider.addEventListener("input", () => {{
                const smoothingSeconds = Number(smoothingSlider.value);
                smoothingText.textContent = `VAM smoothing: ${{smoothingSeconds}} s`;
                const updatedVam = clampAbs(
                  rollingMeanByTime(vamRaw, data.elev_t, smoothingSeconds),
                  2500
                );
                Plotly.restyle(gd, {{y: [updatedVam]}}, [vamTraceIndex]);
              }});

              smoothingControl.appendChild(smoothingText);
              smoothingControl.appendChild(smoothingSlider);
              controls.appendChild(smoothingControl);
            }}
            traces.forEach((trace, idx) => {{
              const label = document.createElement("label");
              label.style.display = "inline-flex";
              label.style.alignItems = "center";
              label.style.gap = "4px";
              label.style.color = "#f3f4f6";
              const checkbox = document.createElement("input");
              checkbox.type = "checkbox";
              checkbox.checked = true;
              checkbox.style.width = "12px";
              checkbox.style.height = "12px";
              checkbox.addEventListener("change", () => {{
                Plotly.restyle(gd, {{visible: checkbox.checked}}, [idx]);
              }});
              label.appendChild(checkbox);
              label.appendChild(document.createTextNode(trace.name));
              controls.appendChild(label);
            }});
          }}
          gd.on("plotly_hover", (evt) => {{
            if (!marker || !evt.points || !evt.points.length) return;
            const x = evt.points[0].x;
            const times = data.gpx_times;
            if (!times || times.length === 0) return;
            let lo = 0;
            let hi = times.length - 1;
            while (lo < hi) {{
              const mid = Math.floor((lo + hi) / 2);
              if (times[mid] < x) lo = mid + 1;
              else hi = mid;
            }}
            const idx = lo;
            const lat = data.gpx_lats[idx];
            const lon = data.gpx_lons[idx];
            if (lat !== null && lon !== null && lat !== undefined && lon !== undefined) {{
              marker.setLatLng([lat, lon]);
            }}
          }});
        }});
    </script>
    """


def load_hr_stream(activity_id: int):
    try:
        t, hr, _ = get_hr_stream_cached(activity_id, use_cache=True)
        return t, hr
    except Exception:
        return None, None


def load_power_stream(activity_id: int):
    try:
        t, watts, _ = get_power_stream_cached(activity_id, use_cache=True)
        return t, watts
    except Exception:
        return None, None


def render_available_stream_plots(*, t_hr, hr, t_power, power):
    rendered = 0

    if t_hr is not None and hr is not None:
        hr_df = pd.DataFrame({"time_s": t_hr, "value": hr}).dropna()
        if not hr_df.empty:
            hr_chart = (
                alt.Chart(hr_df)
                .mark_line(color="#FC4C02")
                .encode(
                    x=alt.X("time_s:Q", title="Time (s)"),
                    y=alt.Y("value:Q", title="Heart rate (bpm)"),
                    tooltip=[
                        alt.Tooltip("time_s:Q", title="Time (s)"),
                        alt.Tooltip("value:Q", title="Heart rate"),
                    ],
                )
                .properties(height=180)
            )
            st.altair_chart(hr_chart, width='stretch')
            rendered += 1

    if t_power is not None and power is not None:
        power_df = pd.DataFrame({"time_s": t_power, "value": power}).dropna()
        if not power_df.empty:
            power_chart = (
                alt.Chart(power_df)
                .mark_line(color="#0ea5e9")
                .encode(
                    x=alt.X("time_s:Q", title="Time (s)"),
                    y=alt.Y("value:Q", title="Power (W)"),
                    tooltip=[
                        alt.Tooltip("time_s:Q", title="Time (s)"),
                        alt.Tooltip("value:Q", title="Power"),
                    ],
                )
                .properties(height=180)
            )
            st.altair_chart(power_chart, width='stretch')
            rendered += 1

    return rendered


# ---------- UI HELPERS ----------


def render_app_theme() -> None:
    st.markdown(
        """
        <style>
        :root {
            --app-bg: #0d1016;
            --app-panel: #151922;
            --app-panel-soft: #1f232d;
            --app-sidebar: #272a34;
            --app-border: #3a4150;
            --app-border-soft: #2d3340;
            --app-text: #f5f6f8;
            --app-muted: #a7adb8;
            --app-orange: #FC4C02;
            --app-orange-hover: #e04500;
            --app-blue: #0ea5e9;
            --app-green: #22c55e;
            --app-nav-height: 3.15rem;
        }

        .stApp {
            background: var(--app-bg);
            color: var(--app-text);
        }

        [data-testid="stSidebar"] {
            background: var(--app-sidebar);
        }

        [data-testid="stSidebar"] h2,
        [data-testid="stSidebar"] h3,
        [data-testid="stSidebar"] p,
        [data-testid="stSidebar"] label,
        [data-testid="stSidebar"] span {
            color: var(--app-text);
        }

        [data-testid="stMainBlockContainer"] {
            padding-top: 1.25rem;
            max-width: 1560px;
        }

        h1, h2, h3, h4 {
            letter-spacing: 0;
        }

        div[data-testid="stButton"] > button {
            border-radius: 8px;
            border-color: var(--app-border);
            background: #12161f;
            color: var(--app-text);
            font-weight: 650;
        }

        div[data-testid="stButton"] > button:hover {
            border-color: #5b6475;
            color: var(--app-text);
        }

        div[data-testid="stButton"] > button[kind="primary"] {
            background: var(--app-orange);
            border-color: var(--app-orange);
            color: #ffffff;
        }

        div[data-testid="stButton"] > button[kind="primary"]:hover {
            background: var(--app-orange-hover);
            border-color: var(--app-orange-hover);
            color: #ffffff;
        }

        [data-testid="stSidebar"] div[data-testid="stButton"] > button {
            font-size: 0.86rem;
            min-height: 2.1rem;
            padding: 0.35rem 0.55rem;
        }

        .app-brand {
            display: flex;
            align-items: center;
            gap: 12px;
            margin: 8px 0 24px 0;
        }

        .app-brand-mark {
            width: 38px;
            height: 38px;
            border-radius: 8px;
            background: var(--app-orange);
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .app-brand-mark svg {
            transform: rotate(180deg);
            transform-origin: center;
        }

        .app-brand-name {
            font-size: 1.05rem;
            font-weight: 800;
            letter-spacing: 0.04em;
        }

        .view-header {
            display: flex;
            align-items: flex-end;
            justify-content: space-between;
            gap: 18px;
            margin: 1.9rem 0 1.1rem 0;
        }

        .view-header h2 {
            margin: 0;
            font-size: 1.9rem;
            line-height: 1.14;
            font-weight: 800;
        }

        .view-header .meta,
        .section-label,
        .sidebar-muted {
            color: var(--app-muted);
            font-size: 0.92rem;
            font-weight: 550;
        }

        .section-title {
            margin: 1.55rem 0 0.75rem 0;
            font-size: 1.28rem;
            font-weight: 780;
            color: var(--app-text);
        }

        .metric-grid {
            display: grid;
            gap: 12px;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            margin: 0.7rem 0 1rem 0;
        }

        .metric-card {
            min-height: 82px;
            border: 1px solid var(--app-border-soft);
            border-radius: 8px;
            background: var(--app-panel);
            padding: 14px 16px;
        }

        .metric-label {
            color: var(--app-muted);
            font-size: 0.78rem;
            font-weight: 700;
            margin-bottom: 8px;
        }

        .metric-value {
            color: var(--app-text);
            font-size: 1.55rem;
            line-height: 1.1;
            font-weight: 760;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .metric-value.small {
            font-size: 1.28rem;
        }

        .sidebar-panel {
            border-top: 1px solid var(--app-border-soft);
            padding-top: 18px;
            margin-top: 18px;
        }

        .sidebar-panel h3 {
            font-size: 1rem;
            margin: 0 0 0.45rem 0;
            font-weight: 780;
        }

        .sidebar-week {
            border: 1px solid var(--app-border-soft);
            background: rgba(13, 16, 22, 0.45);
            border-radius: 8px;
            padding: 12px;
            margin: 0.65rem 0 0.8rem 0;
        }

        .sidebar-week .label {
            color: var(--app-muted);
            font-size: 0.78rem;
            font-weight: 700;
            margin-bottom: 4px;
        }

        .sidebar-week .value {
            font-size: 1.02rem;
            font-weight: 760;
        }

        .week-nav-row {
            height: 0;
            margin: 0;
            padding: 0;
        }

        div[data-testid="stElementContainer"]:has(.week-nav-row) + div[data-testid="stHorizontalBlock"] {
            margin: 0.35rem 0 1.25rem 0;
            align-items: center;
        }

        div[data-testid="stElementContainer"]:has(.week-nav-row) + div[data-testid="stHorizontalBlock"] div[data-testid="stButton"] > button {
            min-height: 2rem;
            padding: 0.2rem 0.65rem;
            font-size: 0.84rem;
            border-radius: 7px;
            background: transparent;
            border-color: transparent;
            color: var(--app-muted);
        }

        div[data-testid="stElementContainer"]:has(.week-nav-row) + div[data-testid="stHorizontalBlock"] div[data-testid="stButton"] > button:hover {
            border-color: var(--app-border-soft);
            color: var(--app-text);
            background: rgba(21, 25, 34, 0.38);
        }

        .week-title {
            text-align: center;
            margin: 0;
        }

        .week-title h2 {
            margin: 0;
            font-size: 1.35rem;
            line-height: 1.15;
            font-weight: 800;
        }

        .week-title .meta {
            margin-top: 0.18rem;
            color: var(--app-muted);
            font-size: 0.78rem;
            font-weight: 720;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }

        .week-activity-slot-placeholder {
            height: 50px;
        }

        .view-switch-row {
            position: sticky;
            top: 0;
            z-index: 999;
            min-height: var(--app-nav-height);
            margin: -0.15rem 0 1.05rem 0;
            padding: 0.45rem 0 0.2rem 0;
            background: rgba(13, 16, 22, 0.94);
            border-bottom: 1px solid var(--app-border-soft);
            backdrop-filter: blur(12px);
        }

        div[data-testid="stStatusWidget"],
        div[data-testid="stNotification"],
        div[data-testid="stAlert"] {
            scroll-margin-top: calc(var(--app-nav-height) + 1rem);
        }

        .view-switch-row div[data-testid="stButton"] > button {
            min-height: 2rem;
            padding: 0.15rem 0.35rem 0.45rem 0.35rem;
            border: 0;
            border-bottom: 2px solid transparent;
            border-radius: 0;
            background: transparent;
            color: var(--app-muted);
            font-size: 0.88rem;
            font-weight: 760;
            box-shadow: none;
        }

        .view-switch-row div[data-testid="stButton"] > button:hover {
            border-color: transparent;
            color: var(--app-text);
            background: transparent;
        }

        .view-switch-row div[data-testid="stButton"] > button[kind="primary"] {
            background: transparent;
            border: 0;
            border-bottom: 2px solid var(--app-orange);
            color: var(--app-text);
        }

        .view-switch-row div[data-testid="stButton"] > button[kind="primary"]:hover {
            background: transparent;
            border-bottom-color: var(--app-orange);
            color: var(--app-text);
        }

        [data-testid="stMetric"] {
            border: 1px solid var(--app-border-soft);
            border-radius: 8px;
            background: var(--app-panel);
            padding: 0.8rem 0.9rem;
        }

        [data-testid="stMetricLabel"] {
            color: var(--app-muted);
        }

        .block-container hr {
            border-color: var(--app-border-soft);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_brand() -> None:
    st.sidebar.markdown(
        """
        <div class="app-brand">
          <div class="app-brand-mark">
            <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
              <path d="M13.45 3 L6.2 20 h4.3 l2.95-7 l2.95 7 h4.4 z" fill="#ffffff"/>
              <path d="M13.45 13 L10.8 20 h2.65 z" fill="#ffffff"/>
            </svg>
          </div>
          <div class="app-brand-name">stravapas</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_view_header(title: str, meta: str | None = None) -> None:
    meta_html = f'<div class="meta">{meta}</div>' if meta else ""
    st.markdown(
        f"""
        <div class="view-header">
            <h2>{title}</h2>
            {meta_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_top_nav(active_view: str) -> None:
    st.markdown(
        '<div class="view-switch-row">',
        unsafe_allow_html=True,
    )
    view_labels = ["Week overview", "Activity detail", "Year overview", "Training plan"]
    view_cols = st.columns(4)
    for col, label in zip(view_cols, view_labels):
        with col:
            if st.button(
                label,
                key=f"switch_view_{label}",
                type="primary" if active_view == label else "secondary",
                width="stretch",
            ):
                st.session_state["active_tab"] = label
                st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


def render_week_title(week_start: date) -> None:
    st.markdown(
        f"""
        <div class="week-title">
            <h2>{format_week_range(week_start)}</h2>
            <div class="meta">Week {week_start.isocalendar().week}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_section_title(title: str) -> None:
    st.markdown(f'<div class="section-title">{title}</div>', unsafe_allow_html=True)


def _display_value(value) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    return str(value)


def render_metric_cards(metrics: list[tuple[str, object]], columns: int | None = None) -> None:
    visible = [(label, _display_value(value)) for label, value in metrics]
    visible = [(label, value) for label, value in visible if value is not None and value != ""]
    if not visible:
        return
    template = (
        f"repeat({columns}, minmax(0, 1fr))"
        if columns
        else "repeat(auto-fit, minmax(150px, 1fr))"
    )
    cards = []
    for label, value in visible:
        value_class = "metric-value small" if len(value) > 14 else "metric-value"
        safe_label = html_escape(str(label))
        safe_value = html_escape(str(value))
        cards.append(
            f'<div class="metric-card"><div class="metric-label">{safe_label}</div>'
            f'<div class="{value_class}">{safe_value}</div></div>'
        )
    st.markdown(
        f'<div class="metric-grid" style="grid-template-columns:{template};">'
        + "".join(cards)
        + "</div>",
        unsafe_allow_html=True,
    )


def _sport_options(existing_sports: list[str] | tuple[str, ...], current: str | None = None) -> list[str]:
    sports = {str(s) for s in existing_sports if str(s).strip()}
    sports.update(DEFAULT_MANUAL_SPORTS)
    if current:
        sports.add(str(current))
    return sorted(sports)


def render_manual_activity_entry_form(
    default_date: date,
    existing_sports: list[str] | tuple[str, ...],
) -> None:
    render_section_title("Manual activity")
    st.caption("Saved in `manual_activities.json` and included in weekly/year totals.")
    with st.form(key=f"manual_activity_create_{default_date.isoformat()}"):
        c1, c2 = st.columns(2)
        activity_date_input = c1.date_input("Date", value=default_date)
        sport_choice = c2.selectbox(
            "Activity type",
            _sport_options(existing_sports),
            index=_sport_options(existing_sports).index("Run")
            if "Run" in _sport_options(existing_sports)
            else 0,
        )
        name_input = st.text_input(
            "Name",
            value=f"Manual {sport_choice}",
            placeholder="Manual activity name",
        )
        m1, m2, m3 = st.columns(3)
        distance_input = m1.text_input("Distance (km)", value="", placeholder="0")
        moving_input = m2.text_input("Moving time (h)", value="", placeholder="1.25")
        elapsed_input = m3.text_input("Elapsed time (h)", value="", placeholder="optional")
        m4, m5, m6 = st.columns(3)
        elev_input = m4.text_input("Elevation gain (m)", value="", placeholder="0")
        cadence_input = m5.text_input("Average cadence", value="", placeholder="optional")
        kjs_input = m6.text_input("Work (kJ)", value="", placeholder="optional")
        h1, h2, h3 = st.columns(3)
        avg_hr_input = h1.text_input("Average HR (bpm)", value="", placeholder="optional")
        max_hr_input = h2.text_input("Max HR (bpm)", value="", placeholder="optional")
        avg_power_input = h3.text_input("Average power (W)", value="", placeholder="optional")
        notes_input = st.text_area("Notes", value="", height=90)
        s1, s2 = st.columns(2)
        save_clicked = s1.form_submit_button("Add activity")
        cancel_clicked = s2.form_submit_button("Cancel")

    if cancel_clicked:
        st.session_state.pop("manual_activity_date", None)
        st.rerun()

    if save_clicked:
        try:
            activity_id = create_manual_activity(
                activity_date=activity_date_input,
                name=name_input,
                sport=sport_choice,
                distance_km=parse_optional_float(distance_input),
                moving_time_h=parse_optional_float(moving_input),
                elapsed_time_h=parse_optional_float(elapsed_input),
                elevation_m=parse_optional_float(elev_input),
                average_heartrate=parse_optional_float(avg_hr_input),
                max_heartrate=parse_optional_float(max_hr_input),
                average_cadence=parse_optional_float(cadence_input),
                average_watts=parse_optional_float(avg_power_input),
                kilojoules=parse_optional_float(kjs_input),
                notes=notes_input,
            )
        except ValueError:
            st.error("Invalid number format. Use values like `10.5`, `1.25`, or leave blank.")
        else:
            load_activities.clear()
            st.session_state["selected_activity_id"] = activity_id
            st.session_state["selected_date"] = activity_date_input
            st.session_state["week_pick"] = activity_date_input
            st.session_state["calendar_month"] = date(
                activity_date_input.year,
                activity_date_input.month,
                1,
            )
            st.session_state.pop("manual_activity_date", None)
            st.success("Manual activity added.")
            st.rerun()


def format_week_range(week_start: date) -> str:
    week_end = week_start + timedelta(days=6)
    if week_start.month == week_end.month:
        return f"{week_start.strftime('%b')} {week_start.day}-{week_end.day}, {week_start.year}"
    return f"{week_start.strftime('%b')} {week_start.day} - {week_end.strftime('%b')} {week_end.day}, {week_start.year}"


# ---------- STREAMLIT APP ----------


def main():
    st.set_page_config(page_title="stravapas", layout="wide")
    render_app_theme()

    if "active_tab" not in st.session_state:
        st.session_state["active_tab"] = "Week overview"
    if "pending_view" in st.session_state:
        st.session_state["active_tab"] = st.session_state.pop("pending_view")
    render_top_nav(st.session_state["active_tab"])

    if "ran_sync" not in st.session_state:
        with st.spinner("Syncing latest activities (GPX/HR/Power)..."):
            try:
                sync_latest()
                st.session_state["ran_sync"] = True
            except Exception as e:
                st.error(f"Sync failed: {e}")
                st.session_state["ran_sync"] = False

    df = load_activities()
    # Auto-trigger map build only once per Streamlit session to avoid spawning
    # repeated background python processes on every rerun.
    if "auto_map_build_started" not in st.session_state:
        start_full_map_build(force=False)
        st.session_state["auto_map_build_started"] = True
    all_dates = sorted(df["activity_date"].unique())
    last_activity_date = max(all_dates) if all_dates else date.today()
    if "selected_activity_id" not in st.session_state:
        st.session_state["selected_activity_id"] = None
    if "selected_date" not in st.session_state:
        st.session_state["selected_date"] = last_activity_date

    default_date = last_activity_date

    render_brand()

    selected_sports = df["sport"].dropna().unique().tolist()
    selected_sports_key = tuple(selected_sports)

    if all_dates:
        year_options = list(range(min(all_dates).year, max(all_dates).year + 1))
    else:
        year_options = [date.today().year]
    overrides_mtime_ns = _path_mtime_ns(OVERRIDES_PATH)
    manual_mtime_ns = _path_mtime_ns(MANUAL_ACTIVITIES_PATH)
    zones_mtime_ns = _path_mtime_ns(HR_ZONES_PATH)
    current_year = date.today().year
    latest_year = year_options[-1]
    closed_year_options = [y for y in year_options if y < current_year]
    snapshot_years = tuple(closed_year_options[-YEAR_SNAPSHOT_PREVIOUS_COUNT:])

    snapshot_init_key = (
        snapshot_years,
        selected_sports_key,
        overrides_mtime_ns,
        manual_mtime_ns,
        zones_mtime_ns,
    )
    if st.session_state.get("year_snapshot_init_key") != snapshot_init_key:
        for y in snapshot_years:
            if load_saved_year_overview_bundle_any_revision(y) is None:
                y_df = _year_slice(df, y, selected_sports_key)
                y_revision = _year_bundle_revision(
                    y_df,
                    selected_sports_key,
                    overrides_mtime_ns=overrides_mtime_ns,
                    zones_mtime_ns=zones_mtime_ns,
                )
                with st.spinner(f"Building saved year file for {y}..."):
                    y_bundle = get_year_overview_bundle(
                        year=y,
                        selected_sports=selected_sports_key,
                    )
                    save_year_overview_bundle(y, y_revision, y_bundle)
        st.session_state["year_snapshot_init_key"] = snapshot_init_key

    if "week_pick" not in st.session_state:
        st.session_state["week_pick"] = last_activity_date
    if "calendar_month" not in st.session_state:
        st.session_state["calendar_month"] = date(
            st.session_state["week_pick"].year,
            st.session_state["week_pick"].month,
            1,
        )
    display_week_start = st.session_state["week_pick"] - timedelta(
        days=st.session_state["week_pick"].weekday()
    )
    display_week_end = display_week_start + timedelta(days=6)
    # Week navigation is intentionally allowed beyond the recorded activity
    # range. Keep the date input bounds wide enough to include that state so a
    # previous/next navigation click cannot leave the widget with an invalid
    # default value.
    sidebar_date_candidates = [*all_dates, display_week_start]
    sidebar_date_min = min(sidebar_date_candidates)
    sidebar_date_max = max(sidebar_date_candidates)

    st.sidebar.markdown(
        f"""
        <div class="sidebar-panel">
            <h3>Current week</h3>
            <div class="sidebar-week">
                <div class="value">{format_week_range(display_week_start)}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    jump_date = st.sidebar.date_input(
        "Jump to week",
        value=display_week_start,
        min_value=sidebar_date_min,
        max_value=sidebar_date_max,
        key=f"sidebar_week_jump_{display_week_start.isoformat()}",
    )
    if jump_date != display_week_start:
        st.session_state["week_pick"] = jump_date
        st.session_state["calendar_month"] = date(jump_date.year, jump_date.month, 1)
        st.session_state["pending_view"] = "Week overview"
        st.rerun()

    st.sidebar.markdown('<div class="sidebar-panel"><h3>Full sport map</h3></div>', unsafe_allow_html=True)
    full_map_status = get_full_map_status()
    if full_map_status["running"]:
        st.sidebar.caption("Status: building in background...")
    elif full_map_status["exists"] and not full_map_status["needs_rebuild"]:
        st.sidebar.caption("Status: ready")
    elif full_map_status["exists"]:
        st.sidebar.caption("Status: ready (update pending)")
    else:
        st.sidebar.caption("Status: not built yet")
    if full_map_status.get("last_error"):
        st.sidebar.caption(f"Last build message: {full_map_status['last_error']}")

    if st.sidebar.button("Open full map", key="open_full_sport_map", width='stretch'):
        st.session_state["active_tab"] = "Full map"
        st.rerun()
    if st.sidebar.button("Rebuild map", key="rebuild_full_sport_map", width='stretch'):
        if start_full_map_build(force=True):
            st.sidebar.success("Map rebuild started in background.")
        else:
            st.sidebar.info("Map build already running.")

    view = st.session_state["active_tab"]

    if view == "Full map":
        render_view_header("Full sport map")
        status = get_full_map_status()
        c1, c2 = st.columns([1, 1])
        with c1:
            if st.button("Back to week overview", type="primary", width='stretch'):
                st.session_state["active_tab"] = "Week overview"
                st.rerun()
        with c2:
            if st.button("Refresh map status", width='stretch'):
                st.rerun()

        if status["running"] and not status["exists"]:
            st.info("Map is being built in the background. Please wait a moment and refresh.")
        elif not status["exists"]:
            st.warning("Map file not found yet. Trigger a rebuild from the sidebar widget.")
        else:
            try:
                map_html = FULL_MAP_HTML_PATH.read_text(encoding="utf-8")
                html(map_html, height=920, scrolling=True)
                if status["running"]:
                    st.caption("A newer version is currently being rebuilt in the background.")
                elif status["needs_rebuild"]:
                    st.caption("Map file is available; a newer version will be built in background.")
            except Exception as exc:
                st.error(f"Could not load map HTML: {exc}")

    if view == "Week overview":
        ref_day = st.session_state["week_pick"]

        week_start = ref_day - timedelta(days=ref_day.weekday())  # Monday
        week_end = week_start + timedelta(days=6)
        week_dates = [week_start + timedelta(days=i) for i in range(7)]
        st.markdown('<div class="week-nav-row"></div>', unsafe_allow_html=True)
        nav_left, nav_title, nav_add, nav_right = st.columns([1, 5, 0.55, 1])
        with nav_left:
            if st.button("← Previous", key=f"week_prev_{week_start.isoformat()}", width='stretch'):
                new_week = week_start - timedelta(days=7)
                st.session_state["week_pick"] = new_week
                st.session_state["calendar_month"] = date(new_week.year, new_week.month, 1)
                st.rerun()
        with nav_title:
            render_week_title(week_start)
        with nav_add:
            if st.button("+", key=f"add_manual_activity_week_{week_start.isoformat()}", help="Add manual activity", width="stretch"):
                today = date.today()
                st.session_state["manual_activity_date"] = (
                    today if week_start <= today <= week_end else week_start
                )
                st.rerun()
        with nav_right:
            if st.button("Next →", key=f"week_next_{week_start.isoformat()}", width='stretch'):
                new_week = week_start + timedelta(days=7)
                st.session_state["week_pick"] = new_week
                st.session_state["calendar_month"] = date(new_week.year, new_week.month, 1)
                st.rerun()
        weekly_notes = load_weekly_notes()
        weekly_note_default = str(weekly_notes.get(week_start.isoformat(), ""))
        with st.expander("Weekly notes", expanded=False):
            if weekly_note_default.strip():
                st.info(weekly_note_default)
            with st.form(key=f"weekly_note_form_{week_start.isoformat()}"):
                weekly_note_input = st.text_area(
                    "Notes for this week",
                    value=weekly_note_default,
                    placeholder="Add context for the week: fatigue, illness, travel, goals, etc.",
                    height=120,
                )
                w1, w2 = st.columns(2)
                save_weekly_note_clicked = w1.form_submit_button("Save weekly note")
                clear_weekly_note_clicked = w2.form_submit_button("Clear weekly note")
            if save_weekly_note_clicked:
                save_weekly_note(week_start, weekly_note_input)
                st.success("Weekly note saved.")
                st.rerun()
            if clear_weekly_note_clicked:
                save_weekly_note(week_start, None)
                st.success("Weekly note cleared.")
                st.rerun()
        zones = load_hr_zones(HR_ZONES_PATH)
        show_zone_mini = st.toggle("Show zone mini charts", value=False, key="week_show_zone_mini")
        cols = st.columns(7)
        for col, d in zip(cols, week_dates):
            day_acts = df[
                (df["activity_date"] == d) & (df["sport"].isin(selected_sports))
            ]
            with col:
                col.markdown(f"**{d.strftime('%a')}**\n{d.isoformat()}")
                if day_acts.empty:
                    col.caption("No activity")
                    continue
                day_rows = day_acts.reset_index(drop=True)
                day_indices = list(range(len(day_rows)))
                day_select_key = f"week_day_activity_{d.isoformat()}"
                selected_idx_from_state = st.session_state.get(day_select_key)
                if selected_idx_from_state in day_indices:
                    selected_idx_from_state = int(selected_idx_from_state)
                else:
                    # Default to the first activity that has a usable map.
                    selected_idx_from_state = 0
                    for idx in day_indices:
                        act_id_candidate = int(day_rows.iloc[idx]["id"])
                        try:
                            candidate_gpx = ensure_gpx(act_id_candidate)
                            read_gpx_points(candidate_gpx)
                            selected_idx_from_state = idx
                            break
                        except Exception:
                            continue
                    st.session_state[day_select_key] = selected_idx_from_state

                def _activity_summary_label(idx: int) -> str:
                    row = day_rows.iloc[idx]
                    sport = str(row.get("sport", "Activity"))
                    if pd.notna(row.get("distance_km")):
                        dist_km = float(row.get("distance_km", 0.0))
                    else:
                        dist_km = float(row.get("distance", 0.0) or 0.0) / 1000.0
                    if pd.notna(row.get("moving_time_h")):
                        time_h = float(row.get("moving_time_h", 0.0))
                    else:
                        time_h = float(row.get("moving_time", 0.0) or 0.0) / 3600.0
                    return f"{sport} · {dist_km:.1f} km · {time_h:.2f} h"

                selected_row_for_map = day_rows.iloc[selected_idx_from_state]
                selected_act_id_for_map = int(selected_row_for_map["id"])
                try:
                    gpx_path = ensure_gpx(selected_act_id_for_map)
                    pts = read_gpx_points(gpx_path)
                    mini_html = make_osm_map_mini(pts, height_px=WEEKLY_PANEL_HEIGHT_PX)
                    html(mini_html, height=WEEKLY_PANEL_HEIGHT_PX)
                except Exception:
                    no_map_html = (
                        f"<div style='height:{WEEKLY_PANEL_HEIGHT_PX}px; width:100%; box-sizing:border-box; "
                        "display:flex; align-items:center; justify-content:center; "
                        "font-size:0.82rem; color:#9ca3af;'>"
                        "No map</div>"
                    )
                    html(no_map_html, height=WEEKLY_PANEL_HEIGHT_PX)

                if day_indices:
                    first_idx = int(day_indices[0])
                    first_row = day_rows.iloc[first_idx]
                    first_act_id = int(first_row["id"])
                    first_label = _activity_summary_label(first_idx)
                    if col.button(
                        first_label,
                        key=f"open_direct_{first_act_id}_{d.isoformat()}",
                        width="stretch",
                    ):
                        st.session_state["selected_activity_id"] = first_act_id
                        st.session_state["selected_date"] = d
                        st.session_state["pending_view"] = "Activity detail"
                        try:
                            st.experimental_rerun()  # older Streamlit versions
                        except AttributeError:
                            st.rerun()  # newer Streamlit

                for idx in day_indices[1:WEEKLY_ACTIVITY_BUTTON_SLOTS]:
                    idx = int(idx)
                    act_row = day_rows.iloc[idx]
                    act_id = int(act_row["id"])
                    summary_label = _activity_summary_label(idx)
                    if col.button(
                        summary_label,
                        key=f"open_direct_{act_id}_{d.isoformat()}",
                        width="stretch",
                    ):
                        st.session_state["selected_activity_id"] = act_id
                        st.session_state["selected_date"] = d
                        st.session_state["pending_view"] = "Activity detail"
                        try:
                            st.experimental_rerun()  # older Streamlit versions
                        except AttributeError:
                            st.rerun()  # newer Streamlit

                displayed_activity_slots = min(len(day_indices), WEEKLY_ACTIVITY_BUTTON_SLOTS)
                for _ in range(WEEKLY_ACTIVITY_BUTTON_SLOTS - displayed_activity_slots):
                    col.markdown(
                        f"<div class='week-activity-slot-placeholder' "
                        f"style='height:{WEEKLY_ACTIVITY_BUTTON_SLOT_PX}px;'></div>",
                        unsafe_allow_html=True,
                    )

                if show_zone_mini and zones and not day_acts.empty:
                    day_ids = day_acts["id"].astype(int).tolist()
                    zone_totals, _, _ = compute_zone_totals_for_activities(
                        day_ids, zones, activities_df=day_acts
                    )
                    if sum(zone_totals.values()) > 0:
                        day_df = build_zone_chart_df(zone_totals, zones)
                        col.vega_lite_chart(
                            day_df,
                            {
                                "mark": {"type": "bar"},
                                "encoding": {
                                    "x": {
                                        "field": "zone",
                                        "type": "ordinal",
                                        "axis": {"labelAngle": 0, "title": None},
                                    },
                                    "y": {
                                        "field": "minutes",
                                        "type": "quantitative",
                                        "axis": {"title": None},
                                    },
                                    "color": {"field": "zone", "type": "nominal"},
                                    "tooltip": [
                                        {"field": "label", "type": "nominal"},
                                        {"field": "display", "type": "nominal"},
                                    ],
                                },
                                "width": "container",
                                "height": WEEKLY_PANEL_HEIGHT_PX,
                                "config": {
                                    "legend": {"disable": True},
                                    "range": {"category": HR_ZONE_COLORS},
                                },
                            },
                            width='stretch',
                        )
                    else:
                        col.markdown(
                            f"<div style='height:{WEEKLY_PANEL_HEIGHT_PX}px;'></div>",
                            unsafe_allow_html=True,
                        )
                else:
                    col.markdown(
                        f"<div style='height:{WEEKLY_PANEL_HEIGHT_PX}px;'></div>",
                        unsafe_allow_html=True,
                    )

        manual_activity_date = st.session_state.get("manual_activity_date")
        if isinstance(manual_activity_date, date):
            render_manual_activity_entry_form(manual_activity_date, selected_sports)

        render_section_title("Weekly summary")
        week_df = df[
            (df["activity_date"] >= week_start)
            & (df["activity_date"] <= week_end)
            & (df["sport"].isin(selected_sports))
        ].copy()
        week_df_all = df[
            (df["activity_date"] >= week_start) & (df["activity_date"] <= week_end)
        ].copy()

        if week_df.empty:
            st.info("No activities for this week with selected sports.")
        else:
            if "moving_time_h" in week_df.columns:
                total_time_h = week_df["moving_time_h"].sum()
            else:
                total_time_h = week_df["moving_time"].sum() / 3600.0

            if "distance_km" in week_df.columns:
                total_distance_km = week_df["distance_km"].sum()
            else:
                total_distance_km = week_df["distance"].sum() / 1000.0

            total_elev_m = week_df["total_elevation_gain"].fillna(0).sum()

            render_metric_cards(
                [
                    ("Total time", f"{total_time_h:.2f} h"),
                    ("Total distance", f"{total_distance_km:.1f} km"),
                    ("Elevation gain", f"{total_elev_m:.0f} m"),
                ],
                columns=3,
            )

            def _sport_summary_metrics(frame: pd.DataFrame, sport_key: str) -> tuple[float, float]:
                sport_series = frame.get("sport")
                if sport_series is None:
                    return 0.0, 0.0
                sport_norm = sport_series.astype(str).str.lower()
                if sport_key == "run":
                    mask = sport_norm.str.contains("run", na=False)
                elif sport_key == "bike":
                    mask = (
                        sport_norm.str.contains("ride", na=False)
                        | sport_norm.str.contains("cycle", na=False)
                        | sport_norm.str.contains("bike", na=False)
                    )
                else:
                    return 0.0, 0.0

                sub = frame.loc[mask].copy()
                if sub.empty:
                    return 0.0, 0.0

                if "moving_time_h" in sub.columns:
                    hours = float(sub["moving_time_h"].fillna(0).sum())
                else:
                    hours = float(sub["moving_time"].fillna(0).sum()) / 3600.0

                if "distance_km" in sub.columns:
                    distance_km = float(sub["distance_km"].fillna(0).sum())
                else:
                    distance_km = float(sub["distance"].fillna(0).sum()) / 1000.0
                return hours, distance_km

            run_h, run_km = _sport_summary_metrics(week_df_all, "run")
            bike_h, bike_km = _sport_summary_metrics(week_df_all, "bike")

            render_section_title("By sport")
            render_metric_cards(
                [
                    ("Running", f"{run_h:.2f} h / {run_km:.1f} km"),
                    ("Biking", f"{bike_h:.2f} h / {bike_km:.1f} km"),
                ],
                columns=2,
            )

            zones = load_hr_zones(HR_ZONES_PATH)
            if not zones:
                st.info("HR zones config not found or invalid.")
            else:
                zone_totals, missing_hr, avg_only_count = compute_zone_totals_for_activities(
                    week_df["id"].astype(int).tolist(), zones, activities_df=week_df
                )
                if sum(zone_totals.values()) <= 0:
                    st.info("No HR stream data available to compute zones for this week.")
                else:
                    chart_df = build_zone_chart_df(zone_totals, zones)
                    chart_df["zone"] = chart_df["label"]
                    render_section_title("Time in HR zones")
                    st.vega_lite_chart(
                        chart_df,
                        {
                            "mark": {"type": "bar"},
                            "encoding": {
                                "x": {
                                    "field": "zone",
                                    "type": "ordinal",
                                    "axis": {"labelAngle": 0, "title": None},
                                },
                                "y": {
                                    "field": "minutes",
                                    "type": "quantitative",
                                    "axis": {"title": None},
                                },
                                "color": {"field": "zone", "type": "nominal"},
                                "tooltip": [
                                    {"field": "zone", "type": "nominal"},
                                    {"field": "display", "type": "nominal"},
                                ],
                            },
                            "width": "container",
                            "height": 180,
                            "config": {"range": {"category": HR_ZONE_COLORS}},
                        },
                        width='stretch',
                    )
                    if missing_hr:
                        st.caption(
                            f"HR zones computed from {len(week_df) - missing_hr} "
                            f"activities; {missing_hr} missing HR streams."
                        )
                    if avg_only_count:
                        st.caption(
                            "Some zone times are estimated from average heart rate only."
                        )

    if view == "Activity detail":
        initial_date = (
            st.session_state["selected_date"]
            if st.session_state["selected_date"] is not None
            else default_date
        )
        selected_date = st.sidebar.date_input(
            "Select a date",
            value=initial_date,
            min_value=min(all_dates) if all_dates else date.today(),
            max_value=max(all_dates) if all_dates else date.today(),
        )
        st.session_state["selected_date"] = selected_date

        day_df = df[
            (df["activity_date"] == selected_date)
            & (df["sport"].isin(selected_sports))
        ].copy()

        render_view_header(
            "Activity detail",
            f"{selected_date.strftime('%a %b %d, %Y')}",
        )

        if day_df.empty:
            st.info("No activities for this date with selected sports.")
            return

        options = []
        for _, row in day_df.iterrows():
            label = f"{int(row['id'])} – {row.get('name', '')} ({row['sport']})"
            options.append((label, int(row["id"])))

        labels = [o[0] for o in options]
        ids = {o[0]: o[1] for o in options}

        selected_label = None
        default_index = 0
        if st.session_state["selected_activity_id"] is not None:
            for idx, (_, act_id) in enumerate(options):
                if act_id == st.session_state["selected_activity_id"]:
                    default_index = idx
                    break

        selected_label = st.selectbox("Choose an activity", labels, index=default_index)
        activity_id = ids[selected_label]
        st.session_state["selected_activity_id"] = activity_id

        act_row = day_df.loc[day_df["id"] == activity_id].iloc[0]

        col_metrics = st.container()

        with col_metrics:
            render_section_title("Summary")

            distance_km = act_row.get("distance_km") or (
                act_row.get("distance", 0) / 1000.0
            )
            moving_h = act_row.get("moving_time_h") or (
                act_row.get("moving_time", 0) / 3600.0
            )
            elev = act_row.get("total_elevation_gain", 0)

            avg_hr = act_row.get("average_heartrate", None)
            max_hr = act_row.get("max_heartrate", None)
            avg_watts = act_row.get("average_watts", None)
            kjs = act_row.get("kilojoules", None)
            avg_speed_mps = _get_avg_speed_mps(act_row)

            primary_metrics = [
                ("Sport", act_row["sport"]),
                ("Distance", f"{distance_km:.1f} km"),
                ("Moving time", f"{moving_h:.2f} h"),
                ("Elevation gain", f"{elev:.0f} m"),
            ]
            secondary_metrics = []
            if avg_hr is not None and pd.notna(avg_hr):
                secondary_metrics.append(("Avg HR", f"{avg_hr:.0f} bpm"))
            if max_hr is not None and pd.notna(max_hr):
                secondary_metrics.append(("Max HR", f"{max_hr:.0f} bpm"))
            if _is_bike_sport(act_row["sport"]):
                if avg_speed_mps is not None:
                    secondary_metrics.append(("Avg speed", f"{avg_speed_mps * 3.6:.1f} km/h"))
            elif _is_run_sport(act_row["sport"]):
                pace = _format_pace_from_speed(avg_speed_mps)
                if pace is not None:
                    secondary_metrics.append(("Pace", pace))
            if avg_watts is not None and pd.notna(avg_watts):
                secondary_metrics.append(("Avg power", f"{avg_watts:.0f} W"))
            if kjs is not None and pd.notna(kjs):
                secondary_metrics.append(("Work", f"{kjs:.0f} kJ"))

            render_metric_cards(primary_metrics, columns=4)
            render_metric_cards(secondary_metrics)

        manual_overrides = load_activity_overrides().get(str(activity_id), {})
        if not isinstance(manual_overrides, dict):
            manual_overrides = {}
        distance_override_default = (
            f"{float(manual_overrides['distance']) / 1000.0:g}"
            if manual_overrides.get("distance") is not None
            else ""
        )
        moving_override_default = (
            f"{float(manual_overrides['moving_time']) / 3600.0:g}"
            if manual_overrides.get("moving_time") is not None
            else ""
        )
        speed_override_default = (
            f"{float(manual_overrides['average_speed']) * 3.6:g}"
            if manual_overrides.get("average_speed") is not None
            else ""
        )
        elev_override_default = (
            f"{float(manual_overrides['total_elevation_gain']):g}"
            if manual_overrides.get("total_elevation_gain") is not None
            else ""
        )
        power_override_default = (
            f"{float(manual_overrides['average_watts']):g}"
            if manual_overrides.get("average_watts") is not None
            else ""
        )
        hr_override_default = (
            f"{float(manual_overrides['average_heartrate']):g}"
            if manual_overrides.get("average_heartrate") is not None
            else ""
        )
        notes_override_default = str(manual_overrides.get("notes", "")).strip()
        current_sport = str(act_row.get("sport", "Workout"))
        sport_choices = _sport_options(selected_sports, current_sport)
        sport_default_index = (
            sport_choices.index(current_sport) if current_sport in sport_choices else 0
        )
        current_elev = act_row.get("total_elevation_gain", None)
        current_speed = _get_avg_speed_mps(act_row)
        current_power = act_row.get("average_watts", None)
        current_avg_hr = act_row.get("average_heartrate", None)
        current_elev_label = (
            f"{float(current_elev):.0f} m"
            if current_elev is not None and pd.notna(current_elev)
            else "n/a"
        )
        current_speed_label = (
            f"{current_speed * 3.6:.1f} km/h"
            if current_speed is not None and pd.notna(current_speed)
            else "n/a"
        )
        current_power_label = (
            f"{float(current_power):.0f} W"
            if current_power is not None and pd.notna(current_power)
            else "n/a"
        )
        current_hr_label = (
            f"{float(current_avg_hr):.0f} bpm"
            if current_avg_hr is not None and pd.notna(current_avg_hr)
            else "n/a"
        )
        if notes_override_default:
            render_section_title("Notes")
            st.info(notes_override_default)
        with st.expander("Edit activity data", expanded=False):
            st.caption(
                "Saved in `activity_overrides.json` and applied across summaries/charts."
            )
            st.caption(
                "Current values shown: "
                f"type {current_sport}, distance {distance_km:.1f} km, "
                f"moving time {moving_h:.2f} h, speed {current_speed_label}, "
                f"elevation {current_elev_label}, "
                f"avg power {current_power_label}, avg HR {current_hr_label}."
            )
            with st.form(key=f"manual_data_form_{activity_id}"):
                sport_input = st.selectbox(
                    "Activity type",
                    sport_choices,
                    index=sport_default_index,
                )
                distance_input = st.text_input(
                    "Distance override (km)",
                    value=distance_override_default,
                    placeholder="Leave blank to remove override",
                )
                moving_input = st.text_input(
                    "Moving time override (h)",
                    value=moving_override_default,
                    placeholder="Leave blank to remove override",
                )
                speed_input = st.text_input(
                    "Average speed override (km/h)",
                    value=speed_override_default,
                    placeholder="Leave blank to derive from distance and time",
                )
                elev_input = st.text_input(
                    "Elevation gain override (m)",
                    value=elev_override_default,
                    placeholder="Leave blank to remove override",
                )
                power_input = st.text_input(
                    "Average power override (W)",
                    value=power_override_default,
                    placeholder="Leave blank to remove override",
                )
                hr_input = st.text_input(
                    "Average heart rate override (bpm)",
                    value=hr_override_default,
                    placeholder="Leave blank to remove override",
                )
                notes_input = st.text_area(
                    "Notes",
                    value=notes_override_default,
                    placeholder="Any free-text note for this activity",
                    height=100,
                )
                c1, c2 = st.columns(2)
                save_clicked = c1.form_submit_button("Save overrides")
                clear_clicked = c2.form_submit_button("Clear overrides")

            if save_clicked:
                try:
                    distance_value = parse_optional_float(distance_input)
                    moving_value = parse_optional_float(moving_input)
                    speed_value = parse_optional_float(speed_input)
                    elev_value = parse_optional_float(elev_input)
                    power_value = parse_optional_float(power_input)
                    hr_value = parse_optional_float(hr_input)
                except ValueError:
                    st.error("Invalid number format. Use digits like `850` or `245.5`.")
                else:
                    save_activity_overrides(
                        activity_id,
                        {
                            "distance": (
                                distance_value * 1000.0
                                if distance_value is not None
                                else None
                            ),
                            "moving_time": (
                                moving_value * 3600.0
                                if moving_value is not None
                                else None
                            ),
                            "average_speed": (
                                speed_value / 3.6
                                if speed_value is not None
                                else None
                            ),
                            "total_elevation_gain": elev_value,
                            "average_watts": power_value,
                            "average_heartrate": hr_value,
                            "sport": sport_input,
                            "notes": notes_input,
                        },
                    )
                    load_activities.clear()
                    st.success("Manual values saved.")
                    st.rerun()

            if clear_clicked:
                save_activity_overrides(
                    activity_id,
                    {
                        "distance": None,
                        "moving_time": None,
                        "average_speed": None,
                        "total_elevation_gain": None,
                        "average_watts": None,
                        "average_heartrate": None,
                        "sport": None,
                        "notes": None,
                    },
                )
                load_activities.clear()
                st.success("Manual overrides cleared.")
                st.rerun()

        if str(act_row.get("source", "")).lower() == "manual":
            if st.button("Delete manual activity", key=f"delete_manual_activity_{activity_id}"):
                if delete_manual_activity(activity_id):
                    load_activities.clear()
                    st.session_state["selected_activity_id"] = None
                    st.success("Manual activity deleted.")
                    st.rerun()

        render_section_title("Map and data")
        t_hr, hr = load_hr_stream(activity_id)
        t_power, power = load_power_stream(activity_id)
        gpx_times, gpx_lats, gpx_lons = None, None, None
        elev_t, elev = None, None
        gpx_error = None
        try:
            gpx_path = ensure_gpx(activity_id)
            elev_t, elev = read_gpx_elevation_trace(gpx_path)
            gpx_times, gpx_lats, gpx_lons = read_gpx_track_with_time(gpx_path)
        except Exception as e:
            gpx_error = str(e)

        has_map_data = (
            gpx_times is not None
            and gpx_lats is not None
            and gpx_lons is not None
            and len(gpx_times) > 0
        )

        if has_map_data:
            interactive_html = build_interactive_map_plot_html(
                activity_id=activity_id,
                gpx_times=gpx_times,
                gpx_lats=gpx_lats,
                gpx_lons=gpx_lons,
                t_hr=t_hr,
                hr=hr,
                t_power=t_power,
                power=power,
                elev_t=elev_t,
                elev=elev,
            )
            html(interactive_html, height=590, scrolling=False)
        else:
            if gpx_error:
                st.info(f"Map unavailable for this activity ({gpx_error}).")
            else:
                st.info("No GPX timestamps available for hover-linked map.")
            rendered = render_available_stream_plots(
                t_hr=t_hr,
                hr=hr,
                t_power=t_power,
                power=power,
            )
            if rendered == 0:
                st.info("No HR or power streams available for this activity.")

        zones = load_hr_zones(HR_ZONES_PATH)
        if zones:
            zone_totals, _, avg_only_count = compute_zone_totals_for_activities(
                [activity_id], zones, activities_df=day_df
            )
            if sum(zone_totals.values()) > 0:
                render_section_title("HR zones")
                zone_df = build_zone_chart_df(zone_totals, zones)
                st.vega_lite_chart(
                    zone_df,
                    {
                        "mark": {"type": "bar"},
                        "encoding": {
                            "x": {
                                "field": "label",
                                "type": "ordinal",
                                "axis": {"labelAngle": 0, "title": None},
                            },
                            "y": {
                                "field": "minutes",
                                "type": "quantitative",
                                "axis": {"title": None},
                            },
                            "color": {"field": "label", "type": "nominal"},
                            "tooltip": [
                                {"field": "label", "type": "nominal"},
                                {"field": "display", "type": "nominal"},
                            ],
                        },
                        "width": "container",
                        "height": 120,
                        "config": {"range": {"category": HR_ZONE_COLORS}},
                    },
                    width='stretch',
                )
                if avg_only_count:
                    st.caption(
                        "Time in zones is calculated from average heart rate only."
                    )

    if view == "Training plan":
        render_view_header("Training plan")
        st.caption(f"Source: `{TRAINING_PLAN_CSV_PATH}`")

        try:
            plan_df = load_training_plan()
        except Exception as exc:
            st.error(f"Could not load training plan CSV: {exc}")
            return

        if plan_df.empty:
            st.info("Training plan file is empty.")
            return

        if "row_type" in plan_df.columns:
            week_summary_df = plan_df[plan_df["row_type"].astype(str) == "week_summary"].copy()
            session_df = plan_df[plan_df["row_type"].astype(str) == "session"].copy()
        else:
            week_summary_df = plan_df.copy()
            session_df = plan_df.copy()

        week_options = (
            week_summary_df["iso_week"].dropna().astype(int).drop_duplicates().sort_values().tolist()
            if "iso_week" in week_summary_df.columns
            else []
        )
        if not week_options:
            st.warning("No week entries (`iso_week`) found in the training plan.")
            st.dataframe(plan_df, width='stretch', hide_index=True)
            return

        default_week = None
        if "week_pick" in st.session_state:
            try:
                default_week = int(st.session_state["week_pick"].isocalendar().week)
            except Exception:
                default_week = None
        if default_week not in week_options:
            default_week = week_options[0]

        selected_week = st.selectbox(
            "ISO week",
            week_options,
            index=week_options.index(default_week),
            key="training_plan_week_select",
        )

        selected_week_summary = week_summary_df[week_summary_df["iso_week"].astype("Int64") == selected_week].copy()
        selected_week_sessions = session_df[session_df["iso_week"].astype("Int64") == selected_week].copy()

        if not selected_week_summary.empty:
            top = selected_week_summary.iloc[0]
            render_metric_cards(
                [
                    ("Phase", str(top.get("phase", ""))),
                    ("Week start", str(top.get("week_start", ""))),
                    ("Weekly hours", f"{float(top.get('weekly_total_hours', 0) or 0):.2f} h"),
                    ("Sessions", f"{int(top.get('weekly_total_sessions', 0) or 0)}"),
                ],
                columns=4,
            )

            focus = str(top.get("weekly_focus", "")).strip()
            if focus and focus.lower() != "nan":
                st.info(focus)
            notes = str(top.get("notes", "")).strip()
            if notes and notes.lower() != "nan":
                st.caption(notes)

        if selected_week_sessions.empty:
            st.info("No session rows found for this week.")
        else:
            if "session_order" in selected_week_sessions.columns:
                selected_week_sessions = selected_week_sessions.sort_values("session_order")

            render_section_title("Week sessions")
            show_cols = [
                c
                for c in [
                    "day",
                    "session_name",
                    "sport",
                    "duration_min",
                    "intensity",
                    "vertical_target_m",
                    "session_description",
                ]
                if c in selected_week_sessions.columns
            ]
            st.dataframe(
                selected_week_sessions[show_cols],
                width='stretch',
                hide_index=True,
            )

            week_edit_df = plan_df[plan_df["iso_week"].astype("Int64") == selected_week].copy()
            with st.expander("Edit week (all fields)", expanded=False):
                st.caption("Edit any field for this week, then click Save week changes.")
                week_edit_source = week_edit_df.copy().fillna("")
                for col in week_edit_source.columns:
                    week_edit_source[col] = week_edit_source[col].astype(str)

                preferred_order = [
                    "row_type",
                    "day",
                    "session_order",
                    "session_name",
                    "sport",
                    "duration_min",
                    "intensity",
                    "vertical_target_m",
                    "session_description",
                    "iso_week",
                    "week_start",
                    "week_end",
                    "phase",
                    "weekly_focus",
                    "weekly_total_hours",
                    "weekly_total_minutes",
                    "weekly_run_sessions",
                    "weekly_strength_sessions",
                    "weekly_cross_sessions",
                    "weekly_total_sessions",
                    "plan_name",
                    "main_objective",
                    "notes",
                ]
                column_order = [c for c in preferred_order if c in week_edit_source.columns]
                column_order += [c for c in week_edit_source.columns if c not in column_order]

                with st.form("training_plan_week_edit_form", clear_on_submit=False):
                    editable_week_df = st.data_editor(
                        week_edit_source,
                        width='stretch',
                        hide_index=True,
                        num_rows="fixed",
                        key=f"training_plan_week_editor_{selected_week}",
                        disabled=False,
                        column_order=column_order,
                    )
                    save_week_clicked = st.form_submit_button(
                        "Save week changes",
                        type="primary",
                        use_container_width=True,
                    )

                if save_week_clicked:
                    try:
                        # Persist week edits as object-typed values first to avoid
                        # pandas dtype-assignment warnings on mixed text/numeric input.
                        plan_updated = plan_df.copy().astype("object")
                        editable_week_df = (
                            editable_week_df.reindex(columns=plan_updated.columns)
                            .astype("object")
                        )
                        plan_updated.loc[
                            week_edit_df.index, plan_updated.columns
                        ] = editable_week_df.to_numpy(dtype=object)
                        save_training_plan(plan_updated)
                        load_training_plan.clear()
                        st.success(f"Week {selected_week} saved.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Could not save week changes: {exc}")

        render_section_title("Target graphs")
        if session_df.empty:
            st.info("No session rows available to build target graphs.")
        else:
            chart_year = date.today().year
            if "week_start" in week_summary_df.columns:
                non_null_week_starts = week_summary_df["week_start"].dropna()
                if not non_null_week_starts.empty:
                    chart_year = int(non_null_week_starts.iloc[0].year)

            sessions_for_graph = session_df.copy()
            sessions_for_graph["duration_min"] = (
                pd.to_numeric(sessions_for_graph.get("duration_min"), errors="coerce")
                .fillna(0.0)
            )
            sessions_for_graph["duration_h"] = sessions_for_graph["duration_min"] / 60.0
            sessions_for_graph["iso_week"] = (
                pd.to_numeric(sessions_for_graph.get("iso_week"), errors="coerce")
                .astype("Int64")
            )
            sessions_for_graph = sessions_for_graph.dropna(subset=["iso_week"]).copy()
            sessions_for_graph["week"] = sessions_for_graph["iso_week"].astype(int)

            weekly_target = (
                sessions_for_graph.groupby("week", as_index=False)
                .agg(time_h=("duration_h", "sum"), sessions=("session_name", "count"))
                .sort_values("week")
            )

            if "sport" in sessions_for_graph.columns:
                sessions_for_graph["sport_bucket"] = sessions_for_graph["sport"].apply(
                    lambda s: _resolve_sport_bucket(str(s), ["Running", "Biking", "Weight training", "Other"])
                )
            else:
                sessions_for_graph["sport_bucket"] = "Other"

            by_sport_week = (
                sessions_for_graph.groupby(["week", "sport_bucket"], as_index=False)["duration_h"]
                .sum()
                .rename(columns={"duration_h": "time_h"})
            )
            by_sport_wide = (
                by_sport_week.pivot(index="week", columns="sport_bucket", values="time_h")
                .fillna(0.0)
                .sort_index()
            )
            sport_order = [c for c in ["Running", "Biking", "Weight training", "Other"] if c in by_sport_wide.columns]

            st.markdown("**Weekly target hours**")
            _render_weekly_chart(
                _weekly_area_point_chart(weekly_target, "time_h", "Hours", 220),
                key=f"plan_weekly_hours_{chart_year}",
                year=chart_year,
                supports_select=True,
            )

            st.markdown("**Weekly target sessions**")
            _render_weekly_chart(
                _weekly_area_point_chart(weekly_target, "sessions", "Sessions", 180),
                key=f"plan_weekly_sessions_{chart_year}",
                year=chart_year,
                supports_select=True,
            )

            if not by_sport_week.empty:
                st.markdown("**Weekly target hours by sport**")
                stack_chart = (
                    alt.Chart(by_sport_week)
                    .mark_area(opacity=0.35)
                    .encode(
                        x=alt.X("week:Q", title="Week of year"),
                        y=alt.Y("time_h:Q", title="Hours"),
                        color=alt.Color("sport_bucket:N", title="Sport"),
                        tooltip=[
                            alt.Tooltip("week:Q", title="Week"),
                            alt.Tooltip("sport_bucket:N", title="Sport"),
                            alt.Tooltip("time_h:Q", title="Hours"),
                        ],
                    )
                    .properties(height=220)
                )
                st.altair_chart(stack_chart, width='stretch')

                totals_by_sport = by_sport_week.groupby("sport_bucket")["time_h"].sum().to_dict()
                render_metric_cards(
                    [
                        (f"{label} target", f"{float(totals_by_sport.get(label, 0.0)):.2f} h")
                        for label in sport_order
                    ],
                    columns=max(1, min(4, len(sport_order))),
                )

                for sport_label in sport_order:
                    sport_week_df = by_sport_wide[[sport_label]].reset_index().rename(columns={sport_label: "time_h"})
                    st.markdown(f"**{sport_label} target hours**")
                    _render_weekly_chart(
                        _weekly_area_point_chart(sport_week_df, "time_h", "Hours", 150),
                        key=f"plan_weekly_{sport_label}_{chart_year}",
                        year=chart_year,
                        supports_select=True,
                    )

    if view == "Year overview":
        if "selected_year" not in st.session_state:
            st.session_state["selected_year"] = year_options[-1]
        if "year_refresh" not in st.session_state:
            st.session_state["year_refresh"] = 0
        year = st.sidebar.selectbox(
            "Year",
            options=year_options,
            key="selected_year",
            on_change=_bump_year_refresh,
        )
        refresh_token = st.session_state.get("year_refresh", 0)
        year_df_for_revision = _year_slice(df, year, selected_sports_key)
        year_revision = _year_bundle_revision(
            year_df_for_revision,
            selected_sports_key,
            overrides_mtime_ns=overrides_mtime_ns,
            zones_mtime_ns=zones_mtime_ns,
        )

        if year in snapshot_years and year < current_year:
            year_bundle = load_saved_year_overview_bundle_any_revision(year)
            if year_bundle is None:
                year_df_for_revision = _year_slice(df, year, selected_sports_key)
                year_revision = _year_bundle_revision(
                    year_df_for_revision,
                    selected_sports_key,
                    overrides_mtime_ns=overrides_mtime_ns,
                    zones_mtime_ns=zones_mtime_ns,
                )
                with st.spinner(f"Building saved year file for {year}..."):
                    year_bundle = get_year_overview_bundle(
                        year=year,
                        selected_sports=selected_sports_key,
                    )
                    save_year_overview_bundle(year, year_revision, year_bundle)
        else:
            year_bundle = get_year_overview_bundle(
                year=year,
                selected_sports=selected_sports_key,
            )
        year_df = year_bundle["year_df"]

        render_view_header("Year overview", str(year))
        if year_bundle["empty"]:
            st.info("No activities for this year with selected sports.")
        else:
            render_metric_cards(
                [
                    ("Total time", f"{year_bundle['total_time_h']:.2f} h"),
                    ("Total distance", f"{year_bundle['total_distance_km']:.1f} km"),
                    ("Elevation gain", f"{year_bundle['total_elev_m']:.0f} m"),
                ],
                columns=3,
            )

            zones = year_bundle["zones"]
            if not zones:
                st.info("HR zones config not found or invalid.")
            else:
                zone_totals = year_bundle["zone_totals"]
                missing_hr = year_bundle["missing_hr"]
                avg_only_count = year_bundle["avg_only_count"]
                if not year_bundle["has_zone_data"]:
                    st.info("No HR stream data available to compute zones for this year.")
                else:
                    chart_df = build_zone_chart_df(zone_totals, zones).copy()
                    chart_df["zone"] = chart_df["label"]
                    render_section_title("Time in HR zones")
                    st.vega_lite_chart(
                        chart_df,
                        {
                            "mark": {"type": "bar"},
                            "encoding": {
                                "x": {
                                    "field": "zone",
                                    "type": "ordinal",
                                    "axis": {"labelAngle": 0, "title": None},
                                },
                                "y": {
                                    "field": "minutes",
                                    "type": "quantitative",
                                    "axis": {"title": None},
                                },
                                "color": {"field": "zone", "type": "nominal"},
                                "tooltip": [
                                    {"field": "zone", "type": "nominal"},
                                    {"field": "display", "type": "nominal"},
                                ],
                            },
                            "width": "container",
                            "height": 200,
                            "config": {"range": {"category": HR_ZONE_COLORS}},
                        },
                        width='stretch',
                    )
                    if missing_hr:
                        st.caption(
                            f"HR zones computed from {len(year_df) - missing_hr} "
                            f"activities; {missing_hr} missing HR streams."
                        )
                    if avg_only_count:
                        st.caption(
                            "Some zone times are estimated from average heart rate only."
                        )
            render_section_title("Hours by sport")
            sport_order = year_bundle["sport_order"]
            totals_by_sport = year_bundle["totals_by_sport"]
            totals_km_by_sport = year_bundle["totals_km_by_sport"]

            sport_metrics = []
            for label in sport_order:
                hours_val = totals_by_sport[label]
                if label in {"Running", "Trail running", "Biking"}:
                    km_val = totals_km_by_sport[label]
                    value = f"{hours_val:.2f} ({km_val:.1f} km)"
                else:
                    value = f"{hours_val:.2f}"
                sport_metrics.append((label, value))
            render_metric_cards(sport_metrics, columns=3)

            render_section_title("Weekly hours by sport")
            all_sports_week = year_bundle["all_sports_week"]
            elev_week = year_bundle["elev_week"]

            st.markdown("**All sports (hours)**")
            _render_weekly_chart(
                _weekly_area_point_chart(all_sports_week, "time_h", "Hours", 240),
                key=f"weekly_all_sports_{year}_{refresh_token}",
                year=year,
                supports_select=True,
            )

            st.markdown("**Elevation gain (m)**")
            _render_weekly_chart(
                _weekly_area_point_chart(elev_week, "elev_m", "Meters", 240),
                key=f"weekly_elev_{year}_{refresh_token}",
                year=year,
                supports_select=True,
            )

            running_week = year_bundle["running_week"]
            st.markdown("**All running**")
            _render_weekly_chart(
                _weekly_hours_km_chart(running_week, "time_h", "distance_km", 160),
                key=f"weekly_running_{year}_{refresh_token}",
                year=year,
                supports_select=False,
            )

            for sport_label in sport_order:
                sport_week = year_bundle["sport_weeks"][sport_label]

                st.markdown(f"**{sport_label}**")
                if sport_label in {"Running", "Trail running", "Biking", "MountainBiking"}:
                    chart = _weekly_hours_km_chart(
                        sport_week, "time_h", "distance_km", 160
                    )
                else:
                    chart = _weekly_area_point_chart(sport_week, "time_h", "Hours", 160)
                _render_weekly_chart(
                    chart,
                    key=f"weekly_{sport_label}_{year}_{refresh_token}",
                    year=year,
                    supports_select=not (
                        sport_label
                        in {"Running", "Trail running", "Biking", "MountainBiking"}
                    ),
                )




if __name__ == "__main__":
    main()
