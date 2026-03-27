from __future__ import annotations

import calendar
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
from strava_tracks import export_gpx_for_activity

CSV_PATH = Path("activities_clean.csv")
HR_ZONES_PATH = Path("hr_zones.json")
OVERRIDES_PATH = Path("activity_overrides.json")
WEEKLY_NOTES_PATH = Path("weekly_notes.json")
HR_STREAMS_DIR = Path("hr_streams")
YEAR_OVERVIEW_CACHE_DIR = Path("year_overview_cache")
YEAR_SNAPSHOT_PREVIOUS_COUNT = 2
FULL_MAP_HTML_PATH = Path("gpx_sport_map.html")
FULL_MAP_BUILD_SCRIPT = Path("build_gpx_map.py")
HR_ZONE_COLORS = [
    "#9ca3af",  # Recovery
    "#3b82f6",  # Zone 1
    "#22c55e",  # Zone 2
    "#fd7e14",  # Zone 3
    "#b91c1c",  # Zone 4
    "#8b5cf6",  # Zone 5
]
WEEKLY_PANEL_HEIGHT_PX = 140

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
    "total_elevation_gain",
    "average_watts",
    "average_heartrate",
}


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
            if col not in NUMERIC_OVERRIDE_FIELDS:
                continue
            try:
                val = float(raw_val)
            except (TypeError, ValueError):
                continue
            if col not in df.columns:
                df[col] = pd.NA
            df.loc[mask, col] = val

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
    return df


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
    """Return path to GPX for this activity; export from Strava if missing."""
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
    s = (sport or "").strip().lower()
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
    if "Weight" in s or "strength" in s:
        return "Weight training"
    return None


def _resolve_sport_bucket(sport: str, known_labels: list[str]) -> str:
    s = (sport or "").strip().lower()
    for label in known_labels:
        if s == label.strip().lower():
            return label
    bucket = _bucket_main_sport(sport)
    if bucket is not None:
        return bucket
    return "Other"


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
      <div id="plot-{activity_id}" style="flex:1; height:520px;"></div>
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

      const traces = [];
      if (data.hr && data.t_hr) {{
        traces.push({{
          x: data.t_hr,
          y: data.hr,
          mode: "lines",
          name: "HR (bpm)",
          line: {{color: "#FC4C02"}},
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
        }});
      }}
      if (data.power && data.t_power) {{
        traces.push({{
          x: data.t_power,
          y: data.power,
          mode: "lines",
          name: "Power (W)",
          yaxis: "y3",
          line: {{color: "#0ea5e9"}},
        }});
      }}

      const layout = {{
        margin: {{l: 40, r: 40, t: 20, b: 40}},
        xaxis: {{title: "Time (s)"}},
        yaxis: {{title: "HR"}},
        yaxis2: {{
          title: "Elevation (m)",
          overlaying: "y",
          side: "right",
          showgrid: false,
        }},
        yaxis3: {{
          title: "Power (W)",
          overlaying: "y",
          side: "right",
          anchor: "free",
          position: 0.95,
          showgrid: false,
        }},
        legend: {{orientation: "h", y: 1.1}},
        hovermode: "x",
      }};

      Plotly.newPlot(plotId, traces, layout, {{displayModeBar: false, responsive: true}})
        .then((gd) => {{
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


# ---------- STREAMLIT APP ----------


def main():
    st.set_page_config(page_title="Training Calendar", layout="wide")
    st.title("Training Calendar")

    if "ran_sync" not in st.session_state:
        with st.spinner("Syncing latest activities (GPX/HR/Power)..."):
            try:
                sync_latest()
                st.session_state["ran_sync"] = True
            except Exception as e:
                st.error(f"Sync failed: {e}")
                st.session_state["ran_sync"] = False

    df = load_activities()
    start_full_map_build(force=False)
    all_dates = sorted(df["activity_date"].unique())
    last_activity_date = max(all_dates) if all_dates else date.today()
    if "selected_activity_id" not in st.session_state:
        st.session_state["selected_activity_id"] = None
    if "selected_date" not in st.session_state:
        st.session_state["selected_date"] = last_activity_date

    default_date = last_activity_date

    st.sidebar.markdown(
        """
        <style>
        [data-testid="stSidebar"] .stButton > button {
            font-size: 12px;
            padding: 0.15rem 0.1rem;
            line-height: 1.1;
            white-space: nowrap;
        }
        [data-testid="stSidebar"] .stButton > button[kind="primary"] {
            background: #FC4C02;
            border-color: #FC4C02;
            color: #ffffff;
        }
        [data-testid="stSidebar"] .stButton > button[kind="primary"]:hover {
            background: #e04500;
            border-color: #e04500;
            color: #ffffff;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        """
        <style>
        [data-testid="stButton"] > button[kind="primary"] {
            background: #FC4C02;
            border-color: #FC4C02;
            color: #ffffff;
        }
        [data-testid="stButton"] > button[kind="primary"]:hover {
            background: #e04500;
            border-color: #e04500;
            color: #ffffff;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.sidebar.markdown(
"""
    <div style="display:flex; align-items:center; gap:10px; margin:6px 0 14px 0;">
      <div style="
          width:38px;
          height:38px;
          border-radius:8px;
          background:#FC4C02;
          display:flex;
          align-items:center;
          justify-content:center;
      ">
        <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
          <!-- Large triangle -->
          <path d="M13.45 3 L6.2 20 h4.3 l2.95-7 l2.95 7 h4.4 z"
                fill="#ffffff"/>
          <!-- Small triangle -->
          <path d="M13.45 13 L10.8 20 h2.65 z"
                fill="#ffffff"/>
        </svg>
      </div>

      <div style="
          font-weight:700;
          font-size:18px;
          letter-spacing:0.6px;
          font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      ">
        STRAVA
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

    selected_sports = df["sport"].dropna().unique().tolist()
    selected_sports_key = tuple(selected_sports)

    if all_dates:
        year_options = list(range(min(all_dates).year, max(all_dates).year + 1))
    else:
        year_options = [date.today().year]
    overrides_mtime_ns = _path_mtime_ns(OVERRIDES_PATH)
    zones_mtime_ns = _path_mtime_ns(HR_ZONES_PATH)
    current_year = date.today().year
    latest_year = year_options[-1]
    closed_year_options = [y for y in year_options if y < current_year]
    snapshot_years = tuple(closed_year_options[-YEAR_SNAPSHOT_PREVIOUS_COUNT:])

    snapshot_init_key = (
        snapshot_years,
        selected_sports_key,
        overrides_mtime_ns,
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

    cal_month = st.session_state["calendar_month"]
    st.sidebar.markdown("### Week selection")
    nav_cols = st.sidebar.columns([1, 4, 1])
    with nav_cols[0]:
        if st.button("◀", key="cal_prev", width='stretch'):
            year = cal_month.year
            month = cal_month.month - 1
            if month == 0:
                month = 12
                year -= 1
            st.session_state["calendar_month"] = date(year, month, 1)
            cal_month = st.session_state["calendar_month"]
    with nav_cols[2]:
        if st.button("▶", key="cal_next", width='stretch'):
            year = cal_month.year
            month = cal_month.month + 1
            if month == 13:
                month = 1
                year += 1
            st.session_state["calendar_month"] = date(year, month, 1)
            cal_month = st.session_state["calendar_month"]
    st.sidebar.markdown(
        f"**{calendar.month_name[cal_month.month]} {cal_month.year}**"
    )

    weekday_labels = ["Wk", "M", "T", "W", "T", "F", "S", "S"]
    header_cols = st.sidebar.columns(8)
    for col, label in zip(header_cols, weekday_labels):
        col.markdown(f"**{label}**")

    cal = calendar.Calendar(firstweekday=0)
    month_weeks = cal.monthdatescalendar(cal_month.year, cal_month.month)
    for week in month_weeks:
        week_cols = st.sidebar.columns(8)
        week_num = week[0].isocalendar().week
        week_has_selected_day = any(display_week_start <= d <= display_week_end for d in week)
        if week_has_selected_day:
            week_cols[0].markdown(f"<span style='color:#FC4C02; font-weight:700;'>{week_num}</span>", unsafe_allow_html=True)
        else:
            week_cols[0].markdown(f"**{week_num}**")
        for col, day in zip(week_cols[1:], week):
            is_current_month = day.month == cal_month.month
            label = str(day.day)
            is_in_displayed_week = display_week_start <= day <= display_week_end
            if col.button(
                label,
                key=f"cal_{day.isoformat()}",
                disabled=not is_current_month,
                type="primary" if is_in_displayed_week and is_current_month else "secondary",
                width='stretch',
            ):
                st.session_state["week_pick"] = day
                st.session_state["pending_view"] = "Week overview"

    st.sidebar.markdown("### Full sport map")
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

    if "active_tab" not in st.session_state:
        st.session_state["active_tab"] = "Week overview"
    if "pending_view" in st.session_state:
        st.session_state["active_tab"] = st.session_state.pop("pending_view")
    st.markdown(
        """
        <style>
        [data-testid="stMain"] .view-switch-row [data-testid="stButton"] > button {
            font-size: 16px;
            font-weight: 700;
            padding: 0.55rem 0.8rem;
            border-radius: 10px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown('<div class="view-switch-row">', unsafe_allow_html=True)
    view_labels = ["Week overview", "Activity detail", "Year overview"]
    view_cols = st.columns(3)
    for col, label in zip(view_cols, view_labels):
        with col:
            if st.button(
                label,
                key=f"switch_view_{label}",
                type="primary" if st.session_state["active_tab"] == label else "secondary",
                width='stretch',
            ):
                st.session_state["active_tab"] = label
                st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)
    view = st.session_state["active_tab"]

    if view == "Full map":
        st.subheader("Full sport map")
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
        week_dates = [week_start + timedelta(days=i) for i in range(7)]
        nav_left, nav_title, nav_right = st.columns([1, 6, 1])
        with nav_left:
            if st.button("◀", key=f"week_prev_{week_start.isoformat()}", type="primary", width='stretch'):
                new_week = week_start - timedelta(days=7)
                st.session_state["week_pick"] = new_week
                st.session_state["calendar_month"] = date(new_week.year, new_week.month, 1)
                st.rerun()
        with nav_title:
            st.subheader(f"Week of {week_start.isoformat()}")
        with nav_right:
            if st.button("▶", key=f"week_next_{week_start.isoformat()}", type="primary", width='stretch'):
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

                def _day_act_label(idx: int) -> str:
                    row = day_rows.iloc[idx]
                    return f"{row['sport']} – {row.get('name', '')}"

                selected_row_for_map = day_rows.iloc[selected_idx_from_state]
                selected_act_id_for_map = int(selected_row_for_map["id"])
                try:
                    gpx_path = ensure_gpx(selected_act_id_for_map)
                    pts = read_gpx_points(gpx_path)
                    mini_html = make_osm_map_mini(pts, height_px=WEEKLY_PANEL_HEIGHT_PX)
                    html(mini_html, height=WEEKLY_PANEL_HEIGHT_PX)
                except Exception:
                    col.markdown(
                        f"<div style='height:{WEEKLY_PANEL_HEIGHT_PX}px; display:flex; align-items:center; justify-content:center; font-size:0.8rem; color:#6b7280; border:1px dashed #d1d5db; border-radius:6px;'>Map unavailable</div>",
                        unsafe_allow_html=True,
                    )

                selected_idx = col.selectbox(
                    "Activity",
                    options=day_indices,
                    format_func=_day_act_label,
                    key=day_select_key,
                    label_visibility="collapsed",
                )
                selected_row = day_rows.iloc[int(selected_idx)]
                selected_act_id = int(selected_row["id"])

                if col.button(
                    "Open selected activity",
                    key=f"open_selected_{selected_act_id}_{d.isoformat()}",
                    width='stretch',
                ):
                    st.session_state["selected_activity_id"] = selected_act_id
                    st.session_state["selected_date"] = d
                    st.session_state["pending_view"] = "Activity detail"
                    try:
                        st.experimental_rerun()  # older Streamlit versions
                    except AttributeError:
                        st.rerun()  # newer Streamlit

                col.caption(f"{len(day_rows)} activit{'y' if len(day_rows) == 1 else 'ies'}")
                if zones and not day_acts.empty:
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

        st.markdown("### Weekly summary")
        week_end = week_start + timedelta(days=6)
        week_df = df[
            (df["activity_date"] >= week_start)
            & (df["activity_date"] <= week_end)
            & (df["sport"].isin(selected_sports))
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

            s1, s2, s3 = st.columns(3)
            s1.metric("Total time (h)", f"{total_time_h:.2f}")
            s2.metric("Total distance (km)", f"{total_distance_km:.1f}")
            s3.metric("Total elevation gain (m)", f"{total_elev_m:.0f}")

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
                    st.markdown("### Time in HR zones")
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

        st.subheader(f"Activities on {selected_date}")

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
            st.markdown("### Summary")

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

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Sport", act_row["sport"])
            m2.metric("Distance (km)", f"{distance_km:.1f}")
            m3.metric("Moving time (h)", f"{moving_h:.2f}")
            m4.metric("Elevation gain (m)", f"{elev:.0f}")

            m5, m6, m7, m8, m9 = st.columns(5)
            if avg_hr is not None:
                m5.metric("Avg HR", f"{avg_hr:.0f} bpm")
            if max_hr is not None:
                m6.metric("Max HR", f"{max_hr:.0f} bpm")
            if _is_bike_sport(act_row["sport"]):
                if avg_speed_mps is not None:
                    m7.metric("Avg speed (km/h)", f"{avg_speed_mps * 3.6:.1f}")
            elif _is_run_sport(act_row["sport"]):
                pace = _format_pace_from_speed(avg_speed_mps)
                if pace is not None:
                    m7.metric("Pace", pace)
            if avg_watts is not None and pd.notna(avg_watts):
                m8.metric("Avg power (W)", f"{avg_watts:.0f}")
            if kjs is not None:
                m9.metric("Work (kJ)", f"{kjs:.0f}")

        manual_overrides = load_activity_overrides().get(str(activity_id), {})
        if not isinstance(manual_overrides, dict):
            manual_overrides = {}
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
        current_elev = act_row.get("total_elevation_gain", None)
        current_power = act_row.get("average_watts", None)
        current_avg_hr = act_row.get("average_heartrate", None)
        current_elev_label = (
            f"{float(current_elev):.0f} m"
            if current_elev is not None and pd.notna(current_elev)
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
            st.markdown("### Notes")
            st.info(notes_override_default)
        with st.expander("Manual activity data", expanded=False):
            st.caption(
                "Saved in `activity_overrides.json` and applied across summaries/charts."
            )
            st.caption(
                "Current values shown: "
                f"elevation {current_elev_label}, avg power {current_power_label}, avg HR {current_hr_label}."
            )
            with st.form(key=f"manual_data_form_{activity_id}"):
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
                    elev_value = parse_optional_float(elev_input)
                    power_value = parse_optional_float(power_input)
                    hr_value = parse_optional_float(hr_input)
                except ValueError:
                    st.error("Invalid number format. Use digits like `850` or `245.5`.")
                else:
                    save_activity_overrides(
                        activity_id,
                        {
                            "total_elevation_gain": elev_value,
                            "average_watts": power_value,
                            "average_heartrate": hr_value,
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
                        "total_elevation_gain": None,
                        "average_watts": None,
                        "average_heartrate": None,
                        "notes": None,
                    },
                )
                load_activities.clear()
                st.success("Manual overrides cleared.")
                st.rerun()

        st.markdown("### Map + data plots")
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
            html(interactive_html, height=560, scrolling=False)
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
                st.markdown("### HR zones")
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

        st.subheader(f"Year summary {year}")
        if year_bundle["empty"]:
            st.info("No activities for this year with selected sports.")
        else:
            y1, y2, y3 = st.columns(3)
            y1.metric("Total time (h)", f"{year_bundle['total_time_h']:.2f}")
            y2.metric("Total distance (km)", f"{year_bundle['total_distance_km']:.1f}")
            y3.metric("Total elevation gain (m)", f"{year_bundle['total_elev_m']:.0f}")

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
                    st.markdown("### Time in HR zones")
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
            st.markdown("### Hours by sport")
            sport_order = year_bundle["sport_order"]
            totals_by_sport = year_bundle["totals_by_sport"]
            totals_km_by_sport = year_bundle["totals_km_by_sport"]

            cols = st.columns(3)
            for idx, label in enumerate(sport_order):
                hours_val = totals_by_sport[label]
                if label in {"Running", "Trail running", "Biking"}:
                    km_val = totals_km_by_sport[label]
                    value = f"{hours_val:.2f} ({km_val:.1f} km)"
                else:
                    value = f"{hours_val:.2f}"
                cols[idx % 3].metric(label, value)

            st.markdown("### Weekly hours by sport")
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
