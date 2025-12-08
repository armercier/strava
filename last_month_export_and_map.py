from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
import gpxpy
import folium

# Import your existing GPX export function from your Strava module
from strava_tracks import export_gpx_for_activity  # adjust name if needed


"""
last_month_export_and_map.py

High-level description
----------------------
This module selects recent Strava activities from a CSV export, exports per-activity GPX files
(via an externally provided export_gpx_for_activity function), reads track points from those
GPX files, and renders all selected tracks onto an interactive Folium map using swisstopo
background tiles. The produced map is saved as an HTML file.

Primary behaviour / workflow
---------------------------
1. Load and filter activities from a CSV file (CSV_PATH).
2. Filter rows by sport type (TARGET_SPORTS) and by date (DAYS_BACK).
3. For each selected activity:
    - Ensure a GPX file exists (attempt to export using export_gpx_for_activity if not).
    - Parse GPX and extract track points (lat, lon).
    - Add track metadata and points to an in-memory list.
4. Render all tracks to a Folium map centered on the combined points using swisstopo tiles,
    draw each track as a PolyLine colored by sport, fit the viewport to bounds, and save to an HTML file.

Side effects
------------
- Reads CSV_PATH.
- May create GPX_DIR and write GPX files into it.
- Writes the final map HTML to OUT_HTML.
- Calls export_gpx_for_activity(activity_id): this function is expected to either return a
  Path (or string path) to a saved GPX file or raise a RuntimeError for recoverable issues
  (e.g., "No lat/lon data") or other exceptions for fatal errors.

Configuration expectations
--------------------------
- CSV_PATH: path to a Strava activities CSV export with at minimum:
     - an id column named 'id'
     - a sport column: one of 'sport_type', 'sport' or 'type'
     - a date column: 'date' or 'start_date_local' (ISO or parseable by pandas.to_datetime)
- TARGET_SPORTS: set of sport type strings to include (e.g., "Run", "Ride", "NordicSki", ...).
- DAYS_BACK: integer number of days before today used as a cut-off for activity_date.
- GPX_DIR: directory where GPX files will be stored/located.
- export_gpx_for_activity(activity_id): imported function that handles the Strava export logic.

Key functions (summary)
-----------------------
load_and_filter_activities() -> pandas.DataFrame
     - Loads CSV_PATH and normalizes column names to create an 'activity_date' column.
     - Detects sport column among common names.
     - Filters rows by TARGET_SPORTS and by activity_date >= today - DAYS_BACK.
     - Ensures 'id' is integer and returns a DataFrame with columns ['id', <sport_col>, 'activity_date']
     - Raises SystemExit if CSV file missing, RuntimeError if required columns absent.

read_gpx_points(path: pathlib.Path) -> list[tuple[float, float]]
     - Parses a GPX file using gpxpy and extracts a flat list of (latitude, longitude) tuples
        from all tracks/segments/points.
     - Raises RuntimeError if the GPX contains no points or propagates parsing errors.

build_swisstopo_map(tracks: list[dict], out_html: pathlib.Path) -> pathlib.Path
     - Accepts a list of track dictionaries, each with keys:
          'id'   : int
          'sport': str
          'date' : date
          'points': list[(lat, lon)]
     - Builds a Folium Map centered on the median point of all points, adds a swisstopo TileLayer,
        draws each track as a colored PolyLine (color per sport), fits map bounds to the track extent,
        saves the map to out_html and returns that path.
     - Uses a built-in simple color_map for known sports and falls back to black.
     - Assumes at least one track and that points are valid numeric lat/lon pairs.

     - Orchestrates the entire flow: filtering, exporting/reading GPX, assembling track data,
        calling the map builder, and printing progress.
     - Skips activities for which export_gpx_for_activity reports missing GPS streams
        ("No lat/lon data") or where GPX parsing fails.
     - Exits early with informative messages if no matching activities or no valid tracks to plot.

Dependencies
------------
- pandas
- gpxpy
- folium
- a user-supplied module function export_gpx_for_activity to produce GPX files for activity IDs

Error handling notes
--------------------
- Missing CSV_PATH -> SystemExit with a helpful message.
- Missing expected CSV columns -> RuntimeError.
- export_gpx_for_activity can raise RuntimeError for known recoverable conditions (handled
  and skipped), other exceptions will be re-raised.
- GPX parsing errors or empty GPX tracks cause those activities to be skipped with a printed
  message; they do not stop the whole run unless no tracks remain.

Usage example (conceptual)
--------------------------
- Configure CSV_PATH, GPX_DIR, OUT_HTML, TARGET_SPORTS and DAYS_BACK as desired.
- Ensure export_gpx_for_activity is implemented and imported.
- Run the module as a script (python last_month_export_and_map.py) or call main() from another
  driver script.

Notes
-----
- The module is tailored to create maps using the swisstopo tile service; if used outside Swiss
  contexts the tile URLs and attribution should be reviewed for appropriateness.
- The default DAYS_BACK value can be adjusted to implement calendar-month logic if required.
"""

# --------- CONFIG ---------
CSV_PATH = Path("activities_clean.csv")   # your full-activity CSV
GPX_DIR = Path("gpx")                     # where GPX files will be stored
OUT_HTML = Path("last_month_sport_map.html")

# Define which sports you want to include
TARGET_SPORTS = {
    "Run",
    "Ride",
    "NordicSki",
    "BackcountrySki",
    "AlpineSki",
    "SkiTouring",   # include both variants, Strava naming can vary
}

# How far back: last 30 days (change if you want calendar month logic)
DAYS_BACK = 1000
# --------------------------


def load_and_filter_activities() -> pd.DataFrame:
    """Load CSV and filter on sport + last month."""

    if not CSV_PATH.exists():
        raise SystemExit(f"CSV file not found: {CSV_PATH}")

    # Try to parse date columns robustly
    df = pd.read_csv(CSV_PATH)

    # --- Find sport column ---
    if "sport_type" in df.columns:
        sport_col = "sport_type"
    elif "sport" in df.columns:
        sport_col = "sport"
    elif "type" in df.columns:
        sport_col = "type"
    else:
        raise RuntimeError("No sport column found (expected sport_type/sport/type).")

    # --- Find date column ---
    if "date" in df.columns:
        df["activity_date"] = pd.to_datetime(df["date"]).dt.date
    elif "start_date_local" in df.columns:
        df["activity_date"] = pd.to_datetime(df["start_date_local"]).dt.date
    else:
        raise RuntimeError(
            "No date column found (expected 'date' or 'start_date_local')."
        )

    today = datetime.today().date()
    start_date = today - timedelta(days=DAYS_BACK)

    mask = (df[sport_col].isin(TARGET_SPORTS)) & (df["activity_date"] >= start_date)
    sub = df.loc[mask, ["id", sport_col, "activity_date"]].copy()

    # Ensure id is int
    sub["id"] = sub["id"].astype(int)

    sub = sub.sort_values("activity_date")
    return sub


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


def build_swisstopo_map(
    tracks: List[Dict[str, object]],
    out_html: Path,
) -> Path:
    """
    tracks: list of dicts with:
        {
          "points": List[(lat, lon)],
          "sport": str,
          "date": date,
          "id": int
        }
    """

    # Center on middle track / middle point
    all_points = [pt for t in tracks for pt in t["points"]]  # type: ignore
    mid_idx = len(all_points) // 2
    center = all_points[mid_idx]

    m = folium.Map(location=center, zoom_start=11, tiles=None)

    # swisstopo topo tiles
    folium.TileLayer(
        tiles=(
            "https://wmts.geo.admin.ch/1.0.0/"
            "ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg"
        ),
        attr="&copy; swisstopo / geo.admin.ch",
        name="swisstopo",
        max_zoom=19,
    ).add_to(m)

    # Simple color map per sport
    color_map = {
        "Run": "red",
        "Ride": "blue",
        "NordicSki": "green",
        "BackcountrySki": "purple",
        "AlpineSki": "orange",
        "SkiTouring": "darkgreen",
    }

    # Add each track
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

    # Fit to overall bounds
    lats = [lat for lat, lon in all_points]
    lons = [lon for lat, lon in all_points]
    bounds = [[min(lats), min(lons)], [max(lats), max(lons)]]
    m.fit_bounds(bounds)

    m.save(str(out_html))
    return out_html


def main():
    # 1. Filter activities from CSV
    df = load_and_filter_activities()
    if df.empty:
        print("No activities matching filters (sport + last month).")
        return

    print("Selected activities:")
    print(df)

    GPX_DIR.mkdir(exist_ok=True)

    tracks_for_map: List[Dict[str, object]] = []

    # 2. For each activity, export GPX (via Strava) and read points
    for _, row in df.iterrows():
        act_id = int(row["id"])
        sport = str(row.iloc[1])       # sport col is second in the subset
        act_date = row["activity_date"]

        print(f"Exporting GPX for {act_id} ({sport}, {act_date})...")
        gpx_path = GPX_DIR / f"activity_{act_id}.gpx"

        if not gpx_path.exists():
            try:
                # Call your existing export; this can raise "No lat/lon data..."
                gpx_path = export_gpx_for_activity(act_id)
            except RuntimeError as e:
                msg = str(e)
                if "No lat/lon data" in msg:
                    print(f"  -> skipping {act_id} (no GPS in streams)")
                    continue
                else:
                    # unexpected error: re-raise
                    raise

        try:
            pts = read_gpx_points(gpx_path)
        except Exception as e:
            print(f"  -> skipping {act_id} (error reading GPX: {e})")
            continue

        tracks_for_map.append(
            {"id": act_id, "sport": sport, "date": act_date, "points": pts}
        )

    if not tracks_for_map:
        print("No tracks to plot (e.g., all failed to export/read GPX).")
        return

    # 3. Build the swisstopo map with all tracks
    html_path = build_swisstopo_map(tracks_for_map, OUT_HTML)
    print("Map saved to:", html_path.resolve())


if __name__ == "__main__":
    main()