from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Set

import pandas as pd
import gpxpy
import folium

"""
build_gpx_map.py

Read existing GPX files for filtered activities and render them on a Folium
map (swisstopo tiles). This script does NOT call the Strava API; ensure GPX
files are fetched first (e.g., via fetch_gpx_range.py).

Configure MAP_DAYS_BACK independently from the fetch script.
"""

# --------- CONFIG ---------
CSV_PATH = Path("activities_clean.csv")
GPX_DIR = Path("gpx")
OUT_HTML = Path("gpx_sport_map.html")

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

# How far back to include GPX on the map (independent window)
MAP_DAYS_BACK = 11 * 365
# --------------------------


def load_and_filter_activities(days_back: int) -> pd.DataFrame:
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

    start_date = datetime.today().date() - timedelta(days=days_back)
    mask = (df[sport_col].isin(TARGET_SPORTS)) & (df["activity_date"] >= start_date)
    sub = df.loc[mask, ["id", sport_col, "activity_date"]].copy()
    sub["id"] = sub["id"].astype(int)
    return sub.sort_values("activity_date")


def read_gpx_points(path: Path) -> List[Tuple[float, float]]:
    """Return a list of (lat, lon) points from a GPX track."""
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


def build_layered_map(
    tracks: List[Dict[str, object]],
    out_html: Path,
) -> Path:
    """
    Build a Folium map with stacked base layers:
    - OpenStreetMap everywhere
    - swisstopo overlay (visible where tiles exist, i.e., Switzerland)
    """
    all_points = [pt for t in tracks for pt in t["points"]]  # type: ignore
    mid_idx = len(all_points) // 2
    center = all_points[mid_idx]

    m = folium.Map(location=center, zoom_start=11, tiles=None)

    folium.TileLayer(
        tiles="OpenStreetMap",
        name="OpenStreetMap",
        max_zoom=19,
        control=False,  # keep OSM as the base layer
    ).add_to(m)

    folium.TileLayer(
        tiles=(
            "https://wmts.geo.admin.ch/1.0.0/"
            "ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg"
        ),
        attr="&copy; swisstopo / geo.admin.ch",
        name="swisstopo (Switzerland)",
        max_zoom=19,
        overlay=True,
        show=False,  # start with swisstopo layer toggled off
    ).add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)

    color_map = {
        "Run": "#e41a1c",
        "Ride": "#377eb8",
        "Walk": "#4daf4a",
        "Hike": "#984ea3",
        "NordicSki": "#ff7f00",
        "BackcountrySki": "#a65628",
        "AlpineSki": "#f781bf",
        "SkiTouring": "#66c2a5",
        "StandUpPaddling": "#fc8d62",
        "Kayaking": "#8da0cb",
        "MountainBikeRide": "#e78ac3",
        "TrailRun": "#D12626",
    }

    for t in tracks:
        pts = t["points"]  # type: ignore
        sport = t["sport"]  # type: ignore
        date_str = str(t["date"])  # type: ignore
        act_id = t["id"]  # type: ignore

        col = color_map.get(sport, "black")
        folium.PolyLine(
            locations=pts,
            weight=3,
            opacity=0.9,
            color=col,
            tooltip=f"{sport} – {date_str} – {act_id}",
        ).add_to(m)

    lats = [lat for lat, lon in all_points]
    lons = [lon for lat, lon in all_points]
    bounds = [[min(lats), min(lons)], [max(lats), max(lons)]]
    m.fit_bounds(bounds)

    m.save(str(out_html))
    return out_html


def build_map_for_range(days_back: int = MAP_DAYS_BACK) -> None:
    df = load_and_filter_activities(days_back)
    if df.empty:
        print("No activities matching filters (sport + date window).")
        return

    tracks_for_map: List[Dict[str, object]] = []
    missing = 0

    for _, row in df.iterrows():
        act_id = int(row["id"])
        sport = str(row.iloc[1])
        act_date = row["activity_date"]
        gpx_path = GPX_DIR / f"activity_{act_id}.gpx"

        if not gpx_path.exists():
            missing += 1
            print(f"{act_id} ({sport}, {act_date}): GPX missing, skipping (fetch first)")
            continue

        try:
            pts = read_gpx_points(gpx_path)
        except Exception as e:
            print(f"{act_id} ({sport}, {act_date}): error reading GPX ({e}), skipping")
            continue

        tracks_for_map.append(
            {"id": act_id, "sport": sport, "date": act_date, "points": pts}
        )

    if not tracks_for_map:
        print("No tracks to plot (all missing or failed to read GPX).")
        return

    html_path = build_layered_map(tracks_for_map, OUT_HTML)
    print(f"Map saved to: {html_path.resolve()}")
    if missing:
        print(f"Note: {missing} activities were skipped due to missing GPX files.")


if __name__ == "__main__":
    build_map_for_range()
